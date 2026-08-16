# -*- coding: utf-8 -*-
"""BugTeam 客户取货 API 客户端。

客户端只负责 BugTeam 的 HTTP 协议和响应结构，不负责号池业务编排。
客户 token 只从服务端配置读取，绝不返回给 WebUI 或写入运行日志。
"""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from config import sub2api as _cfg

class BugTeamError(RuntimeError):
    """BugTeam API 错误，保留 HTTP 状态供上层分类。"""

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


class BugTeamConfigError(BugTeamError):
    """BugTeam 服务端 token 未配置。"""


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


def _message(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        for key in ("error", "message", "detail"):
            value = body.get(key)
            if value not in (None, ""):
                return str(value)[:500]
        data = body.get("data")
        if isinstance(data, dict):
            for key in ("error", "message", "detail"):
                value = data.get(key)
                if value not in (None, ""):
                    return str(value)[:500]
    return fallback


class BugTeamClient:
    """带 token、幂等键和有限重试的 BugTeam API 客户端。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        session: requests.Session | None = None,
        sleep_fn=time.sleep,
    ) -> None:
        self.base_url = (
            base_url
            or getattr(_cfg, "BUGTEAM_API_BASE", "https://bugteam.team")
            or "https://bugteam.team"
        ).strip().rstrip("/")
        self.token = str(
            token if token is not None else getattr(_cfg, "BUGTEAM_API_TOKEN", "") or ""
        ).strip()
        try:
            self.timeout = max(
                1.0,
                float(
                    timeout
                    if timeout is not None
                    else getattr(_cfg, "BUGTEAM_API_TIMEOUT", 30) or 30
                ),
            )
        except (TypeError, ValueError):
            self.timeout = 30.0
        try:
            self.max_retries = max(
                0,
                min(
                    5,
                    int(
                        max_retries
                        if max_retries is not None
                        else getattr(_cfg, "BUGTEAM_MAX_RETRIES", 2) or 2
                    ),
                ),
            )
        except (TypeError, ValueError):
            self.max_retries = 2
        self.session = session or requests.Session()
        self.sleep_fn = sleep_fn

    @property
    def token_configured(self) -> bool:
        return bool(self.token)

    def _ensure_configured(self) -> None:
        if not self.token:
            raise BugTeamConfigError("未配置 BugTeam 客户 token")

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

    def _headers(
        self,
        *,
        json_body: bool = False,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {"Accept": "application/json", "X-Customer-Token": self.token}
        if json_body:
            headers["Content-Type"] = "application/json"
        if extra:
            headers.update({str(k): str(v) for k, v in extra.items() if v is not None})
        return headers

    def _request_json(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        retry_network: bool | None = None,
        idempotency_key: str | None = None,
        accepted_statuses: tuple[int, ...] = (200, 201, 202),
    ) -> Any:
        self._ensure_configured()
        method = str(method or "GET").upper()
        url = self._url(path_or_url)
        if retry_network is None:
            retry_network = method in ("GET", "HEAD") or (
                method == "POST" and bool(idempotency_key)
            )
        network_attempts = self.max_retries + 1 if retry_network else 1
        rate_attempts = 0
        attempt = 0

        while True:
            attempt += 1
            request_headers = self._headers(json_body=json_body is not None, extra=headers)
            if idempotency_key:
                request_headers["Idempotency-Key"] = idempotency_key
            try:
                response = self.session.request(
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
                raise BugTeamError(
                    f"BugTeam 网络请求失败: {type(exc).__name__}: {exc}"
                ) from exc

            body = self._decode_body(response)
            status = int(getattr(response, "status_code", 0) or 0)
            retry_after = _retry_after(getattr(response, "headers", {}), body)
            if status == 429 and rate_attempts < self.max_retries:
                rate_attempts += 1
                wait = retry_after if retry_after is not None else min(2.0**rate_attempts, 30.0)
                self.sleep_fn(max(0.0, wait))
                continue
            if status not in accepted_statuses:
                safe_target = urlsplit(url).path or "/"
                raise BugTeamError(
                    f"BugTeam {method} {safe_target} 失败: {_message(body, f'HTTP {status}')}"[:1000],
                    status_code=status,
                    body=body,
                    retry_after=retry_after,
                )
            return body

    @staticmethod
    def _as_dict(body: Any) -> dict[str, Any]:
        return body if isinstance(body, dict) else {"data": body}

    def dashboard(self) -> dict[str, Any]:
        return self._as_dict(self._request_json("GET", "/api/customer/dashboard"))

    def balance(self) -> dict[str, Any]:
        return self._as_dict(self._request_json("GET", "/api/customer/balance"))

    def inventory(self, product: str, quantity: int) -> dict[str, Any]:
        body = self._request_json(
            "GET",
            "/api/customer/inventory",
            params={"product": str(product), "quantity": int(quantity)},
        )
        return self._as_dict(body)

    def create_order(self, product: str, quantity: int, *, idempotency_key: str) -> dict[str, Any]:
        body = self._request_json(
            "POST",
            "/api/customer/pickup/orders",
            json_body={"product": str(product), "quantity": int(quantity)},
            idempotency_key=str(idempotency_key),
            retry_network=True,
        )
        return self._as_dict(body)

    def order_status(self, order_id: str, *, status_url: str | None = None) -> dict[str, Any]:
        return self._as_dict(
            self._request_json("GET", status_url or f"/api/customer/pickup/orders/{order_id}")
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        body = self._request_json(
            "POST",
            f"/api/customer/pickup/orders/{order_id}/cancel",
            json_body={},
            retry_network=False,
        )
        return self._as_dict(body)

    def download_order(self, order_id: str, *, format: str = "sub2") -> dict[str, Any]:
        body = self._request_json(
            "GET",
            f"/api/customer/pickup/orders/{order_id}/download",
            params={"format": str(format or "sub2")},
        )
        return self._as_dict(body)

    def take_order(self, order_id: str, *, take_url: str | None = None) -> dict[str, Any]:
        """兼容现有补池编排：BugTeam 完成后直接下载，不再执行 take。"""
        return self.download_order(order_id, format="sub2")

    def finalize_order(self, order_id: str) -> dict[str, Any]:
        """BugTeam 自动交付没有独立 finalize；返回当前订单状态供编排层继续轮询。"""
        return self.order_status(order_id)

    def list_recoveries(
        self,
        *,
        state: str = "claimable",
        before_id: Any = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "state": str(state or "claimable"),
            "limit": max(1, min(100, int(limit or 50))),
        }
        if before_id not in (None, "", 0, "0"):
            params["before_id"] = before_id
        return self._as_dict(
            self._request_json("GET", "/api/customer/recoveries", params=params)
        )

    def claim_recovery(
        self,
        recovery_id: Any,
        *,
        claim_url: str | None = None,
        ticket: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        path = claim_url or f"/api/customer/recoveries/{recovery_id}/claim"
        extra: dict[str, str] = {}
        if ticket:
            extra["X-Recovery-Ticket"] = str(ticket)
        body = self._request_json(
            "POST",
            path,
            headers=extra,
            retry_network=True,
            idempotency_key=idempotency_key or f"bugteam-recovery-{recovery_id}",
        )
        return self._as_dict(body)
