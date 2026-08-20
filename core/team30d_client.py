# -*- coding: utf-8 -*-
"""30d.team 公开兑换 / 401 找回客户端。无需登录，卡密即凭证。"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://30d.team"


class Team30dError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def parse_card_codes(text: str) -> list[str]:
    """一行一个卡密；空行和 # 注释忽略，去重保序。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower() in seen:
            continue
        seen.add(line.lower())
        out.append(line)
    return out


def _decode_body(resp: requests.Response) -> Any:
    text = resp.text or ""
    if not text.strip():
        return {}
    try:
        return resp.json()
    except Exception:
        try:
            return json.loads(text)
        except Exception:
            return {"raw": text[:2000]}


def _error_message(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        for key in ("error", "message", "msg"):
            val = body.get(key)
            if val:
                return str(val)
    return fallback


class Team30dClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE,
        timeout: float = 30.0,
        session: requests.Session | None = None,
        sleep_fn=time.sleep,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE).strip().rstrip("/") or DEFAULT_BASE
        self.timeout = max(5.0, float(timeout or 30.0))
        self.session = session or requests.Session()
        self.sleep_fn = sleep_fn

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", str(path or "").lstrip("/"))

    def _request(self, method: str, path: str, *, json_body: dict | None = None, headers: dict | None = None) -> Any:
        hdrs = {"Accept": "application/json"}
        if json_body is not None:
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        try:
            resp = self.session.request(
                method,
                self._url(path),
                json=json_body,
                headers=hdrs,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise Team30dError(f"网络错误: {type(exc).__name__}: {exc}") from exc
        body = _decode_body(resp)
        if not resp.ok:
            raise Team30dError(
                _error_message(body, f"HTTP {resp.status_code}"),
                status_code=resp.status_code,
                body=body,
            )
        if isinstance(body, dict) and body.get("ok") is False:
            raise Team30dError(_error_message(body, "请求失败"), status_code=resp.status_code, body=body)
        return body

    def inventory_summary(self) -> dict:
        data = self._request("GET", "/api/redeem/inventory/summary")
        return data if isinstance(data, dict) else {"data": data}

    def preview(
        self,
        card_code: str,
        *,
        format: str = "sub2api",
        project: str = "30d_team",
        target_id: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "card_code": str(card_code or "").strip(),
            "format": format or "sub2api",
            "project": project or "30d_team",
        }
        if str(target_id or "").strip():
            payload["target_id"] = str(target_id).strip()
        data = self._request("POST", "/api/redeem/preview", json_body=payload)
        return data if isinstance(data, dict) else {"data": data}

    def redeem(
        self,
        card_code: str,
        *,
        format: str = "sub2api",
        project: str = "30d_team",
        target_id: str | None = None,
        action: str | None = None,
        client_request_id: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "card_code": str(card_code or "").strip(),
            "format": format or "sub2api",
            "project": project or "30d_team",
            "action": str(action or "").strip() or "redeem_remaining",
            "client_request_id": str(client_request_id or "").strip() or uuid.uuid4().hex,
        }
        if str(target_id or "").strip():
            payload["target_id"] = str(target_id).strip()
        data = self._request("POST", "/api/redeem/orders", json_body=payload)
        return data if isinstance(data, dict) else {"data": data}

    def order_status(self, order_no: str, download_token: str) -> dict:
        data = self._request(
            "GET",
            f"/api/redeem/orders/{order_no}",
            headers={"Authorization": f"Bearer {download_token}"},
        )
        return data if isinstance(data, dict) else {"data": data}

    def wait_order(
        self,
        order: dict,
        *,
        attempts: int = 90,
        interval: float = 2.0,
    ) -> dict:
        order_no, token, status = extract_order_fields(order)
        if not order_no or not token:
            return order
        current = dict(order)
        if _order_terminal(status):
            return current
        last = current
        for i in range(max(1, int(attempts))):
            polled = self.order_status(order_no, token)
            no2, tok2, st = extract_order_fields(polled)
            if isinstance(polled, dict):
                last = dict(polled)
                last["order_no"] = no2 or order_no
                last["download_token"] = tok2 or token
                last["status"] = st or status
            else:
                last = polled
            if _order_terminal(st):
                return last
            if i + 1 < attempts:
                self.sleep_fn(max(0.2, float(interval)))
        return last

    def download(self, order_no: str, download_token: str) -> Any:
        url = self._url(f"/api/redeem/orders/{order_no}/download")
        try:
            resp = self.session.get(
                url,
                params={"token": download_token},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise Team30dError(f"下载失败: {type(exc).__name__}: {exc}") from exc
        body = _decode_body(resp)
        if not resp.ok:
            raise Team30dError(
                _error_message(body, f"下载 HTTP {resp.status_code}"),
                status_code=resp.status_code,
                body=body,
            )
        return body

    def health_check(self, card_codes: list[str]) -> dict:
        data = self._request(
            "POST",
            "/api/redeem/reclaim/health-check",
            json_body={"card_codes": list(card_codes)},
        )
        return data if isinstance(data, dict) else {"data": data}

    def batch_reclaim(
        self,
        card_codes: list[str],
        *,
        mode: str = "401",
        query_only: bool = False,
    ) -> dict:
        payload: dict[str, Any] = {"card_codes": list(card_codes), "mode": mode or "401"}
        if query_only:
            payload["query_only"] = True
        data = self._request("POST", "/api/redeem/reclaim/batch-cards", json_body=payload)
        return data if isinstance(data, dict) else {"data": data}

    def poll_reclaim(
        self,
        card_codes: list[str],
        *,
        attempts: int = 60,
        interval: float = 3.0,
    ) -> dict:
        last: dict = {}
        for i in range(max(1, int(attempts))):
            last = self.batch_reclaim(card_codes, query_only=True)
            if not _reclaim_busy(last):
                return last
            if i + 1 < attempts:
                self.sleep_fn(max(0.5, float(interval)))
        return last


def _first_text(*vals: Any) -> str:
    for val in vals:
        text = str(val or "").strip()
        if text:
            return text
    return ""


def extract_order_fields(payload: Any) -> tuple[str, str, str]:
    """从兑换/状态响应抽出 order_no, download_token, status。

    提交接口把 token 放在顶层；轮询只回 order 对象且不含 token，必须两边都读。
    """
    data = payload if isinstance(payload, dict) else {}
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    order = data.get("order") if isinstance(data.get("order"), dict) else {}
    if not order and isinstance(nested.get("order"), dict):
        order = nested.get("order")
    src = order or nested or data
    order_no = _first_text(
        data.get("order_no"), data.get("orderNo"),
        src.get("order_no"), src.get("orderNo"),
        nested.get("order_no"),
    )
    token = _first_text(
        data.get("download_token"), data.get("downloadToken"),
        src.get("download_token"), src.get("downloadToken"),
        nested.get("download_token"),
    )
    status = _first_text(src.get("status"), data.get("status"), nested.get("status")).lower()
    return order_no, token, status


def _order_terminal(status: str) -> bool:
    st = str(status or "").strip().lower()
    return bool(st) and st not in {"pending", "processing", "probing", "replenishing", "exporting"}


def _reclaim_busy(payload: Any) -> bool:
    data = payload if isinstance(payload, dict) else {}
    nested = data.get("data") if isinstance(data.get("data"), dict) else data
    running = int(nested.get("already_running") or nested.get("running") or nested.get("pending") or 0)
    if running > 0:
        return True
    items = nested.get("items") or nested.get("results") or []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            st = str(item.get("status") or "").lower()
            if st in {"pending", "running", "processing"}:
                return True
    return False


def preview_action(preview: Any) -> str | None:
    """对照前端：有剩余额度则 redeem_remaining，否则 refresh_bound。"""
    data = preview if isinstance(preview, dict) else {}
    p = data.get("preview") if isinstance(data.get("preview"), dict) else data
    if p.get("can_redeem_remaining") and int(p.get("card_quota_remaining") or 0) > 0:
        return "redeem_remaining"
    if p.get("can_refresh_bound") and int(p.get("bound_count") or 0) > 0:
        return "refresh_bound"
    rec = str(p.get("recommended_action") or data.get("recommended_action") or "").strip()
    return rec or None


def health_need_reclaim(payload: Any) -> list[str]:
    """从 health-check 响应抽出需要找回的卡密。检测失败时返回空（不要当号坏）。"""
    data = payload if isinstance(payload, dict) else {}
    nested = data.get("data") if isinstance(data.get("data"), dict) else data
    codes: list[str] = []
    seen: set[str] = set()

    def _add(code: str) -> None:
        text = str(code or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            codes.append(text)

    items = nested.get("items") or nested.get("results") or nested.get("cards") or []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("card_code") or item.get("cardCode") or item.get("code") or "").strip()
            st = str(item.get("status") or item.get("state") or item.get("category") or "").lower()
            need = item.get("need_reclaim")
            if need is True or st in {"need_reclaim", "reclaim", "401", "unhealthy", "expired", "invalid"}:
                _add(code)
            # 凭据级列表
            creds = item.get("credentials") or item.get("accounts") or []
            if isinstance(creds, list) and any(
                str((c or {}).get("status") or "").lower() in {"need_reclaim", "reclaim", "401"}
                for c in creds if isinstance(c, dict)
            ):
                _add(code)
    need_list = nested.get("need_reclaim_codes") or nested.get("need_reclaim")
    if isinstance(need_list, list):
        for code in need_list:
            if isinstance(code, str):
                _add(code)
    return codes
