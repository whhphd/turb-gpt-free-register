# -*- coding: utf-8 -*-
"""SogouEdu 客户取货 API 客户端。

客户端只负责 HTTP、认证和供应商 API 结构，不负责号池业务规则。
Token 只保存在当前进程内；进程重启后使用 .env 中的用户名/密码重新登录。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

from config import sub2api as _cfg

logger = logging.getLogger(__name__)


class SogouEduError(RuntimeError):
    """供应商 API 错误，保留 HTTP 状态和响应体供编排服务分类。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: Any = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after


class SogouEduConfigError(SogouEduError):
    """供应商登录配置缺失。"""


@dataclass(frozen=True)
class SogouEduResponse:
    status_code: int
    body: Any
    headers: dict[str, str]


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _retry_after(headers: Any, body: Any = None) -> float | None:
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        pass
    if raw is None and isinstance(body, dict):
        raw = body.get("retry_after_seconds") or body.get("retry_after")
    value = _number(raw)
    return max(0.0, value) if value is not None else None


def _nested_value(body: Any, *keys: str) -> Any:
    if not isinstance(body, dict):
        return None
    for key in keys:
        if body.get(key) not in (None, ""):
            return body.get(key)
    data = body.get("data")
    if isinstance(data, dict):
        for key in keys:
            if data.get(key) not in (None, ""):
                return data.get(key)
    return None


