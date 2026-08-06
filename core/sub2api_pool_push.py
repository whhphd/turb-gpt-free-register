# -*- coding: utf-8 -*-
"""把本地 Codex 授权 JSON 推送到 sub2api 号池（POST /api/v1/admin/accounts/batch）。"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests

from config import sub2api as _cfg
from core import db

logger = logging.getLogger(__name__)


def _api_base() -> str:
    base = str(getattr(_cfg, "SUB2API_API_BASE", "") or getattr(_cfg, "SUB2_CODEX_API_BASE", "") or "").strip()
    return base.rstrip("/")


def _api_key() -> str:
    return str(
        getattr(_cfg, "SUB2API_API_KEY", "")
        or getattr(_cfg, "SUB2API_API_TOKEN", "")
        or getattr(_cfg, "SUB2_CODEX_API_TOKEN", "")
        or ""
    ).strip()


def _auth_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = _api_key()
    if not key:
        return headers
    header = str(getattr(_cfg, "SUB2API_API_AUTH_HEADER", "x-api-key") or "x-api-key").strip()
    prefix = str(getattr(_cfg, "SUB2API_API_AUTH_PREFIX", "") or "").strip()
    headers[header] = f"{prefix} {key}".strip() if prefix else key
    return headers


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_expires_unix(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        # ms → s
        if n > 10_000_000_000:
            n //= 1000
        return n if n > 0 else None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return _parse_expires_unix(int(s))
    try:
        # 2026-08-16T14:01:35Z / +08:00
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def _rfc3339_from_unix(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _looks_like_token(value: Any) -> bool:
    s = str(value or "").strip()
    return s.startswith("eyJ") or s.startswith("rt.") or s.startswith("rt_") or len(s) > 80


def _extract_token_blob(obj: dict) -> dict:
    """从各种嵌套结构里抽出含 access_token 的凭证对象。"""
    if not isinstance(obj, dict):
        return {}
    if obj.get("access_token") or obj.get("accessToken"):
        return obj
    # sub2api account
    creds = obj.get("credentials")
    if isinstance(creds, dict) and (creds.get("access_token") or creds.get("accessToken")):
        # 合并 name/email 到 blob 便于后续取邮箱
        out = dict(creds)
        if obj.get("name") and not out.get("email"):
            out.setdefault("email", obj.get("name"))
        if obj.get("email"):
            out.setdefault("email", obj.get("email"))
        return out
    # CPA / 回执包装
    for key in (
        "auth_json", "authJson", "auth", "auth_file", "authFile", "file",
        "tokens", "token", "oauth", "session",
    ):
        nested = obj.get(key)
        if isinstance(nested, dict):
            got = _extract_token_blob(nested)
            if got:
                return got
    data = obj.get("data")
    if isinstance(data, dict):
        got = _extract_token_blob(data)
        if got:
            return got
        # create-from-oauth data 本身可能是 account
        if isinstance(data.get("credentials"), dict):
            return _extract_token_blob(data)
    # cpa_submit_response / sub2_submit_response
    for key in ("cpa_submit_response", "sub2_submit_response", "submit_response"):
        nested = obj.get(key)
        if isinstance(nested, dict):
            got = _extract_token_blob(nested)
            if got:
                return got
    return {}


def detect_upload_json_format(payload: Any, *, filename: str = "") -> str:
    """识别上传 JSON 格式：codex_cpa / sub2api_export / sub2api_account / list / unknown。"""
    fname = str(filename or "").lower()
    if isinstance(payload, list):
        return "list"
    if not isinstance(payload, dict):
        return "unknown"
    # sub2api 导出包
    if isinstance(payload.get("accounts"), list):
        return "sub2api_export"
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        return "sub2api_export"
    # 单条 sub2api account
    if str(payload.get("platform") or "").lower() == "openai" and isinstance(payload.get("credentials"), dict):
        return "sub2api_account"
    if isinstance(payload.get("credentials"), dict) and (
        payload.get("credentials", {}).get("access_token") or payload.get("credentials", {}).get("accessToken")
    ):
        return "sub2api_account"
    # CPA / 本地 codex 凭证
    if payload.get("type") in ("codex", "openai", "oauth") or payload.get("access_token") or payload.get("accessToken"):
        return "codex_cpa"
    if _extract_token_blob(payload):
        # 文件名辅助
        if "cpa" in fname:
            return "codex_cpa"
        if "sub2" in fname:
            return "sub2api_account"
        return "codex_cpa"
    return "unknown"


def normalize_upload_json_to_codex_entries(
    payload: Any,
    *,
    filename: str = "",
) -> list[dict]:
    """把 CPA / sub2api 各种 JSON 统一成 codex 风格条目列表（含 access_token）。

    每项：{email, access_token, refresh_token?, id_token?, account_id?, ... , _format, _source_file}
    """
    fmt = detect_upload_json_format(payload, filename=filename)
    entries: list[dict] = []

    def _push_from_blob(blob: dict, *, fmt_name: str, idx: int = 0) -> None:
        tok = _extract_token_blob(blob)
        if not tok:
            return
        access = str(tok.get("access_token") or tok.get("accessToken") or "").strip()
        if not access:
            return
        # 展平成 codex 风格
        item = {
            "type": "codex",
            "email": str(tok.get("email") or blob.get("email") or blob.get("name") or "").strip(),
            "access_token": access,
            "refresh_token": str(tok.get("refresh_token") or tok.get("refreshToken") or "").strip(),
            "id_token": str(tok.get("id_token") or tok.get("idToken") or "").strip(),
            "account_id": str(
                tok.get("chatgpt_account_id")
                or tok.get("account_id")
                or blob.get("account_id")
                or blob.get("chatgpt_account_id")
                or ""
            ).strip(),
            "chatgpt_account_id": str(tok.get("chatgpt_account_id") or "").strip(),
            "plan_type": str(tok.get("plan_type") or tok.get("chatgpt_plan_type") or blob.get("plan_type") or "").strip(),
            "chatgpt_plan_type": str(tok.get("chatgpt_plan_type") or "").strip(),
            "client_id": str(tok.get("client_id") or blob.get("client_id") or "").strip(),
            "expires_at": tok.get("expires_at") or tok.get("expired") or blob.get("expires_at") or blob.get("expired"),
            "expired": blob.get("expired") or tok.get("expired"),
            "_format": fmt_name,
            "_source_file": filename,
            "_index": idx,
        }
        # 补 JWT 邮箱
        if not item["email"]:
            jwt = _decode_jwt_payload(access)
            profile = jwt.get("https://api.openai.com/profile") if isinstance(jwt.get("https://api.openai.com/profile"), dict) else {}
            item["email"] = str(profile.get("email") or jwt.get("email") or "").strip()
        entries.append(item)

    if fmt == "list" and isinstance(payload, list):
        for i, item in enumerate(payload):
            if isinstance(item, dict):
                sub_fmt = detect_upload_json_format(item, filename=filename)
                if sub_fmt == "sub2api_account":
                    _push_from_blob(item, fmt_name="sub2api_account", idx=i)
                else:
                    _push_from_blob(item, fmt_name=sub_fmt if sub_fmt != "unknown" else "codex_cpa", idx=i)
        return entries

    if fmt == "sub2api_export" and isinstance(payload, dict):
        accounts = payload.get("accounts")
        if not isinstance(accounts, list):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
        for i, acc in enumerate(accounts):
            if isinstance(acc, dict):
                _push_from_blob(acc, fmt_name="sub2api_export", idx=i)
        return entries

    if fmt in ("sub2api_account", "codex_cpa") and isinstance(payload, dict):
        _push_from_blob(payload, fmt_name=fmt, idx=0)
        return entries

    # 最后兜底再抽一次
    if isinstance(payload, dict):
        _push_from_blob(payload, fmt_name="unknown", idx=0)
    return entries


def build_pool_account_from_codex_json(
    content: dict,
    *,
    filename: str = "",
    group_id: int | None = None,
    concurrency: int | None = None,
    priority: int | None = None,
    load_factor: int | None = None,
    rate_multiplier: float | None = None,
) -> dict:
    """把 codex / CPA / 已展平的凭证 dict 转成 sub2api CreateAccountRequest。

    规则：
    - 核心凭据（access/refresh/id_token、account_id、email、plan 等）只从 JSON/JWT 取，不改写
    - 号池调度参数（group_ids / concurrency / priority / load_factor / rate_multiplier /
      auto_pause_on_expired）一律用本函数入参或 config.sub2api 保存配置，**忽略 JSON 里同名字段**
    """
    if not isinstance(content, dict):
        raise ValueError("凭证 JSON 不是对象")

    # 跳过仅回执、无 token 的 sub2-callback 本地摘要
    record_type = str(content.get("type") or "")
    if record_type == "codex_sub2_callback" or str(filename).endswith("-sub2-callback.json"):
        # 若包装里其实还能抽出 token，则继续
        blob = _extract_token_blob(content)
        if not blob:
            raise ValueError("sub2-callback 回执不含完整 token，请用带 access_token 的 JSON")
        content = {**content, **blob}

    # 若是 sub2api account 形态，先展平 credentials
    if isinstance(content.get("credentials"), dict) and not (
        content.get("access_token") or content.get("accessToken")
    ):
        flat = _extract_token_blob(content)
        if flat:
            content = {**content, **flat}

    access_token = str(content.get("access_token") or content.get("accessToken") or "").strip()
    if not access_token:
        # 再试深度抽取
        flat = _extract_token_blob(content)
        access_token = str(flat.get("access_token") or flat.get("accessToken") or "").strip()
        if access_token:
            content = {**content, **flat}
    if not access_token:
        raise ValueError("缺少 access_token")

    jwt = _decode_jwt_payload(access_token)
    profile = jwt.get("https://api.openai.com/profile") if isinstance(jwt.get("https://api.openai.com/profile"), dict) else {}
    auth = jwt.get("https://api.openai.com/auth") if isinstance(jwt.get("https://api.openai.com/auth"), dict) else {}

    email = str(
        content.get("email")
        or profile.get("email")
        or jwt.get("email")
        or ""
    ).strip()
    name = email or (filename.replace("codex-", "").replace(".json", "") if filename else "codex-account")

    chatgpt_account_id = str(
        content.get("chatgpt_account_id")
        or content.get("account_id")
        or auth.get("chatgpt_account_id")
        or ""
    ).strip()
    chatgpt_user_id = str(
        content.get("chatgpt_user_id")
        or content.get("user_id")
        or auth.get("user_id")
        or ""
    ).strip()
    organization_id = str(
        content.get("organization_id")
        or auth.get("organization_id")
        or auth.get("poid")
        or ""
    ).strip()
    plan_type = str(
        content.get("chatgpt_plan_type")
        or content.get("plan_type")
        or auth.get("chatgpt_plan_type")
        or ""
    ).strip()

    exp_unix = (
        _parse_expires_unix(content.get("expires_at"))
        or _parse_expires_unix(content.get("expired"))
        or _parse_expires_unix(jwt.get("exp"))
    )
    exp_rfc = _rfc3339_from_unix(exp_unix)

    credentials: dict[str, Any] = {
        "access_token": access_token,
    }
    for key, val in (
        ("refresh_token", content.get("refresh_token") or content.get("refreshToken")),
        ("id_token", content.get("id_token") or content.get("idToken")),
        ("email", email),
        ("chatgpt_account_id", chatgpt_account_id),
        ("chatgpt_user_id", chatgpt_user_id),
        ("organization_id", organization_id),
        ("plan_type", plan_type),
        ("client_id", content.get("client_id")),
        ("expires_at", exp_rfc),
    ):
        if isinstance(val, str) and val.strip():
            credentials[key] = val.strip()

    # 号池参数：只用保存配置 / 调用方入参，绝不读 content 里的 concurrency/group_ids 等
    gid = int(group_id if group_id is not None else getattr(_cfg, "SUB2API_POOL_GROUP_ID", 8) or 8)
    conc = int(concurrency if concurrency is not None else getattr(_cfg, "SUB2API_POOL_CONCURRENCY", 50) or 50)
    prio = int(priority if priority is not None else getattr(_cfg, "SUB2API_POOL_PRIORITY", 1) or 1)
    load = int(load_factor if load_factor is not None else getattr(_cfg, "SUB2API_POOL_LOAD_FACTOR", 10) or 10)
    rate = float(
        rate_multiplier
        if rate_multiplier is not None
        else getattr(_cfg, "SUB2API_POOL_RATE_MULTIPLIER", 1.0) or 1.0
    )
    auto_pause = bool(getattr(_cfg, "SUB2API_POOL_AUTO_PAUSE_ON_EXPIRED", True))

    account: dict[str, Any] = {
        "name": name,
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {
            "import_source": "codex_pool_push",
            "source_file": filename or "",
            "local_plan": content.get("plan_type") or content.get("chatgpt_plan_type") or "",
        },
        "group_ids": [gid],
        "concurrency": max(1, conc),
        "priority": prio,
        "load_factor": max(1, load),
        "rate_multiplier": rate,
        "auto_pause_on_expired": auto_pause,
    }
    if exp_unix:
        account["expires_at"] = int(exp_unix)
    return account


def _post_batch(accounts: list[dict], *, timeout: float) -> dict:
    base = _api_base()
    if not base:
        raise RuntimeError("未配置 SUB2API_API_BASE")
    if not _api_key():
        raise RuntimeError("未配置 SUB2API_API_KEY")
    url = urljoin(base + "/", "api/v1/admin/accounts/batch")
    headers = _auth_headers()
    headers["Idempotency-Key"] = f"codex-pool-push-{uuid.uuid4().hex}"
    resp = requests.post(url, headers=headers, json={"accounts": accounts}, timeout=timeout)
    try:
        body = resp.json()
    except Exception:
        body = {"raw": (resp.text or "")[:500]}
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {body}")
    # sub2api 常见 {code:0, data:{success,failed,...}}
    if isinstance(body, dict) and body.get("code") not in (0, None, "0"):
        raise RuntimeError(str(body.get("message") or body.get("error") or body)[:300])
    return body if isinstance(body, dict) else {"data": body}


def _pool_config_snapshot(
    *,
    group_id: int | None = None,
    concurrency: int | None = None,
    priority: int | None = None,
    load_factor: int | None = None,
    rate_multiplier: float | None = None,
) -> dict:
    return {
        "group_id": int(group_id if group_id is not None else getattr(_cfg, "SUB2API_POOL_GROUP_ID", 8) or 8),
        "concurrency": int(concurrency if concurrency is not None else getattr(_cfg, "SUB2API_POOL_CONCURRENCY", 50) or 50),
        "priority": int(priority if priority is not None else getattr(_cfg, "SUB2API_POOL_PRIORITY", 1) or 1),
        "load_factor": int(load_factor if load_factor is not None else getattr(_cfg, "SUB2API_POOL_LOAD_FACTOR", 10) or 10),
        "rate_multiplier": float(
            rate_multiplier
            if rate_multiplier is not None
            else getattr(_cfg, "SUB2API_POOL_RATE_MULTIPLIER", 1.0) or 1.0
        ),
        "api_base": _api_base(),
    }


def _dispatch_pool_batches(
    prepared: list[tuple[str, dict]],
    *,
    results: list[dict],
    mark_exported: bool = False,
) -> tuple[int, int]:
    """分批提交 prepared[(label, account)]，返回 (success, failed)。"""
    batch_size = max(1, int(getattr(_cfg, "SUB2API_POOL_BATCH_SIZE", 50) or 50))
    timeout = max(10.0, float(getattr(_cfg, "SUB2API_API_TIMEOUT", 20) or 20) * 3)
    success = 0
    failed = 0

    for i in range(0, len(prepared), batch_size):
        chunk = prepared[i : i + batch_size]
        accounts = [a for _, a in chunk]
        try:
            body = _post_batch(accounts, timeout=timeout)
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            items = []
            if isinstance(data, dict):
                items = data.get("items") or data.get("results") or data.get("accounts") or []
                batch_success = int(data.get("success") or data.get("created") or 0)
                batch_failed = int(data.get("failed") or 0)
            else:
                batch_success = 0
                batch_failed = 0

            if isinstance(items, list) and items:
                for idx, item in enumerate(items):
                    fname, acc = chunk[idx] if idx < len(chunk) else (f"#{idx}", {})
                    email = (acc or {}).get("name") or ""
                    if not isinstance(item, dict):
                        failed += 1
                        results.append({"filename": fname, "email": email, "ok": False, "error": str(item)})
                        continue
                    err = item.get("error") or item.get("message") or ""
                    ok_item = not err and str(item.get("action") or item.get("status") or "created").lower() not in (
                        "failed", "error", "skipped_error",
                    )
                    action = str(item.get("action") or "").lower()
                    if action in ("created", "updated", "success", ""):
                        ok_item = not err
                    if ok_item:
                        success += 1
                        if mark_exported:
                            try:
                                db.mark_codex_exported(fname)
                            except Exception:
                                pass
                        results.append({
                            "filename": fname,
                            "email": email or item.get("name") or item.get("email") or "",
                            "ok": True,
                            "account_id": item.get("account_id") or item.get("id"),
                            "action": item.get("action") or item.get("status"),
                        })
                    else:
                        failed += 1
                        results.append({
                            "filename": fname,
                            "email": email,
                            "ok": False,
                            "error": err or "batch item failed",
                        })
                if len(items) < len(chunk):
                    for fname, acc in chunk[len(items):]:
                        failed += 1
                        results.append({
                            "filename": fname,
                            "email": (acc or {}).get("name") or "",
                            "ok": False,
                            "error": "响应缺少明细",
                        })
            else:
                if batch_success <= 0 and batch_failed <= 0:
                    batch_success = len(chunk)
                ok_n = min(max(batch_success, 0), len(chunk))
                for idx, (fname, acc) in enumerate(chunk):
                    email = (acc or {}).get("name") or ""
                    if idx < ok_n:
                        success += 1
                        if mark_exported:
                            try:
                                db.mark_codex_exported(fname)
                            except Exception:
                                pass
                        results.append({"filename": fname, "email": email, "ok": True})
                    else:
                        failed += 1
                        results.append({
                            "filename": fname,
                            "email": email,
                            "ok": False,
                            "error": f"batch failed (success={batch_success}, failed={batch_failed})",
                        })
            time.sleep(0.15)
        except Exception as exc:
            logger.exception("[sub2api号池] batch 推送失败 size=%s", len(chunk))
            for fname, acc in chunk:
                failed += 1
                results.append({
                    "filename": fname,
                    "email": (acc or {}).get("name") or "",
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                })

    return success, failed


def parse_upload_json_text(raw: bytes | str, *, filename: str = "") -> Any:
    """解析上传文件内容为 JSON。"""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        text = str(raw or "")
    text = text.strip()
    if not text:
        raise ValueError(f"空文件: {filename or '(unnamed)'}")
    try:
        return json.loads(text)
    except Exception as exc:
        raise ValueError(f"JSON 解析失败 ({filename or 'unnamed'}): {exc}") from exc


def push_codex_files_to_pool(
    filenames: list[str],
    *,
    group_id: int | None = None,
    concurrency: int | None = None,
    priority: int | None = None,
    load_factor: int | None = None,
    rate_multiplier: float | None = None,
) -> dict:
    """推送本地 codex 凭证到 sub2api 号池。

    Returns:
      {
        ok, success, failed, skipped,
        results: [{filename, email, ok, error?, account_id?}],
        config: {...}
      }
    """
    files = [str(x).strip() for x in (filenames or []) if str(x).strip()]
    if not files:
        raise ValueError("filenames 为空")
    if len(files) > 500:
        raise ValueError("单次最多 500 个")

    prepared: list[tuple[str, dict]] = []  # (filename, account_payload)
    results: list[dict] = []
    skipped = 0

    for fname in files:
        try:
            raw, _ = db.read_codex_credential(fname)
            content = json.loads(raw)
            account = build_pool_account_from_codex_json(
                content,
                filename=fname,
                group_id=group_id,
                concurrency=concurrency,
                priority=priority,
                load_factor=load_factor,
                rate_multiplier=rate_multiplier,
            )
            prepared.append((fname, account))
        except Exception as exc:
            skipped += 1
            results.append({
                "filename": fname,
                "email": "",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "skipped": True,
            })

    success, failed = _dispatch_pool_batches(prepared, results=results, mark_exported=True)
    return {
        "ok": failed == 0 and success > 0,
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "total": len(files),
        "results": results,
        "config": _pool_config_snapshot(
            group_id=group_id,
            concurrency=concurrency,
            priority=priority,
            load_factor=load_factor,
            rate_multiplier=rate_multiplier,
        ),
    }


def push_uploaded_json_to_pool(
    uploads: list[tuple[str, Any]],
    *,
    group_id: int | None = None,
    concurrency: int | None = None,
    priority: int | None = None,
    load_factor: int | None = None,
    rate_multiplier: float | None = None,
) -> dict:
    """推送手动上传的 CPA / sub2api JSON 到号池。

    自动识别 CPA codex 凭证 / sub2api 单账号 / sub2api export(accounts[]) 等格式，
    只抽取核心 OAuth 凭据；号池调度参数统一用本函数入参或 SUB2API_POOL_* 保存配置覆盖。

    Args:
      uploads: [(filename, payload_or_raw_text), ...]
               payload 可为已解析 dict/list，或 bytes/str 原文。

    Returns:
      {
        ok, success, failed, skipped, total, account_count,
        formats: {filename: format_name},
        results: [...],
        config: {...}
      }
    """
    items = list(uploads or [])
    if not items:
        raise ValueError("uploads 为空")
    if len(items) > 200:
        raise ValueError("单次最多上传 200 个文件")

    prepared: list[tuple[str, dict]] = []
    results: list[dict] = []
    skipped = 0
    formats: dict[str, str] = {}
    account_count = 0

    for raw_name, raw_payload in items:
        fname = str(raw_name or "").strip() or "upload.json"
        try:
            if isinstance(raw_payload, (dict, list)):
                payload = raw_payload
            else:
                payload = parse_upload_json_text(raw_payload, filename=fname)
            fmt = detect_upload_json_format(payload, filename=fname)
            formats[fname] = fmt
            entries = normalize_upload_json_to_codex_entries(payload, filename=fname)
            if not entries:
                skipped += 1
                results.append({
                    "filename": fname,
                    "email": "",
                    "ok": False,
                    "error": f"未能识别有效账号（format={fmt}）",
                    "skipped": True,
                    "format": fmt,
                })
                continue
            for entry in entries:
                account_count += 1
                email = str(entry.get("email") or "").strip()
                label = f"{fname}#{entry.get('_index', 0)}"
                if email:
                    label = f"{fname}:{email}"
                try:
                    account = build_pool_account_from_codex_json(
                        entry,
                        filename=fname,
                        group_id=group_id,
                        concurrency=concurrency,
                        priority=priority,
                        load_factor=load_factor,
                        rate_multiplier=rate_multiplier,
                    )
                    # 标记来源为手动上传
                    extra = account.setdefault("extra", {})
                    if isinstance(extra, dict):
                        extra["import_source"] = "codex_pool_upload"
                        extra["source_format"] = entry.get("_format") or fmt
                        extra["source_file"] = fname
                    prepared.append((label, account))
                except Exception as exc:
                    skipped += 1
                    results.append({
                        "filename": label,
                        "email": email,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "skipped": True,
                        "format": entry.get("_format") or fmt,
                    })
        except Exception as exc:
            skipped += 1
            results.append({
                "filename": fname,
                "email": "",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "skipped": True,
            })

    success, failed = _dispatch_pool_batches(prepared, results=results, mark_exported=False)
    return {
        "ok": failed == 0 and success > 0,
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "total": len(items),
        "account_count": account_count,
        "formats": formats,
        "results": results,
        "config": _pool_config_snapshot(
            group_id=group_id,
            concurrency=concurrency,
            priority=priority,
            load_factor=load_factor,
            rate_multiplier=rate_multiplier,
        ),
    }