class SogouEduClient:
    """带 Token、401 重登录和有限重试的 SogouEdu API 客户端。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        session: requests.Session | None = None,
        sleep_fn=time.sleep,
    ) -> None:
        self.base_url = (base_url or getattr(_cfg, "SOGOUEDU_API_BASE", "https://sogouedu.cc") or "https://sogouedu.cc").strip().rstrip("/")
        self.username = str(username if username is not None else getattr(_cfg, "SOGOUEDU_USERNAME", "") or "").strip()
        self.password = str(password if password is not None else getattr(_cfg, "SOGOUEDU_PASSWORD", "") or "").strip()
        try:
            self.timeout = max(1.0, float(timeout if timeout is not None else getattr(_cfg, "SOGOUEDU_API_TIMEOUT", 30) or 30))
        except (TypeError, ValueError):
            self.timeout = 30.0
        try:
            self.max_retries = max(0, min(5, int(max_retries if max_retries is not None else getattr(_cfg, "SOGOUEDU_MAX_RETRIES", 2) or 2)))
        except (TypeError, ValueError):
            self.max_retries = 2
        self.session = session or requests.Session()
        self.sleep_fn = sleep_fn
        self._token = ""

    @property
    def token_configured(self) -> bool:
        return bool(self._token)

    def _url(self, path_or_url: str) -> str:
        raw = str(path_or_url or "").strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        return urljoin(self.base_url + "/", raw.lstrip("/"))

    @staticmethod
    def _decode_body(resp: Any) -> Any:
        try:
            return resp.json()
        except Exception:
            raw = getattr(resp, "content", b"")
            if isinstance(raw, bytes):
                text = raw.decode("utf-8", errors="replace")
            else:
                text = str(getattr(resp, "text", raw) or "")
            try:
                return json.loads(text) if text.strip() else {}
            except Exception:
                return {"raw": text[:2000]}

    def _headers(self, *, json_body: bool = False, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["X-Customer-Token"] = self._token
        if json_body:
            headers["Content-Type"] = "application/json"
        if extra:
            headers.update({str(k): str(v) for k, v in extra.items() if v is not None})
        return headers

    def login(self) -> dict[str, Any]:
        if not self.username or not self.password:
            raise SogouEduConfigError("未配置 SogouEdu 用户名或密码")
        response = self._request_json(
            "POST",
            "/api/customer/login",
            json_body={"username": self.username, "password": self.password},
            authenticated=False,
            allow_relogin=False,
            retry_network=True,
        )
        token = _nested_value(response, "token", "access_token", "customer_token")
        if not token:
            raise SogouEduError("SogouEdu 登录响应缺少 token", body=response)
        self._token = str(token).strip()
        return response if isinstance(response, dict) else {"data": response}

    def _request_json(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
        allow_relogin: bool = True,
        retry_network: bool | None = None,
        idempotency_key: str | None = None,
        accepted_statuses: tuple[int, ...] = (200, 201, 202),
    ) -> Any:
        method = str(method or "GET").upper()
        url = self._url(path_or_url)
        is_order_post = method == "POST" and path_or_url.rstrip("/").endswith("/pickup/orders")
        if retry_network is None:
            retry_network = method in ("GET", "HEAD") or is_order_post and bool(idempotency_key)
        network_attempts = self.max_retries + 1 if retry_network else 1
        auth_attempted = False
        rate_attempts = 0
        attempt = 0

        while True:
            attempt += 1
            request_headers = self._headers(json_body=json_body is not None, extra=headers)
            if idempotency_key:
                request_headers["Idempotency-Key"] = idempotency_key
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=request_headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt < network_attempts:
                    self.sleep_fn(min(2.0 ** (attempt - 1), 8.0))
                    continue
                raise SogouEduError(f"SogouEdu 网络请求失败: {type(exc).__name__}: {exc}") from exc

            body = self._decode_body(resp)
            status = int(getattr(resp, "status_code", 0) or 0)
            retry_after = _retry_after(getattr(resp, "headers", {}), body)
            if status == 401 and authenticated and allow_relogin and not auth_attempted:
                auth_attempted = True
                self._token = ""
                self.login()
                continue
            if status == 429 and rate_attempts < self.max_retries:
                rate_attempts += 1
                wait = retry_after if retry_after is not None else min(2.0 ** rate_attempts, 30.0)
                self.sleep_fn(max(0.0, wait))
                continue
            if status not in accepted_statuses:
                message = _nested_value(body, "error", "message", "detail") or f"HTTP {status}"
                raise SogouEduError(
                    f"SogouEdu {method} {path_or_url} 失败: {message}",
                    status_code=status,
                    body=body,
                    retry_after=retry_after,
                )
            return body

    def request(self, method: str, path_or_url: str, **kwargs: Any) -> Any:
        """公开的 JSON 请求入口，供编排服务处理少量扩展接口。"""
        return self._request_json(method, path_or_url, **kwargs)

    def inventory(self, product: str, quantity: int) -> dict[str, Any]:
        body = self._request_json("GET", "/api/customer/inventory", params={"product": product, "quantity": int(quantity)})
        return body if isinstance(body, dict) else {"data": body}

    def balance(self) -> dict[str, Any]:
        body = self._request_json("GET", "/api/customer/balance")
        return body if isinstance(body, dict) else {"data": body}

    def create_order(self, product: str, quantity: int, *, idempotency_key: str) -> dict[str, Any]:
        body = self._request_json(
            "POST",
            "/api/customer/pickup/orders",
            json_body={"product": product, "quantity": int(quantity)},
            idempotency_key=idempotency_key,
            retry_network=True,
        )
        return body if isinstance(body, dict) else {"data": body}

    def order_status(self, order_id: str, *, status_url: str | None = None) -> dict[str, Any]:
        path = status_url or f"/api/customer/pickup/orders/{order_id}"
        body = self._request_json("GET", path)
        return body if isinstance(body, dict) else {"data": body}

    def take_order(self, order_id: str, *, take_url: str | None = None) -> dict[str, Any]:
        path = take_url or f"/api/customer/pickup/orders/{order_id}/take"
        body = self._request_json("POST", path, retry_network=False)
        return body if isinstance(body, dict) else {"data": body}

    def finalize_order(self, order_id: str) -> dict[str, Any]:
        """结算手动/API 提货订单当前已预留的账号。"""
        path = f"/api/customer/manual/orders/{order_id}/finalize"
        body = self._request_json("POST", path, retry_network=False)
        return body if isinstance(body, dict) else {"data": body}

    def list_recoveries(self, *, before_id: Any = None, limit: int = 100) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": max(1, min(100, int(limit or 100)))}
        if before_id not in (None, "", 0, "0"):
            params["before_id"] = before_id
        body = self._request_json("GET", "/api/customer/recoveries", params=params)
        return body if isinstance(body, dict) else {"data": body}

    def get_recovery(self, recovery_id: Any) -> dict[str, Any]:
        body = self._request_json("GET", f"/api/customer/recoveries/{recovery_id}")
        return body if isinstance(body, dict) else {"data": body}

    def claim_recovery(
        self,
        recovery_id: Any,
        *,
        claim_url: str | None = None,
        ticket: str | None = None,
    ) -> Any:
        url = claim_url
        if not url:
            detail = self.get_recovery(recovery_id)
            url = _nested_value(detail, "claim_url")
            ticket = ticket or _nested_value(detail, "ticket")
        if url:
            return self._request_json(
                "POST",
                str(url),
                headers={"Accept": "application/json", "X-Requested-With": "customer-console"},
                retry_network=False,
                allow_relogin=True,
            )
        path = f"/api/customer/recoveries/{recovery_id}/claim"
        params = {"ticket": ticket} if ticket else None
        return self._request_json("POST", path, params=params, retry_network=False, allow_relogin=True)
