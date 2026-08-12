# -*- coding: utf-8 -*-
"""
注册成功后的 Codex OAuth 授权模块（2026-06-15 改造：全新 session + 接码）。

旧方案"复用注册的已登录 session"会撞 /choose-an-account 卡死（React SPA 解析不出
可提交字段）。新方案改为用**全新干净 session**从头登录，走 OpenAI 标准风控路径，
手机号验证靠接码平台自动收码，当前通过 core.sms_provider 支持
GrizzlySMS / HeroSMS / SMSBower（SMS-Activate 兼容，OpenAI service=dr）以及 L/H 本地取号
定义的本地 L 取号服务。

完整接口链（2026-06-15 浏览器抓包确认，均 POST auth.openai.com，json）：
    1. 提交邮箱   /api/accounts/authorize/continue  {"username":{"kind":"email","value":邮箱}}  带 sentinel(authorize_continue)
    2. 验邮箱码   /api/accounts/email-otp/validate   {"code":"xxx"}                            带 sentinel(authorize_continue)
    3. 提交手机号 /api/accounts/add-phone/send       {"phone_number":"+1xxx","channel":"sms"}  无需 sentinel
    4. 验手机码   /api/accounts/phone-otp/validate   {"code":"xxx"}                            无需 sentinel
    5. 选 workspace /api/accounts/workspace/select   {"workspace_id":"<uuid>"}                  无需 sentinel
       workspace_id 从 oai-client-auth-session cookie（base64 解码）的 workspaces[0].id 取
    6. → 重定向 localhost:1455/auth/callback?code=ac_...，从 Location 抠 code

拿到 code 后换 token / 落盘的逻辑（exchange_codex_token / build_codex_storage /
save_codex_credential）沿用旧实现，未改动。
"""
import base64
import hashlib
import json
import logging
import random
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs, quote, unquote, urlunparse

# 用模块属性方式访问 config，支持 WebUI 热加载（config.reload_all()）。
# 协议级常量（CLIENT_ID/URL/SCOPE/OUTPUT_DIRNAME）虽然不会改，统一从 _cfg 读，
# 这样 reload 后立即生效，不用再分两套导入。
from config import codex as _cfg
from core.session import BrowserSession
from core.humanize import delay as human_delay
from core.openai_auth import (
    _is_transient_network_error,
    _extract_error_code,
    detect_account_unusable_response_body,
    AccountUnusableError,
    request_sentinel_token,
    build_sentinel_header,
    network_preflight,
)
from core import sms_provider
from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 跟重定向链时的最大跳数，防死循环
_MAX_REDIRECTS = 15

# 网络层临时性错误（代理抖动 / TLS 握手失败 / 重置）重试参数，对齐 openai_auth.follow_authorize
_NET_MAX_ATTEMPTS = 3
_NET_BACKOFF_BASE = 2.0


def _with_net_retry(label: str, fn):
    """
    对临时性网络错误（TLS/代理/超时/重置）做重试包装。
    非临时错误（业务 4xx 等）直接抛。最多 _NET_MAX_ATTEMPTS 次。
    """
    last_exc = None
    for attempt in range(1, _NET_MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not _is_transient_network_error(exc):
                raise
            if attempt >= _NET_MAX_ATTEMPTS:
                break
            backoff = _NET_BACKOFF_BASE ** (attempt - 1)
            logger.warning(
                f"[Codex] {label} 临时性网络错误 ({type(exc).__name__}: {str(exc)[:120]})，"
                f"{backoff:.1f}s 后重试 (尝试 {attempt}/{_NET_MAX_ATTEMPTS})..."
            )
            time.sleep(backoff)
    raise last_exc if last_exc else RuntimeError(f"[Codex] {label} 重试耗尽但无异常记录")


def _codex_result(
    *,
    status: str,
    ok: bool = False,
    http_status: int | None = None,
    email: str | None = None,
    file_path: str | None = None,
    callback_url: str | None = None,
    message: str = "",
) -> dict:
    """构造与 flow_trigger._flow_result 同形态的结构化结果。"""
    return {
        "status": status,
        "ok": ok,
        "http_status": http_status,
        "email": email,
        "file_path": file_path,
        "callback_url": callback_url,
        "message": message,
    }


# ============================================================
# PKCE / state（对照 CLIProxyAPI pkce.go）
# ============================================================

def _generate_pkce() -> tuple[str, str]:
    """生成 PKCE 代码对：verifier=base64url(96字节)，challenge=base64url(sha256(verifier))。"""
    verifier_bytes = secrets.token_bytes(96)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _generate_state() -> str:
    """生成 OAuth state 随机串，防 CSRF。"""
    return secrets.token_urlsafe(32)


def _build_authorize_url(state: str, code_challenge: str, prompt: str = "login") -> str:
    """按 CLIProxyAPI openai_auth.go 的参数集拼 Codex 授权 URL。"""
    params = {
        "client_id": _cfg.CODEX_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _cfg.CODEX_REDIRECT_URI,
        "scope": _cfg.CODEX_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": prompt,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return f"{_cfg.CODEX_AUTH_URL}?{urlencode(params)}"


def _ensure_oai_context_url(auth_url: str, session: BrowserSession) -> str:
    """在 Codex OAuth 授权 URL 上补齐前端同源上下文参数，保持 oai-did 连续。"""
    try:
        parsed = urlparse(auth_url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        changed = False
        additions = {
            "ext-oai-did": session.device_id,
            "auth_session_logging_id": session.auth_session_logging_id,
            "screen_hint": "login_or_signup",
        }
        for key, value in additions.items():
            if not params.get(key):
                params[key] = [value]
                changed = True
        if not changed:
            return auth_url
        query = urlencode(params, doseq=True)
        return parsed._replace(query=query).geturl()
    except Exception:
        return auth_url


# ============================================================
# CPA 管理接口：授权地址由 CPA 生成，成功回调提交给 CPA
# ============================================================

def _sub2_configured() -> bool:
    """判断 sub2api 管理接口是否已配置到可用状态。"""
    try:
        from config import sub2api as _sub2_cfg
    except Exception:
        return False
    base = str(
        getattr(_sub2_cfg, "SUB2API_API_BASE", "")
        or getattr(_sub2_cfg, "SUB2_CODEX_API_BASE", "")
        or ""
    ).strip()
    key = str(
        getattr(_sub2_cfg, "SUB2_CODEX_API_TOKEN", "")
        or getattr(_sub2_cfg, "SUB2API_API_KEY", "")
        or getattr(_sub2_cfg, "SUB2API_API_TOKEN", "")
        or ""
    ).strip()
    return bool(base and key)


def _cpa_configured() -> bool:
    url = str(getattr(_cfg, "CPA_MANAGEMENT_URL", "") or "").strip()
    key = str(getattr(_cfg, "CPA_MANAGEMENT_KEY", "") or "").strip()
    return bool(url and key)


def _codex_auth_url_source() -> str:
    """
    解析授权地址来源。

    默认 local：本程序自己做 PKCE + 换 token + 落盘，不依赖 CPA/sub2 admin key。
    显式配置 cpa/sub2 但密钥缺失时，自动回退 local。
    """
    raw = str(getattr(_cfg, "CODEX_AUTH_URL_SOURCE", "local") or "local").strip().lower()
    # 兼容别名
    if raw in ("sub2api", "sub2_api", "sub"):
        raw = "sub2"
    if raw in ("self", "pkce", "native"):
        raw = "local"
    if raw == "cpa" and not _cpa_configured():
        logger.warning(
            "[Codex] CODEX_AUTH_URL_SOURCE=cpa 但未配置 CPA_MANAGEMENT_KEY，"
            "已自动改用 local（本程序自行换 token 并保存凭据）"
        )
        return "local"
    if raw == "sub2" and not _sub2_configured():
        logger.warning(
            "[Codex] CODEX_AUTH_URL_SOURCE=sub2 但未配置 SUB2API_API_BASE/KEY，"
            "已自动改用 local（本程序自行换 token 并保存凭据）"
        )
        return "local"
    return raw or "local"


def _cpa_management_origin() -> str:
    raw = str(getattr(_cfg, "CPA_MANAGEMENT_URL", "") or "").strip()
    if not raw:
        raise RuntimeError("[Codex][CPA] 尚未配置 CPA_MANAGEMENT_URL")
    try:
        parsed = urlparse(raw)
    except Exception as exc:
        raise RuntimeError(f"[Codex][CPA] CPA_MANAGEMENT_URL 格式无效: {raw}") from exc
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(f"[Codex][CPA] CPA_MANAGEMENT_URL 格式无效: {raw}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _cpa_management_key() -> str:
    key = str(getattr(_cfg, "CPA_MANAGEMENT_KEY", "") or "").strip()
    if not key:
        raise RuntimeError("[Codex][CPA] 尚未配置 CPA_MANAGEMENT_KEY")
    return key


def _cpa_request_json(method: str, path: str, body: dict | None = None) -> dict:
    """调用 CPA 管理接口，兼容 FlowPilot 的 /v0/management/* 协议。"""
    origin = _cpa_management_origin()
    key = _cpa_management_key()
    timeout = int(getattr(_cfg, "CPA_REQUEST_TIMEOUT", 30) or 30)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "X-Management-Key": key,
    }
    url = f"{origin}{path}"
    session = curl_requests.Session()
    try:
        resp = session.request(
            method.upper(),
            url,
            headers=headers,
            data=None if body is None else json.dumps(body),
            timeout=timeout,
        )
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if resp.status_code < 200 or resp.status_code >= 300:
            msg = ""
            if isinstance(payload, dict):
                msg = payload.get("error") or payload.get("message") or payload.get("detail") or payload.get("reason") or ""
            raise RuntimeError(
                f"[Codex][CPA] 管理接口失败 {method.upper()} {path} status={resp.status_code}: "
                f"{msg or (resp.text or '')[:300]}"
            )
        return payload if isinstance(payload, dict) else {}
    finally:
        try:
            session.close()
        except Exception:
            pass


def _sub2_codex_base() -> str:
    from config import sub2api as _sub2_cfg
    raw = str(
        getattr(_sub2_cfg, "SUB2API_API_BASE", "")
        or getattr(_sub2_cfg, "SUB2_CODEX_API_BASE", "")
        or ""
    ).strip().rstrip("/")
    if not raw:
        raise RuntimeError("[Codex][sub2] 尚未配置 SUB2API_API_BASE")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(f"[Codex][sub2] SUB2API_API_BASE 格式无效: {raw}")
    return raw


def _sub2_codex_headers() -> dict:
    from config import sub2api as _sub2_cfg
    token = str(getattr(_sub2_cfg, "SUB2_CODEX_API_TOKEN", "") or getattr(_sub2_cfg, "SUB2API_API_KEY", "") or getattr(_sub2_cfg, "SUB2API_API_TOKEN", "") or "").strip()
    auth_header = str(getattr(_sub2_cfg, "SUB2_CODEX_AUTH_HEADER", "") or getattr(_sub2_cfg, "SUB2API_API_AUTH_HEADER", "x-api-key") or "x-api-key").strip()
    auth_prefix = str(getattr(_sub2_cfg, "SUB2_CODEX_AUTH_PREFIX", "") or getattr(_sub2_cfg, "SUB2API_API_AUTH_PREFIX", "") or "").strip()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "turb-gpt-free-register/codex-sub2",
    }
    if token:
        headers[auth_header] = f"{auth_prefix} {token}".strip() if auth_prefix else token
    return headers


def _sub2_extract_error_message(payload: dict | None, fallback: str = "") -> str:
    if not isinstance(payload, dict):
        return fallback
    for key in ("error", "message", "detail", "reason", "msg"):
        val = payload.get(key)
        if isinstance(val, dict):
            nested = val.get("message") or val.get("error") or val.get("detail") or ""
            if nested:
                return str(nested)
        if val not in (None, ""):
            # sub2api 成功响应 message=success，失败时 message 才是错误
            text = str(val)
            if key == "message" and text.strip().lower() in ("success", "ok", "true"):
                continue
            return text
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("error", "message", "detail", "reason"):
            val = data.get(key)
            if val not in (None, ""):
                return str(val)
    return fallback


def _sub2_codex_request_json(method: str, path: str, body: dict | None = None) -> dict:
    from config import sub2api as _sub2_cfg
    base = _sub2_codex_base()
    timeout = int(getattr(_sub2_cfg, "SUB2API_API_TIMEOUT", 20) or 20)
    normalized_path = "/" + str(path or "").lstrip("/")
    url = f"{base}{normalized_path}"
    session = curl_requests.Session()
    try:
        resp = session.request(
            method.upper(),
            url,
            headers=_sub2_codex_headers(),
            data=None if body is None else json.dumps(body),
            timeout=timeout,
        )
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if resp.status_code < 200 or resp.status_code >= 300:
            msg = _sub2_extract_error_message(payload if isinstance(payload, dict) else None, "")
            raise RuntimeError(
                f"[Codex][sub2] 接口失败 {method.upper()} {normalized_path} status={resp.status_code}: "
                f"{msg or (resp.text or '')[:300]}"
            )
        if not isinstance(payload, dict):
            return {}
        # sub2api 统一 envelope：{"code":0,"message":"success","data":...}
        code = payload.get("code")
        if code not in (None, 0, "0", "success", "ok", True):
            msg = _sub2_extract_error_message(payload, f"code={code}")
            raise RuntimeError(
                f"[Codex][sub2] 接口业务失败 {method.upper()} {normalized_path}: {msg}"
            )
        return payload
    finally:
        try:
            session.close()
        except Exception:
            pass


def _request_sub2_authorize_url() -> dict:
    """从 sub2api 生成 Codex OAuth 授权地址；本地不生成 PKCE。

    对接：POST /api/v1/admin/openai/generate-auth-url
    响应 data: {auth_url, session_id}（state 从 auth_url 解析）
    """
    from config import sub2api as _sub2_cfg
    path = str(getattr(_sub2_cfg, "SUB2_CODEX_AUTH_URL_PATH", "/api/v1/admin/openai/generate-auth-url") or "/api/v1/admin/openai/generate-auth-url")
    if not _sub2_configured():
        raise RuntimeError(
            "[Codex][sub2] 尚未配置 SUB2API_API_BASE / SUB2API_API_KEY，"
            "无法通过 sub2api 生成授权地址"
        )
    logger.info("[Codex][sub2] 正在通过 sub2api 管理接口生成授权地址... base=%s path=%s", _sub2_codex_base(), path)
    # 空 body 即可；可选 proxy_id / redirect_uri，当前用 sub2api 默认 localhost:1455 callback
    payload = _sub2_codex_request_json("POST", path, {})
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    auth_url = _first_non_empty(
        payload.get("url"), payload.get("auth_url"), payload.get("authUrl"),
        data.get("url"), data.get("auth_url"), data.get("authUrl"),
    )
    session_id = _first_non_empty(
        payload.get("session_id"), payload.get("sessionId"),
        data.get("session_id"), data.get("sessionId"),
    )
    state = _first_non_empty(
        payload.get("state"), payload.get("auth_state"), payload.get("authState"),
        data.get("state"), data.get("auth_state"), data.get("authState"),
        _extract_state_from_auth_url(auth_url),
    )
    if not auth_url.startswith("http"):
        raise RuntimeError(f"[Codex][sub2] sub2api 未返回有效 auth_url: {payload}")
    if not state:
        raise RuntimeError("[Codex][sub2] 授权地址缺少 state")
    logger.info("[Codex][sub2] 已获取授权地址 session_id=%s state=%s...", session_id or "-", state[:12])
    logger.info("[Codex][sub2] 完整授权地址: %s", auth_url)
    if not session_id:
        logger.warning("[Codex][sub2] 授权地址响应缺少 session_id，后续 create-from-oauth 会失败")
    return {"auth_url": auth_url, "state": state, "session_id": session_id, "origin": _sub2_codex_base(), "raw": payload}


def _summarize_sub2_response(payload: dict) -> str:
    """压缩 sub2api 响应日志，避免整包刷屏，同时保留账号创建关键信息。"""
    try:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        parts = []
        if isinstance(data, dict):
            for key in ("id", "account_id", "name", "email", "platform", "type"):
                val = data.get(key)
                if val not in (None, ""):
                    parts.append(f"{key}={val}")
        if parts:
            return " ".join(parts)
        if isinstance(payload, dict):
            compact = {k: payload.get(k) for k in ("code", "message", "success") if k in payload}
            return str(compact or payload)[:300]
    except Exception:
        pass
    return str(payload)[:300]


def _submit_sub2_callback(
    callback_url: str,
    *,
    session_id: str = "",
    redirect_uri: str = "",
    name: str = "",
) -> dict:
    """提交 OAuth callback 给 sub2api。

    默认对接：POST /api/v1/admin/openai/create-from-oauth
    body: session_id + code + state (+ redirect_uri/name/concurrency/priority)
    """
    from config import sub2api as _sub2_cfg
    path = str(getattr(_sub2_cfg, "SUB2_CODEX_CALLBACK_PATH", "/api/v1/admin/openai/create-from-oauth") or "/api/v1/admin/openai/create-from-oauth")
    mode = str(getattr(_sub2_cfg, "SUB2_CODEX_CALLBACK_PAYLOAD_MODE", "create_from_oauth") or "create_from_oauth").strip().lower()
    if mode == "callback_url":
        body = {"callback_url": str(callback_url or "").strip()}
    elif mode == "redirect_url":
        body = {"redirect_url": str(callback_url or "").strip()}
    else:
        parsed = urlparse(str(callback_url or ""))
        qs = parse_qs(parsed.query)
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        if not session_id:
            raise RuntimeError("[Codex][sub2] create-from-oauth 缺少 session_id（generate-auth-url 应返回）")
        if not code:
            raise RuntimeError(f"[Codex][sub2] callback_url 缺少 code: {callback_url}")
        if not state:
            raise RuntimeError(f"[Codex][sub2] callback_url 缺少 state: {callback_url}")
        body = {"session_id": session_id, "code": code, "state": state}
        if redirect_uri:
            body["redirect_uri"] = redirect_uri
        if name:
            body["name"] = str(name).strip()
        if mode in {"create_from_oauth", "create-from-oauth", "create_oauth_account"}:
            body.setdefault("concurrency", 3)
            body.setdefault("priority", 50)
        if mode in {"exchange_code", "exchange-code"}:
            # 仅换 token：走 exchange-code 路径
            path = str(getattr(_sub2_cfg, "SUB2_CODEX_EXCHANGE_PATH", "/api/v1/admin/openai/exchange-code") or "/api/v1/admin/openai/exchange-code")

    max_attempts = max(1, int(getattr(_cfg, "CPA_CALLBACK_SUBMIT_RETRIES", 5) or 5))
    base_delay = max(1.0, float(getattr(_cfg, "CPA_CALLBACK_SUBMIT_RETRY_DELAY", 6) or 6))
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info("[Codex][sub2] 正在上传 OAuth callback 到 sub2api（第 %s/%s 次）... path=%s callback=%s", attempt, max_attempts, path, callback_url)
            payload = _sub2_codex_request_json("POST", path, body)
            logger.info("[Codex][sub2] callback 已上传并处理完成（第 %s 次成功）响应=%s", attempt, _summarize_sub2_response(payload))
            return payload
        except Exception as exc:
            last_exc = exc
            retryable = _is_cpa_callback_retryable(exc)
            if attempt >= max_attempts or not retryable:
                logger.warning("[Codex][sub2] callback 上传失败且不再重试：attempt=%s/%s retryable=%s error=%s", attempt, max_attempts, retryable, exc)
                raise
            delay = base_delay * attempt
            logger.warning("[Codex][sub2] callback 上传失败，将在 %.1fs 后重试：attempt=%s/%s error=%s", delay, attempt, max_attempts, exc)
            time.sleep(delay)
    raise RuntimeError(f"[Codex][sub2] callback 上传失败：{last_exc}")



def _cpa_request_raw(method: str, path: str, body: dict | None = None, *, response_type: str = "text"):
    """调用 CPA 管理接口并返回原始响应；用于下载 auth-files 这类非 JSON 响应。"""
    origin = _cpa_management_origin()
    key = _cpa_management_key()
    timeout = int(getattr(_cfg, "CPA_REQUEST_TIMEOUT", 30) or 30)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "X-Management-Key": key,
    }
    url = f"{origin}{path}"
    session = curl_requests.Session()
    try:
        resp = session.request(
            method.upper(),
            url,
            headers=headers,
            data=None if body is None else json.dumps(body),
            timeout=timeout,
        )
        if resp.status_code < 200 or resp.status_code >= 300:
            msg = ""
            try:
                payload = resp.json()
                if isinstance(payload, dict):
                    msg = payload.get("error") or payload.get("message") or payload.get("detail") or payload.get("reason") or ""
            except Exception:
                pass
            raise RuntimeError(
                f"[Codex][CPA] 管理接口失败 {method.upper()} {path} status={resp.status_code}: "
                f"{msg or (resp.text or '')[:300]}"
            )
        if response_type == "bytes":
            return resp.content
        return resp.text
    finally:
        try:
            session.close()
        except Exception:
            pass


def list_cpa_codex_auth_files() -> list[dict]:
    """读取 CPA auth-files 列表，仅返回 type/name/email 可识别为 codex 的凭证。"""
    payload = _cpa_request_json("GET", "/v0/management/auth-files")
    files = payload.get("files") if isinstance(payload.get("files"), list) else []
    out = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        ftype = str(item.get("type") or "").strip().lower()
        email = str(item.get("email") or "").strip().lower()
        if ftype == "codex" or name.lower().startswith("codex-") or "codex" in name.lower():
            copied = dict(item)
            copied["name"] = name
            copied["email"] = email or str(item.get("email") or "")
            out.append(copied)
    return out


def find_cpa_codex_auth_file(*, email: str = "", local_filename: str = "") -> dict | None:
    """按本地回执/凭证文件名或邮箱匹配 CPA 侧 codex auth 文件。"""
    email_l = str(email or "").strip().lower()
    local_name_l = str(local_filename or "").strip().lower()
    local_stem_l = local_name_l[:-5] if local_name_l.endswith(".json") else local_name_l
    files = list_cpa_codex_auth_files()
    if not files:
        return None

    def score(item: dict) -> int:
        name_l = str(item.get("name") or "").lower()
        item_email_l = str(item.get("email") or "").lower()
        s = 0
        if local_name_l and name_l == local_name_l:
            s = max(s, 100)
        if local_stem_l and name_l.startswith(local_stem_l):
            s = max(s, 80)
        if email_l and item_email_l == email_l:
            s = max(s, 70)
        if email_l and email_l in name_l:
            s = max(s, 60)
        # 本地 CPA 回执名一般是 codex-邮箱-cpa-callback.json，CPA 实际文件是 codex-邮箱-free.json。
        if local_stem_l.endswith("-cpa-callback"):
            base = local_stem_l[:-len("-cpa-callback")]
            if base and name_l.startswith(base + "-"):
                s = max(s, 75)
        return s

    ranked = sorted(((score(item), item) for item in files), key=lambda x: x[0], reverse=True)
    return ranked[0][1] if ranked and ranked[0][0] > 0 else None


def download_cpa_codex_auth_text(*, cpa_name: str | None = None, email: str = "", local_filename: str = "") -> tuple[str, str, dict]:
    """
    从 CPA auth-files 下载一个 Codex JSON 文本。
    Returns: (content_text, download_filename, matched_file_meta)
    """
    meta = None
    name = str(cpa_name or "").strip()
    if name:
        # 已经拿到 CPA 文件名时直接下载，不再额外拉取一次 auth-files 列表。
        # 账号列表批量下载会先统一列一次列表；这里重复列会导致选中多账号时浏览器长时间等待下载确认。
        meta = {"name": name}
    else:
        meta = find_cpa_codex_auth_file(email=email, local_filename=local_filename)
        name = str((meta or {}).get("name") or "").strip()
    if not name:
        target = email or local_filename or cpa_name or "未知"
        raise RuntimeError(f"[Codex][CPA] 未在 CPA auth-files 中找到匹配的 Codex 凭证: {target}")
    text = _cpa_request_raw("GET", f"/v0/management/auth-files/download?name={quote(name, safe='')}", response_type="text")
    # 下载接口正常应返回 JSON 文本，这里做一次轻校验，避免把 HTML/错误文本当凭证导出。
    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise RuntimeError(f"[Codex][CPA] CPA 下载内容不是有效 JSON: {name}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"[Codex][CPA] CPA 下载内容不是 JSON 对象: {name}")
    return json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", name, (meta or {"name": name})

def _first_non_empty(*values) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _extract_state_from_auth_url(auth_url: str) -> str:
    try:
        return parse_qs(urlparse(auth_url).query).get("state", [""])[0]
    except Exception:
        return ""


def _request_cpa_authorize_url() -> dict:
    """从 CPA 生成 Codex OAuth 授权地址；本地不生成 PKCE。"""
    logger.info("[Codex][CPA] 正在通过 CPA 管理接口生成授权地址...")
    payload = _cpa_request_json("GET", "/v0/management/codex-auth-url")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    auth_url = _first_non_empty(
        payload.get("url"),
        payload.get("auth_url"),
        payload.get("authUrl"),
        data.get("url"),
        data.get("auth_url"),
        data.get("authUrl"),
    )
    state = _first_non_empty(
        payload.get("state"),
        payload.get("auth_state"),
        payload.get("authState"),
        data.get("state"),
        data.get("auth_state"),
        data.get("authState"),
        _extract_state_from_auth_url(auth_url),
    )
    if not auth_url.startswith("http"):
        raise RuntimeError(f"[Codex][CPA] CPA 未返回有效 auth_url: {payload}")
    if not state:
        raise RuntimeError("[Codex][CPA] CPA 授权地址缺少 state")
    logger.info(f"[Codex][CPA] 已获取授权地址，state={state[:12]}...")
    logger.info(f"[Codex][CPA] 完整授权地址: {auth_url}")
    return {
        "auth_url": auth_url,
        "state": state,
        "origin": _cpa_management_origin(),
        "raw": payload,
    }


def _is_cpa_callback_retryable(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return (
        "status=409" in text
        or "timeout waiting for oauth callback" in text
        or "timeout" in text
        or "timed out" in text
        or "connection" in text
        or "status=429" in text
        or "status=500" in text
        or "status=502" in text
        or "status=503" in text
        or "status=504" in text
    )


def _is_cpa_callback_reauth_error(exc_or_text) -> bool:
    """CPA 收到 callback 后仍 409 timeout，通常需要重新生成授权地址重新跑一轮 OAuth。"""
    text = str(exc_or_text or "").lower()
    return (
        "oauth-callback" in text
        and "status=409" in text
        and "timeout waiting for oauth callback" in text
    ) or (
        "timeout waiting for oauth callback" in text
    )


def _submit_cpa_callback(callback_url: str) -> dict:
    """提交 OAuth callback 给 CPA。

    CPA 偶发会在浏览器已拿到 localhost callback 后仍返回
    “409 Timeout waiting for OAuth callback”，通常是管理端等待/入库的竞态；
    这里按同一个 callback URL 做多次重试，不重新生成授权地址。
    """
    body = {
        "provider": "codex",
        "redirect_url": str(callback_url or "").strip(),
    }
    max_attempts = max(1, int(getattr(_cfg, "CPA_CALLBACK_SUBMIT_RETRIES", 5) or 5))
    base_delay = max(1.0, float(getattr(_cfg, "CPA_CALLBACK_SUBMIT_RETRY_DELAY", 6) or 6))
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(
                "[Codex][CPA] 正在提交 OAuth callback 给 CPA（第 %s/%s 次）... callback=%s",
                attempt, max_attempts, str(callback_url or "")
            )
            payload = _cpa_request_json("POST", "/v0/management/oauth-callback", body)
            logger.info("[Codex][CPA] callback 已提交（第 %s 次成功）", attempt)
            return payload
        except Exception as exc:
            last_exc = exc
            retryable = _is_cpa_callback_retryable(exc)
            if attempt >= max_attempts or not retryable:
                logger.warning(
                    "[Codex][CPA] callback 提交失败且不再重试：attempt=%s/%s retryable=%s error=%s",
                    attempt, max_attempts, retryable, exc
                )
                raise
            delay = base_delay * attempt
            logger.warning(
                "[Codex][CPA] callback 提交失败，将在 %.1fs 后重试：attempt=%s/%s error=%s",
                delay, attempt, max_attempts, exc
            )
            time.sleep(delay)
    raise RuntimeError(f"[Codex][CPA] callback 提交失败：{last_exc}")


# ============================================================
# 小工具：判定/解析
# ============================================================

def _is_redirect_uri(location: str) -> bool:
    """判断 Location 是否指向注册的 redirect_uri（localhost:1455/auth/callback）。"""
    try:
        parsed = urlparse(location)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and \
        parsed.hostname in ("localhost", "127.0.0.1") and \
        parsed.port == 1455 and \
        parsed.path == "/auth/callback"


def _extract_code(location: str, state: str) -> str:
    """从 redirect_uri 的 Location 里提取并校验 code。"""
    parsed = urlparse(location)
    qs = parse_qs(parsed.query)
    err = (qs.get("error") or [""])[0]
    if err:
        err_desc = (qs.get("error_description") or [""])[0]
        raise RuntimeError(f"[Codex] 授权服务器返回错误: error={err}, desc={err_desc}")
    code = (qs.get("code") or [""])[0]
    if not code:
        raise RuntimeError(f"[Codex] redirect_uri 缺少 code 参数: {location}")
    returned_state = (qs.get("state") or [""])[0]
    if returned_state and returned_state != state:
        raise RuntimeError(
            f"[Codex] state 不匹配（疑似 CSRF）: expected={state[:8]}..., got={returned_state[:8]}..."
        )
    return code


def _decode_jwt_segment(seg: str) -> dict:
    """base64url 解码一个 JWT/cookie 段为 JSON dict（失败返回 {}）。"""
    try:
        padding = "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg + padding))
    except Exception:
        return {}


def _post_json(session: BrowserSession, url: str, payload: dict, referer: str,
               sentinel_header: str | None = None, so_header: str | None = None):
    """统一发 /api/accounts/* 的 JSON POST。"""
    headers = session.get_auth_headers(referer=referer)
    if sentinel_header:
        headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
    return session.post(url, headers=headers, data=json.dumps(payload), allow_redirects=False)


def _resp_json(resp) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}


def _response_text(resp) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            parts = []
            def walk(x):
                if isinstance(x, dict):
                    for v in x.values(): walk(v)
                elif isinstance(x, list):
                    for v in x: walk(v)
                elif x is not None:
                    parts.append(str(x))
            walk(data)
            return " ".join(parts)
    except Exception:
        pass
    return str(getattr(resp, 'text', '') or '')


def _phone_failure_reason(text: str, status_code: int | None = None) -> str:
    low = str(text or '').lower()
    if 'whatsapp' in low or 'whats app' in low:
        return 'whatsapp_channel'
    if any(k in low for k in (
        'phone number is not valid', 'invalid phone number', 'invalid phone', 'not a valid phone',
        '号码无效', '手机号无效', '电话号码无效', 'invalid_number', 'invalid_phone',
    )):
        return 'invalid_phone'
    if any(k in low for k in (
        'cannot send', "can't send", 'could not send', "couldn't send", 'unable to send',
        'cannot deliver', 'unable to deliver', 'failed to send', 'send failed',
        '无法发送', '不能发送', '无法向', '发送验证码', '发送短信',
    )):
        return 'delivery_refused'
    if any(k in low for k in ('too many', 'rate limit', 'throttle', 'limited', '频繁', '限流')):
        return 'send_limited'
    if any(k in low for k in ('already used', 'used too many', 'maximum', '上限', '已被使用')):
        return 'phone_used_or_max'
    if status_code and status_code >= 500:
        return 'server_error'
    if status_code and status_code >= 400:
        return 'send_rejected'
    return ''


# ============================================================
# 步骤 0：用全新 session 跟随 Codex authorize URL，建立 auth.openai.com 会话
# ============================================================

def _bootstrap_authorize(
    session: BrowserSession,
    state: str,
    code_challenge: str | None = None,
    auth_url: str | None = None,
) -> None:
    """
    GET Codex authorize URL 并跟随重定向，落到登录页，建立 auth.openai.com cookies
    （含 oai-client-auth-session：内含 Codex 目标 + 后续要用的 workspace 列表）。
    """
    # 默认使用调用方传入的 CPA 授权地址；未传时才走保留的本地 PKCE 生成逻辑。
    if not auth_url:
        if not code_challenge:
            raise RuntimeError("[Codex] 本地生成授权地址需要 code_challenge")
        auth_url = _build_authorize_url(state, code_challenge, prompt="login")
    auth_url = _ensure_oai_context_url(auth_url, session)
    headers = session.get_auth_navigate_headers(referer="https://chatgpt.com/")
    logger.info("[Codex] 跟随 Codex authorize URL 建立会话...")
    logger.info(f"[Codex] 完整授权地址: {auth_url}")
    resp = _with_net_retry(
        "bootstrap authorize",
        lambda: session.get(auth_url, headers=headers, allow_redirects=True),
    )
    logger.debug(f"[Codex] authorize 落点: {getattr(resp, 'url', '')}, status={getattr(resp, 'status_code', '')}")


# ============================================================
# 步骤 1：提交邮箱（触发邮箱 OTP 发送）
# ============================================================

def _submit_email(session: BrowserSession, email: str) -> None:
    """POST authorize/continue 提交邮箱，触发 OpenAI 发送邮箱 OTP。带 sentinel。"""
    sentinel_resp = request_sentinel_token(session, "authorize_continue")
    sentinel_header, so_header = build_sentinel_header(session, sentinel_resp, "authorize_continue")
    payload = {"username": {"kind": "email", "value": email}}
    resp = _post_json(
        session,
        "https://auth.openai.com/api/accounts/authorize/continue",
        payload,
        referer="https://auth.openai.com/log-in",
        sentinel_header=sentinel_header,
        so_header=so_header,
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(
            f"[Codex] 提交邮箱失败 status={resp.status_code}: {(resp.text or '')[:300]}"
        )
    logger.info(f"[Codex] 已提交邮箱 {email}，等待邮箱 OTP")


# ============================================================
# 步骤 2：提交邮箱 OTP
# ============================================================

def _submit_email_otp(session: BrowserSession, code: str) -> None:
    """POST email-otp/validate 提交邮箱验证码。带 sentinel(authorize_continue)。"""
    sentinel_resp = request_sentinel_token(session, "authorize_continue")
    sentinel_header, so_header = build_sentinel_header(session, sentinel_resp, "authorize_continue")
    resp = _post_json(
        session,
        "https://auth.openai.com/api/accounts/email-otp/validate",
        {"code": code},
        referer="https://auth.openai.com/email-verification",
        sentinel_header=sentinel_header,
        so_header=so_header,
    )
    if resp.status_code != 200:
        error_code = _extract_error_code(resp)
        if error_code in ("account_deactivated", "account_deleted", "account_banned"):
            raise AccountUnusableError(
                f"[Codex] 账号已废（{error_code}）status={resp.status_code}: {(resp.text or '')[:200]}",
                error_code=error_code,
            )
        body_error_code = detect_account_unusable_response_body(resp.text or "")
        if body_error_code:
            raise AccountUnusableError(
                f"[Codex] 账号已废（{body_error_code}）status={resp.status_code}: {(resp.text or '')[:200]}",
                error_code=body_error_code,
            )
        raise RuntimeError(
            f"[Codex] 邮箱 OTP 验证失败 status={resp.status_code}: {(resp.text or '')[:300]}"
        )
    logger.info("[Codex] 邮箱 OTP 验证通过")


# ============================================================
# 步骤 3-4：手机号验证（接码，失败换号重试）
# ============================================================

def _sms_provider_name() -> str:
    """当前接码通道名，仅用于 Codex 流程日志。"""
    return str(getattr(_cfg, "SMS_PROVIDER", "grizzly") or "grizzly").strip().lower()


def _sleep_before_phone_retry(attempt: int, max_retries: int, *, prefix: str = "[Codex]") -> None:
    """换号前随机等待，至少 3 秒，避免连续提交号码过快。"""
    if attempt >= max_retries:
        return
    seconds = random.uniform(3.0, 8.0)
    logger.info(f"{prefix} 换号前随机等待 {seconds:.1f} 秒")
    time.sleep(seconds)


def _do_phone_verification(session: BrowserSession) -> None:
    """
    用接码平台拿号 → add-phone/send 发短信 → 收码 → phone-otp/validate。
    一个号收不到码或被 OpenAI 拒就取消换号，最多 SMS_MAX_RETRIES 次（热加载）。

    实际平台适配在 core.sms_provider：
        - SMS_PROVIDER="grizzly"：GrizzlySMS handler_api.php
        - SMS_PROVIDER="l"：L_API.md 的 /take-phone 和 /fetch-code JSON 接口
    """
    http = sms_provider._http()
    max_retries = _cfg.SMS_MAX_RETRIES
    provider = _sms_provider_name()
    try:
        last_err = None
        for attempt in range(1, max_retries + 1):
            activation_id = None
            try:
                activation_id, phone = sms_provider.acquire_number(http, attempt_index=attempt)
                logger.info(
                    f"[Codex] 手机验证尝试 {attempt}/{max_retries}，"
                    f"provider={provider}, activation_id={activation_id}, 号码=+{phone}"
                )

                # 发短信
                send_resp = _post_json(
                    session,
                    "https://auth.openai.com/api/accounts/add-phone/send",
                    {"phone_number": f"+{phone}", "channel": "sms"},
                    referer="https://auth.openai.com/add-phone",
                )
                send_text = _response_text(send_resp)
                send_reason = _phone_failure_reason(send_text, send_resp.status_code)
                if send_resp.status_code not in (200, 204) or send_reason:
                    # 号码无效 / 无法发送 / WhatsApp 通道 / 限流等 → 释放当前号并换号。
                    logger.warning(
                        f"[Codex] add-phone/send 未成功 reason={send_reason or 'unknown'}, "
                        f"status={send_resp.status_code}: {send_text[:240]}，换号重试"
                    )
                    sms_provider.cancel(activation_id, http)
                    _sleep_before_phone_retry(attempt, max_retries)
                    continue

                # 通知平台短信已发出（status=1）；BAD_STATUS 等失败不能中断等码
                sms_provider.mark_sms_sent(activation_id, http=http)

                # 定时轮询接码平台获取短信。wait_for_sms_code 内部按 SMS_POLL_INTERVAL 轮询，
                # 最长等待 SMS_CODE_WAIT；超时立即取消当前号并换号。
                try:
                    logger.info(
                        f"[Codex] 短信已发送，开始轮询验证码 activation_id={activation_id}, "
                        f"wait={_cfg.SMS_CODE_WAIT}s, interval={_cfg.SMS_POLL_INTERVAL}s"
                    )
                    sms_code = sms_provider.wait_for_sms_code(activation_id, http)
                except sms_provider.SmsCodeTimeout:
                    logger.warning(f"[Codex] 号码 +{phone} 在 {_cfg.SMS_CODE_WAIT}s 内未收到短信，取消换号")
                    sms_provider.cancel(activation_id, http)
                    _sleep_before_phone_retry(attempt, max_retries)
                    continue

                # 验手机码
                val_resp = _post_json(
                    session,
                    "https://auth.openai.com/api/accounts/phone-otp/validate",
                    {"code": sms_code},
                    referer="https://auth.openai.com/phone-verification",
                )
                if val_resp.status_code != 200:
                    val_text = _response_text(val_resp)
                    val_reason = _phone_failure_reason(val_text, val_resp.status_code) or 'code_rejected'
                    logger.warning(
                        f"[Codex] phone-otp/validate 失败 reason={val_reason}, status={val_resp.status_code}: "
                        f"{val_text[:240]}，换号重试"
                    )
                    sms_provider.cancel(activation_id, http)
                    _sleep_before_phone_retry(attempt, max_retries)
                    continue

                # 成功
                sms_provider.complete(activation_id, http)
                logger.info("[Codex] 手机号验证通过")
                return

            except sms_provider.SmsNoBalanceError:
                # 余额不足，重试无意义，直接抛
                raise
            except sms_provider.SmsProviderError as exc:
                last_err = exc
                logger.warning(f"[Codex] 接码尝试 {attempt} 失败：{exc}")
                if activation_id:
                    sms_provider.cancel(activation_id, http)
                _sleep_before_phone_retry(attempt, max_retries)
                continue

        raise RuntimeError(
            f"[Codex] 手机号验证重试 {max_retries} 次仍失败（provider={provider}）"
            + (f"，最后错误：{last_err}" if last_err else "")
        )
    finally:
        http.close()


# ============================================================
# 步骤 5：选 workspace → 拿 callback code
# ============================================================

def _get_workspace_id(session: BrowserSession) -> str:
    """
    从 oai-client-auth-session cookie 解出 workspaces[0].id。
    cookie 形如 base64payload.sig.sig，取第一段 base64 解码后的 JSON。
    """
    raw = None
    try:
        # curl_cffi cookies
        for c in session.session.cookies.jar:
            if c.name == "oai-client-auth-session":
                raw = c.value
                break
    except Exception:
        pass
    if not raw:
        # 退而求其次：从 cookies 字典拿
        try:
            raw = session.session.cookies.get("oai-client-auth-session")
        except Exception:
            raw = None
    if not raw:
        raise RuntimeError("[Codex] 找不到 oai-client-auth-session cookie，无法取 workspace_id")

    payload = _decode_jwt_segment(raw.split(".")[0])
    workspaces = payload.get("workspaces") or []
    if not workspaces:
        raise RuntimeError(f"[Codex] cookie 里无 workspaces 字段: keys={list(payload.keys())}")
    wid = workspaces[0].get("id")
    if not wid:
        raise RuntimeError(f"[Codex] workspaces[0] 无 id: {workspaces[0]}")
    logger.info(f"[Codex] workspace_id={wid}")
    return wid


def _select_workspace_and_get_callback(session: BrowserSession, state: str) -> str:
    """
    POST workspace/select，然后跟随后续重定向/响应里的 URL 直到命中 localhost:1455 callback。
    返回完整 callback URL（含 code）。
    """
    wid = _get_workspace_id(session)
    resp = _post_json(
        session,
        "https://auth.openai.com/api/accounts/workspace/select",
        {"workspace_id": wid},
        referer="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
    )

    # 1) 直接带 Location 头命中 callback
    loc = resp.headers.get("location") or resp.headers.get("Location")
    if loc and _is_redirect_uri(loc):
        return loc

    # 2) 响应 JSON 里给了下一步 URL（continue_url / redirect_url / url / next）
    data = _resp_json(resp)
    next_url = None
    for key in ("redirect_url", "continue_url", "url", "next", "location"):
        v = data.get(key)
        if isinstance(v, str) and v:
            next_url = v
            break

    # 3) 没给 URL 但有 Location（非 callback）→ 从 Location 起跟
    if not next_url and loc:
        next_url = loc

    if not next_url:
        raise RuntimeError(
            f"[Codex] workspace/select 后找不到下一跳 URL: status={resp.status_code}, "
            f"body={(resp.text or '')[:300]}"
        )

    # 跟随重定向链直到命中 callback
    return _follow_until_callback(session, next_url, state)


def _follow_until_callback(session: BrowserSession, url: str, state: str) -> str:
    """从给定 URL 起逐跳跟随，命中 localhost:1455 callback 时返回其 Location。"""
    if url.startswith("/"):
        url = "https://auth.openai.com" + url
    for hop in range(_MAX_REDIRECTS):
        if _is_redirect_uri(url):
            return url
        headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/")
        resp = session.get(url, headers=headers, allow_redirects=False)
        loc = resp.headers.get("location") or resp.headers.get("Location")
        logger.debug(f"[Codex] callback 跟随 hop {hop}: status={getattr(resp,'status_code','')}, location={loc}")
        if loc is None:
            raise RuntimeError(
                f"[Codex] 跟随中断，未命中 callback: url={url}, "
                f"status={getattr(resp,'status_code','')}, body={(resp.text or '')[:200]}"
            )
        if _is_redirect_uri(loc):
            return loc
        url = loc if loc.startswith("http") else ("https://auth.openai.com" + loc)
    raise RuntimeError(f"[Codex] 跟随 callback 超过 {_MAX_REDIRECTS} 跳")


# ============================================================
# 换 token（对照 CLIProxyAPI ExchangeCodeForTokensWithRedirect）—— 未改动
# ============================================================

def exchange_codex_token(session: BrowserSession, code: str, code_verifier: str) -> dict:
    """用 authorization code 换 token。"""
    data = {
        "grant_type": "authorization_code",
        "client_id": _cfg.CODEX_CLIENT_ID,
        "code": code,
        "redirect_uri": _cfg.CODEX_REDIRECT_URI,
        "code_verifier": code_verifier,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    base = session._get_common_headers()
    base.update(headers)
    headers = base

    logger.info("[Codex] 用 authorization code 换 token...")
    resp = session.post(_cfg.CODEX_TOKEN_URL, headers=headers, data=urlencode(data))
    http_status = resp.status_code
    if http_status != 200:
        raise RuntimeError(
            f"[Codex] 换 token 失败 status={http_status}: {(resp.text or '')[:300]}"
        )
    token_resp = resp.json()
    if not token_resp.get("access_token"):
        raise RuntimeError(f"[Codex] token 响应缺少 access_token: {token_resp}")
    logger.info(
        f"[Codex] 换 token 成功，expires_in={token_resp.get('expires_in')}, "
        f"access_token={token_resp['access_token'][:16]}..."
    )
    return token_resp


# ============================================================
# 解析 id_token / 落盘 —— 未改动
# ============================================================

def _parse_id_token(id_token: str) -> dict:
    """base64 解码 JWT payload（不验签），抽 email / account_id / plan_type。"""
    if not id_token:
        return {}
    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return {}
        claims = _decode_jwt_segment(parts[1])
    except Exception as exc:
        logger.warning(f"[Codex] id_token 解析失败: {exc}")
        return {}

    auth_claim = claims.get("https://api.openai.com/auth", {}) or {}
    profile_claim = claims.get("https://api.openai.com/profile", {}) or {}
    # OpenAI 新版 id_token 的 email 在顶层 claim；旧版/CLIProxyAPI 实现里在 profile_claim。
    # 顶层优先，否则回退 profile_claim，避免落盘的 codex-邮箱.json 里 email 字段为空。
    email_value = claims.get("email") or profile_claim.get("email", "")
    return {
        "email": email_value,
        "account_id": auth_claim.get("chatgpt_account_id", ""),
        "plan_type": auth_claim.get("chatgpt_plan_type", ""),
    }


def build_codex_storage(token_resp: dict, id_claims: dict) -> dict:
    """组装 CLIProxyAPI CodexTokenStorage JSON 结构（含导出 sub2 格式所需字段）。"""
    expires_in = token_resp.get("expires_in", 0) or 0
    expired_dt = datetime.now(timezone.utc) + _timedelta_seconds(expires_in)
    last_refresh_dt = datetime.now(timezone.utc)
    plan = str(id_claims.get("plan_type") or "").strip()
    email = str(id_claims.get("email") or "").strip()
    account_id = str(id_claims.get("account_id") or "").strip()
    return {
        "id_token": token_resp.get("id_token", ""),
        "access_token": token_resp.get("access_token", ""),
        "refresh_token": token_resp.get("refresh_token", ""),
        "account_id": account_id,
        "chatgpt_account_id": account_id,
        "client_id": str(getattr(_cfg, "CODEX_CLIENT_ID", "") or ""),
        "plan_type": plan,
        "chatgpt_plan_type": plan,
        "last_refresh": last_refresh_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "email": email,
        "type": "codex",
        "expired": expired_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expired_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _mask_proxy_for_log(proxy: str | None) -> str:
    """日志里隐藏代理密码，仅保留 scheme/user/host/port 与 sid 片段。"""
    text = "" if proxy is None else str(proxy).strip()
    if proxy is None:
        return "未指定(将从代理池重抽)"
    if not text:
        return "直连"
    try:
        parsed = urlparse(text)
        if not parsed.scheme and not parsed.hostname:
            return text[:48] + ("..." if len(text) > 48 else "")
        user = unquote(parsed.username) if parsed.username else ""
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        # 保留 sid 便于对照浏览器启动日志，不回显密码。
        if user:
            netloc = f"{user}:***@{host}{port}"
        else:
            netloc = f"{host}{port}"
        return urlunparse((parsed.scheme, netloc, "", "", "", ""))
    except Exception:
        return text[:48] + ("..." if len(text) > 48 else "")


def resolve_browser_proxy_for_token_exchange(
    proxy: str | None = None,
    opened=None,
) -> str | None:
    """换 token 时尽量复用浏览器实际代理，避免 BrowserSession(proxy=None) 另抽一条 SID。

    语义与 BrowserSession 对齐：
      - 非空字符串：使用该代理
      - ""：显式直连
      - None：无浏览器代理信息时的兜底，由 BrowserSession 自行从池抽取
    """
    if proxy is not None:
        return proxy

    raw = getattr(opened, "raw", None)
    if not isinstance(raw, dict):
        return None

    # Cloak: raw["proxy"]；其它驱动可能写 proxy_url / proxy_used
    for key in ("proxy", "proxy_url", "proxyUrl", "proxy_used"):
        if key not in raw:
            continue
        val = raw.get(key)
        if val is None:
            return ""
        text = str(val).strip()
        return text if text else ""
    return None


def complete_local_codex_oauth(
    *,
    email: str,
    code: str,
    code_verifier: str,
    callback_url: str = "",
    proxy: str | None = None,
) -> dict:
    """
    local 模式收尾：用本程序 PKCE verifier 换 token，并把完整凭据落盘。

    不依赖 CPA / sub2api admin key。返回 {email, path, storage, plan, callback_url}。
    """
    if not code_verifier:
        raise RuntimeError("[Codex] local 模式缺少 code_verifier，无法换 token")
    if not code:
        raise RuntimeError("[Codex] local 模式缺少 authorization code")

    def _close_session(sess) -> None:
        if not sess:
            return
        try:
            closer = getattr(sess, "close", None)
            if callable(closer):
                closer()
        except Exception:
            pass

    logger.info("[Codex] 换 token 使用代理：%s", _mask_proxy_for_log(proxy))
    session = None
    try:
        # 短请求：有显式代理时仍探测出口，便于日志对照；失败路径会降级。
        session = BrowserSession(proxy=proxy, detect_exit_geo=bool(proxy))
        try:
            token_resp = exchange_codex_token(session, code, code_verifier)
        except Exception as exc:
            # 浏览器能走通 1024proxy socks5，但 curl_cffi 偶发 TLS 失败。
            # OAuth code 换 token 不绑定浏览器出口 IP，代理 SSL 失败时改直连兜底。
            if proxy not in (None, "") and _is_transient_network_error(exc):
                logger.warning(
                    "[Codex] 经代理换 token 失败，改直连重试：%s: %s",
                    type(exc).__name__,
                    str(exc)[:180],
                )
                _close_session(session)
                session = BrowserSession(proxy="", detect_exit_geo=False)
                logger.info("[Codex] 换 token 使用代理：直连(SSL 兜底)")
                token_resp = exchange_codex_token(session, code, code_verifier)
            else:
                raise
    finally:
        _close_session(session)

    id_claims = _parse_id_token(token_resp.get("id_token", ""))
    effective_email = str(id_claims.get("email") or email or "").strip() or email
    storage = build_codex_storage(token_resp, id_claims)
    # 确保邮箱写入 storage，便于后续导出
    if not storage.get("email"):
        storage["email"] = effective_email
    plan = str(id_claims.get("plan_type") or storage.get("plan_type") or "").strip()
    path = save_codex_credential(storage, effective_email, plan)

    has_rt = bool(storage.get("refresh_token"))
    has_at = bool(storage.get("access_token"))
    logger.info(
        "[Codex][local] 凭据已保存：email=%s plan=%s path=%s has_rt=%s has_at=%s",
        effective_email, plan or "-", path, has_rt, has_at,
    )
    if not has_rt:
        logger.warning("[Codex][local] 警告：token 响应缺少 refresh_token，后续导出/续期可能不可用")

    return {
        "email": effective_email,
        "path": path,
        "storage": storage,
        "plan": plan,
        "callback_url": callback_url,
        "account_id": storage.get("account_id") or "",
    }


def _timedelta_seconds(seconds: int):
    from datetime import timedelta
    return timedelta(seconds=int(seconds))


def _credential_file_name(email: str, plan_type: str) -> str:
    """对照 CLIProxyAPI filename.go：无 plan→codex-{email}.json，否则带 plan 后缀。"""
    email = (email or "").strip()
    plan = (plan_type or "").strip().lower()
    if plan == "":
        return f"codex-{email}.json"
    return f"codex-{email}-{plan}.json"


def save_codex_credential(storage: dict, email: str, plan_type: str) -> Path:
    """落盘到 {PROJECT_ROOT}/{CODEX_OUTPUT_DIRNAME}/codex-{email}.json。"""
    out_dir = _PROJECT_ROOT / _cfg.CODEX_OUTPUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = _credential_file_name(email, plan_type)
    path = out_dir / fname
    path.write_text(
        json.dumps(storage, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _extract_cpa_auth_json(payload: dict) -> dict | None:
    """
    尝试从 CPA oauth-callback 响应里提取完整授权文件。
    不同 CPA 版本字段名可能不同；只要看起来是 codex auth json 就落本地。
    """
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("auth_json"),
        payload.get("authJson"),
        payload.get("auth"),
        payload.get("auth_file"),
        payload.get("authFile"),
        payload.get("file"),
        payload.get("data"),
    ]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates.extend([
        data.get("auth_json"),
        data.get("authJson"),
        data.get("auth"),
        data.get("auth_file"),
        data.get("authFile"),
        data.get("file"),
    ])
    for item in candidates:
        if isinstance(item, dict) and (
            item.get("type") == "codex"
            or item.get("access_token")
            or item.get("refresh_token")
            or item.get("id_token")
        ):
            return item
    return None


def _save_cpa_local_record(
    *,
    email: str,
    callback_url: str,
    auth_url: str,
    state: str,
    submit_payload: dict,
) -> Path | None:
    """
    本地记录 CPA 授权结果：
      1) 如果 CPA 返回完整 auth json，保存为可用 codex-邮箱[-plan].json；
      2) 否则按配置保存 callback 提交回执，便于追踪 CPA 侧授权文件。
    """
    auth_json = _extract_cpa_auth_json(submit_payload)
    if auth_json:
        effective_email = auth_json.get("email") or email
        plan = auth_json.get("plan_type") or auth_json.get("chatgpt_plan_type") or ""
        return save_codex_credential(auth_json, effective_email, plan)

    if not bool(getattr(_cfg, "CPA_SAVE_CALLBACK_RECEIPT", True)):
        return None

    out_dir = _PROJECT_ROOT / _cfg.CODEX_OUTPUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_email = (email or "unknown").strip().replace("/", "_").replace("\\", "_")
    path = out_dir / f"codex-{safe_email}-cpa-callback.json"
    record = {
        "type": "codex_cpa_callback",
        "email": email,
        "state": state,
        "auth_url": auth_url,
        "callback_url": callback_url,
        "cpa_management_origin": _cpa_management_origin(),
        "cpa_submit_response": submit_payload,
        "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "授权地址由 CPA 生成；callback 已提交给 CPA。若 CPA 响应未包含 token，本文件为本地回执记录。",
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _save_sub2_local_record(
    *,
    email: str,
    callback_url: str,
    auth_url: str,
    state: str,
    submit_payload: dict,
) -> Path | None:
    """本地记录 sub2 授权结果；若 sub2 返回完整 auth json，则保存为可用 codex 凭证。"""
    auth_json = _extract_cpa_auth_json(submit_payload)
    if auth_json:
        effective_email = auth_json.get("email") or email
        plan = auth_json.get("plan_type") or auth_json.get("chatgpt_plan_type") or ""
        return save_codex_credential(auth_json, effective_email, plan)

    if not bool(getattr(_cfg, "CPA_SAVE_CALLBACK_RECEIPT", True)):
        return None

    out_dir = _PROJECT_ROOT / _cfg.CODEX_OUTPUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_email = (email or "unknown").strip().replace("/", "_").replace("\\", "_")
    path = out_dir / f"codex-{safe_email}-sub2-callback.json"
    try:
        sub2_origin = _sub2_codex_base()
    except Exception:
        sub2_origin = ""
    # create-from-oauth 的 data 里通常有 account id，回执里一并抽出方便后续导出
    submit_data = submit_payload.get("data") if isinstance(submit_payload, dict) and isinstance(submit_payload.get("data"), dict) else {}
    account_id = submit_data.get("id") if isinstance(submit_data, dict) else None
    record = {
        "type": "codex_sub2_callback",
        "email": email,
        "state": state,
        "auth_url": auth_url,
        "callback_url": callback_url,
        "sub2_origin": sub2_origin,
        "sub2_account_id": account_id,
        "sub2_submit_response": submit_payload,
        "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "授权地址由 sub2 生成；callback 已上传给 sub2。若 sub2 响应未包含 token，本文件为本地回执记录。下载 sub2api 格式时会按 account_id/email 从 sub2api 导出完整 credentials。",
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def extract_sub2_account_ref(local: dict | None) -> dict:
    """从本地 codex 记录解析 sub2api 账号引用：{id, email, plan}。"""
    local = local if isinstance(local, dict) else {}
    email = str(local.get("email") or "").strip()
    plan = str(local.get("plan") or local.get("plan_type") or local.get("chatgpt_plan_type") or "").strip()
    account_id = local.get("sub2_account_id") or local.get("account_id") or local.get("id")

    submit = local.get("sub2_submit_response") if isinstance(local.get("sub2_submit_response"), dict) else {}
    data = submit.get("data") if isinstance(submit.get("data"), dict) else {}
    if account_id in (None, "", 0, "0"):
        account_id = data.get("id")
    if not email:
        email = str(data.get("name") or "").strip()
    creds = data.get("credentials") if isinstance(data.get("credentials"), dict) else {}
    if not email:
        email = str(creds.get("email") or "").strip()
    if not plan:
        plan = str(creds.get("plan_type") or "").strip()
    if not email:
        email = str(local.get("email") or "").strip()

    try:
        account_id_i = int(account_id) if account_id not in (None, "") else None
    except (TypeError, ValueError):
        account_id_i = None
    return {"id": account_id_i, "email": email, "plan": plan}


def _looks_like_token_credentials(creds: dict | None) -> bool:
    if not isinstance(creds, dict):
        return False
    return bool(
        creds.get("refresh_token")
        or creds.get("access_token")
        or creds.get("id_token")
        or creds.get("rt")
        or creds.get("at")
    )


def _normalize_openai_oauth_credentials(raw: dict | None, *, email: str = "", plan: str = "") -> dict:
    """把本地 CPA/Codex/sub2 回执里的字段整理成 sub2api oauth credentials。"""
    src = dict(raw or {}) if isinstance(raw, dict) else {}
    # 兼容嵌套
    nested = src.get("credentials") if isinstance(src.get("credentials"), dict) else {}
    if nested:
        merged = dict(nested)
        for k, v in src.items():
            if k != "credentials" and k not in merged:
                merged[k] = v
        src = merged

    email = str(
        email
        or src.get("email")
        or src.get("name")
        or ""
    ).strip()
    plan = str(
        plan
        or src.get("plan_type")
        or src.get("chatgpt_plan_type")
        or src.get("plan")
        or ""
    ).strip()

    access_token = str(src.get("access_token") or src.get("at") or "").strip()
    refresh_token = str(src.get("refresh_token") or src.get("rt") or "").strip()
    id_token = str(src.get("id_token") or "").strip()
    client_id = str(
        src.get("client_id")
        or getattr(_cfg, "CODEX_CLIENT_ID", "")
        or "app_EMoamEEZ73f0CkXaXp7hrann"
    ).strip()

    account_id = str(
        src.get("chatgpt_account_id")
        or src.get("account_id")
        or src.get("acc_id")
        or ""
    ).strip()
    user_id = str(
        src.get("chatgpt_user_id")
        or src.get("user_id")
        or ""
    ).strip()
    org_id = str(src.get("organization_id") or src.get("org_id") or "").strip()

    expires_at = src.get("expires_at") or src.get("expired") or src.get("expire_at") or ""
    if isinstance(expires_at, (int, float)) and expires_at > 0:
        # unix 秒
        try:
            expires_at = datetime.fromtimestamp(float(expires_at), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            expires_at = str(expires_at)
    else:
        expires_at = str(expires_at or "").strip()

    creds: dict = {}
    if access_token:
        creds["access_token"] = access_token
    if refresh_token:
        creds["refresh_token"] = refresh_token
    if id_token:
        creds["id_token"] = id_token
    if client_id:
        creds["client_id"] = client_id
    if email:
        creds["email"] = email
    if plan:
        creds["plan_type"] = plan
    if account_id:
        creds["chatgpt_account_id"] = account_id
        creds["account_id"] = account_id
    if user_id:
        creds["chatgpt_user_id"] = user_id
    if org_id:
        creds["organization_id"] = org_id
    if expires_at:
        creds["expires_at"] = expires_at
    sub_exp = src.get("subscription_expires_at")
    if sub_exp not in (None, ""):
        creds["subscription_expires_at"] = sub_exp

    # 保留其它非空扩展字段（不覆盖已规范化键）
    skip = {
        "access_token", "refresh_token", "id_token", "at", "rt", "client_id", "email", "name",
        "plan_type", "chatgpt_plan_type", "plan", "chatgpt_account_id", "account_id", "acc_id",
        "chatgpt_user_id", "user_id", "organization_id", "org_id", "expires_at", "expired",
        "expire_at", "subscription_expires_at", "type", "credentials",
    }
    for k, v in src.items():
        if k in skip or v in (None, "", [], {}):
            continue
        if k not in creds:
            creds[k] = v
    return creds


def local_record_to_sub2api_account(
    local: dict | None,
    *,
    local_filename: str = "",
) -> tuple[dict, dict]:
    """
    离线把本地 codex 记录转成 sub2api accounts[] 单条。

    不依赖 sub2api 号池是否还在；优先用本地已有 token/回执字段。
    """
    local = local if isinstance(local, dict) else {}
    ref = extract_sub2_account_ref(local)
    email = str(ref.get("email") or "").strip()
    plan = str(ref.get("plan") or "").strip()
    source = "local"

    account: dict | None = None
    creds: dict = {}

    # 1) 已是 sub2 回执：直接用 create-from-oauth 返回的 data 骨架
    if local.get("type") == "codex_sub2_callback" or str(local_filename).endswith("-sub2-callback.json"):
        submit = local.get("sub2_submit_response") if isinstance(local.get("sub2_submit_response"), dict) else {}
        data = submit.get("data") if isinstance(submit.get("data"), dict) else {}
        if data:
            account = {
                "name": str(data.get("name") or email or "OpenAI OAuth Account").strip(),
                "notes": data.get("notes"),
                "platform": str(data.get("platform") or "openai"),
                "type": str(data.get("type") or "oauth"),
                "credentials": _normalize_openai_oauth_credentials(
                    data.get("credentials") if isinstance(data.get("credentials"), dict) else data,
                    email=email,
                    plan=plan,
                ),
                "extra": data.get("extra") if isinstance(data.get("extra"), dict) else {},
                "concurrency": int(data.get("concurrency") or 3),
                "priority": int(data.get("priority") or 50),
                "rate_multiplier": float(data.get("rate_multiplier") or 1),
                "auto_pause_on_expired": bool(
                    True if data.get("auto_pause_on_expired") is None else data.get("auto_pause_on_expired")
                ),
            }
            source = "local_sub2_callback"
            creds = account["credentials"]

    # 2) 本地已是完整 codex/CPA 凭证（含 token）
    if account is None or not _looks_like_token_credentials((account or {}).get("credentials")):
        if _looks_like_token_credentials(local) or local.get("type") in ("codex", "openai", "oauth"):
            creds = _normalize_openai_oauth_credentials(local, email=email, plan=plan)
            if _looks_like_token_credentials(creds) or email:
                account = {
                    "name": email or str(local.get("name") or "OpenAI OAuth Account"),
                    "platform": "openai",
                    "type": "oauth",
                    "credentials": creds,
                    "extra": {
                        "email": email,
                        "source": "local_codex_credential",
                        "local_filename": local_filename or "",
                    },
                    "concurrency": 3,
                    "priority": 50,
                    "rate_multiplier": 1,
                    "auto_pause_on_expired": True,
                }
                source = "local_codex_credential"

    # 3) 已经是 sub2api account 条目
    if account is None and str(local.get("platform") or "").lower() == "openai" and isinstance(local.get("credentials"), dict):
        account = {
            "name": str(local.get("name") or email or "OpenAI OAuth Account"),
            "notes": local.get("notes"),
            "platform": "openai",
            "type": str(local.get("type") or "oauth"),
            "credentials": _normalize_openai_oauth_credentials(local.get("credentials"), email=email, plan=plan),
            "extra": local.get("extra") if isinstance(local.get("extra"), dict) else {},
            "concurrency": int(local.get("concurrency") or 3),
            "priority": int(local.get("priority") or 50),
            "rate_multiplier": float(local.get("rate_multiplier") or 1),
            "auto_pause_on_expired": bool(
                True if local.get("auto_pause_on_expired") is None else local.get("auto_pause_on_expired")
            ),
        }
        if local.get("proxy_key"):
            account["proxy_key"] = local.get("proxy_key")
        source = "local_sub2_account"
        creds = account["credentials"]

    if account is None:
        raise RuntimeError(
            f"本地记录无法转换为 sub2api 账号格式：filename={local_filename or '-'} email={email or '-'}"
        )

    # 清理 notes=null 之类可选字段
    if account.get("notes") in (None, ""):
        account.pop("notes", None)

    email = str((account.get("credentials") or {}).get("email") or account.get("name") or email or "").strip()
    plan = str((account.get("credentials") or {}).get("plan_type") or plan or "").strip()
    meta = {
        "email": email,
        "plan": plan,
        "local_filename": local_filename,
        "source": source,
        "sub2_account_id": ref.get("id"),
        "has_refresh_token": bool((account.get("credentials") or {}).get("refresh_token")),
        "has_access_token": bool((account.get("credentials") or {}).get("access_token")),
        "has_id_token": bool((account.get("credentials") or {}).get("id_token")),
    }
    return account, meta


def build_sub2api_export_payload(accounts: list[dict]) -> dict:
    """组装 sub2api accounts/data 导入包。"""
    clean_accounts = [a for a in (accounts or []) if isinstance(a, dict)]
    return {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": [],
        "accounts": clean_accounts,
    }


def download_sub2api_export_for_local(
    local: dict,
    *,
    local_filename: str = "",
    include_proxies: bool = False,  # 兼容旧参数；离线导出不依赖代理
) -> tuple[dict, dict]:
    """
    把本地 codex 记录转成 sub2api 导出包。

    纯离线转换，不依赖 sub2api 号池是否仍存在该账号。
    """
    _ = include_proxies
    account, meta = local_record_to_sub2api_account(local, local_filename=local_filename)
    payload = build_sub2api_export_payload([account])
    meta["account_count"] = 1
    return payload, meta


def download_sub2api_export_bulk(
    local_items: list[tuple[str, dict]],
    *,
    include_proxies: bool = False,
) -> tuple[dict, list[dict], list[dict]]:
    """
    批量把本地记录转成一份 sub2api accounts/data JSON。

    纯离线，不请求 sub2api 是否还存着这些号。
    """
    _ = include_proxies
    accounts: list[dict] = []
    added: list[dict] = []
    errors: list[dict] = []
    seen_emails: set[str] = set()

    for fname, local in local_items:
        try:
            account, meta = local_record_to_sub2api_account(local, local_filename=fname)
            email = str(meta.get("email") or "").strip().lower()
            # 同邮箱去重，保留后出现的（通常更新）
            if email and email in seen_emails:
                accounts = [
                    a for a in accounts
                    if str((a.get("credentials") or {}).get("email") or a.get("name") or "").strip().lower() != email
                ]
                added = [x for x in added if str(x.get("email") or "").strip().lower() != email]
            if email:
                seen_emails.add(email)
            accounts.append(account)
            added.append({
                "email": meta.get("email") or "",
                "local_filenames": [fname],
                "name": account.get("name") or meta.get("email") or "",
                "plan": meta.get("plan") or "",
                "source": meta.get("source") or "local",
                "has_refresh_token": bool(meta.get("has_refresh_token")),
                "has_access_token": bool(meta.get("has_access_token")),
                "has_id_token": bool(meta.get("has_id_token")),
            })
        except Exception as exc:
            errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})

    payload = build_sub2api_export_payload(accounts)
    return payload, added, errors


# ============================================================
# 入口
# ============================================================

def run_codex_oauth(
    email: str,
    otp_provider=None,
    proxy: str | None = None,
    force: bool = False,
    _cpa_reauth_round: int = 1,
) -> dict:
    """
    注册成功后的 Codex OAuth 授权入口（全新 session + 接码方案）。

    不复用注册的 session：内部新建干净 BrowserSession，从头登录该邮箱，
    走 邮箱 OTP → 手机短信验证 → 选 workspace → 拿 code → 换 token → 落盘。

    Args:
        email: 已注册成功的账号邮箱
        otp_provider: 邮箱 OTP 获取回调 fn(email, after_ts)->code，默认用 wait_for_otp
        proxy: 代理（不传从 PROXY_POOL 抽）
        force: True 时跳过 ENABLE_CODEX_AUTO 开关限制，供手动补跑使用

    Returns:
        结构化结果 dict。任何异常都被吞掉转 status=failed，不向上抛，不影响注册主流程。
    """
    if not force and not _cfg.ENABLE_CODEX_AUTO:
        return _codex_result(status="skipped", message="ENABLE_CODEX_AUTO=False")
    if not email:
        return _codex_result(status="skipped", message="email 为空")

    # Codex OAuth 支持多种驱动：
    # protocol：原纯协议；roxy/cloak/browser_use：用真实浏览器跑页面并捕获 localhost callback。
    try:
        from config import codex as _codex_cfg
        from config import roxybrowser as _roxy_cfg
        oauth_driver = str(getattr(_codex_cfg, "CODEX_OAUTH_DRIVER", "protocol") or "protocol").strip().lower()
        if oauth_driver == "same_as_registration":
            oauth_driver = str(getattr(_roxy_cfg, "REGISTRATION_DRIVER", "protocol") or "protocol").strip().lower()
        if oauth_driver in ("roxy", "roxybrowser", "fingerprint", "browser"):
            from core.roxy_codex_oauth import run_roxy_codex_oauth
            return run_roxy_codex_oauth(email, otp_provider=otp_provider, proxy=proxy, force=True)
        if oauth_driver in ("browser_use", "browseruse", "browser-use", "bu"):
            from core.browser_use_codex_oauth import run_browser_use_codex_oauth
            return run_browser_use_codex_oauth(email, otp_provider=otp_provider, proxy=proxy, force=True)
        if oauth_driver in ("skyvern", "sv"):
            from core.skyvern_codex_oauth import run_skyvern_codex_oauth
            return run_skyvern_codex_oauth(email, otp_provider=otp_provider, proxy=proxy, force=True)
        if oauth_driver in ("cloak", "cloakbrowser"):
            from config import cloakbrowser as _cloak_cfg
            from core.cloakbrowser_driver import build_cloak_driver
            from core.roxy_codex_oauth import run_roxy_codex_oauth
            driver, opened = build_cloak_driver(proxy=proxy)
            # build_cloak_driver 在 proxy=None 时会自己抽代理；这里强制把实际代理回传，
            # 避免后续 complete_local_codex_oauth 再抽一条不同 SID。
            browser_proxy = resolve_browser_proxy_for_token_exchange(proxy=proxy, opened=opened)
            logger.info(
                "[Codex][Cloak] 浏览器实际代理将用于换 token：%s",
                _mask_proxy_for_log(browser_proxy),
            )
            try:
                return run_roxy_codex_oauth(
                    email,
                    otp_provider=otp_provider,
                    proxy=browser_proxy,
                    force=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    reuse_existing_profile=True,
                    clear_existing_state=True,
                )
            finally:
                if not bool(getattr(_cloak_cfg, "CLOAK_KEEP_BROWSER_OPEN", False)):
                    try:
                        driver.quit()
                    except Exception:
                        pass
        if oauth_driver not in ("protocol", "api", "http"):
            raise RuntimeError(f"[Codex] 不支持的 CODEX_OAUTH_DRIVER={oauth_driver!r}，可选 protocol / roxy / cloak / browser_use / skyvern")
    except ImportError:
        # 没装 selenium / 未提供 roxy 配置时继续走协议模式，保持旧行为。
        pass

    if otp_provider is None:
        from core.email_provider import wait_for_otp as otp_provider

    session = BrowserSession(proxy=proxy)
    try:
        logger.info(f"[Codex] 开始授权（全新 session）：{email}")

        # 1. 授权地址
        #    默认 local：本程序 PKCE + 换 token + 落盘，不依赖 CPA/sub2 admin key。
        auth_source = _codex_auth_url_source()
        cpa_auth = None
        sub2_auth = None
        code_verifier = None
        code_challenge = None
        auth_url = None
        if auth_source == "cpa":
            cpa_auth = _request_cpa_authorize_url()
            state = cpa_auth["state"]
            auth_url = cpa_auth["auth_url"]
            logger.info(f"[Codex] 当前使用 CPA 授权地址: {auth_url}")
        elif auth_source == "sub2":
            sub2_auth = _request_sub2_authorize_url()
            state = sub2_auth["state"]
            auth_url = sub2_auth["auth_url"]
            logger.info(f"[Codex] 当前使用 sub2api 授权地址: {auth_url}")
        elif auth_source == "local":
            code_verifier, code_challenge = _generate_pkce()
            state = _generate_state()
            auth_url = _build_authorize_url(state, code_challenge, prompt="login")
            logger.info("[Codex] 当前使用本地 PKCE 授权（自行换 token 并保存凭据）: %s", auth_url)
        else:
            raise RuntimeError(f"[Codex] 不支持的 CODEX_AUTH_URL_SOURCE={auth_source!r}")

        # 2. 网络预检 + 建立会话。预检不携带邮箱，不触发 OTP；
        #    真正烧邮箱的 authorize/continue 只在预检成功后执行。
        network_preflight(session)
        human_delay("navigate")

        _bootstrap_authorize(session, state, code_challenge, auth_url=auth_url)
        human_delay("navigate")

        # 3. 提交邮箱（触发邮箱 OTP）
        otp_after_ts = time.time()
        _submit_email(session, email)
        human_delay("form")

        # 4. 收邮箱 OTP + 提交；若一直未收到，协议模式下重新提交邮箱触发重发。
        email_otp = None
        max_email_otp_attempts = 3
        for email_otp_attempt in range(1, max_email_otp_attempts + 1):
            logger.info(f"[Codex] 等待邮箱 OTP：{email}（第 {email_otp_attempt}/{max_email_otp_attempts} 次）")
            try:
                email_otp = otp_provider(email, after_ts=otp_after_ts)
                break
            except Exception as exc:
                if email_otp_attempt >= max_email_otp_attempts:
                    raise
                logger.warning(
                    "[Codex] 一直未收到邮箱 OTP，重新提交邮箱触发重发后继续等待（下一轮 %s/%s）：%s: %s",
                    email_otp_attempt + 1,
                    max_email_otp_attempts,
                    type(exc).__name__,
                    str(exc)[:180],
                )
                otp_after_ts = time.time()
                _submit_email(session, email)
                human_delay("api")
        logger.info(f"[Codex] 邮箱 OTP 收到：{email_otp}")
        human_delay("otp_input")
        _submit_email_otp(session, email_otp)
        human_delay("api")

        # 5. 手机号验证（接码，自动重试换号）
        _do_phone_verification(session)
        human_delay("post_auth")

        # 6. 选 workspace → 拿 callback code
        callback_url = _select_workspace_and_get_callback(session, state)
        code = _extract_code(callback_url, state)
        logger.info(f"[Codex] 已拿到 authorization code：{code[:24]}...")

        # 7A. CPA 模式：把 callback URL 交给 CPA，由 CPA 持有 verifier 并完成换 token / 写 auth。
        #     本地不再用 code 换 token；仅保存 CPA 返回的授权文件或回调回执。
        if auth_source == "cpa":
            submit_payload = _submit_cpa_callback(callback_url)
            path = _save_cpa_local_record(
                email=email,
                callback_url=callback_url,
                auth_url=auth_url or "",
                state=state,
                submit_payload=submit_payload,
            )
            msg = submit_payload.get("message") or submit_payload.get("status_message") or "CPA callback submitted"
            logger.info(f"[Codex][CPA] 成功：{email}，{msg}，本地记录={path or 'disabled'}")
            return _codex_result(
                status="success",
                ok=True,
                email=email,
                file_path=str(path) if path else None,
                callback_url=callback_url,
                message=str(msg),
            )

        # 7A-sub2. sub2api 模式：把 callback 提交给 create-from-oauth 创建账号。
        if auth_source == "sub2":
            submit_payload = _submit_sub2_callback(
                callback_url,
                session_id=(sub2_auth or {}).get("session_id", ""),
                redirect_uri=(parse_qs(urlparse(auth_url or "").query).get("redirect_uri") or [""])[0],
                name=email,
            )
            path = _save_sub2_local_record(
                email=email,
                callback_url=callback_url,
                auth_url=auth_url or "",
                state=state,
                submit_payload=submit_payload,
            )
            msg = submit_payload.get("message") or submit_payload.get("status_message") or "sub2 callback uploaded"
            logger.info(f"[Codex][sub2] 成功：{email}，{msg}，本地记录={path or 'disabled'}")
            return _codex_result(
                status="success",
                ok=True,
                email=email,
                file_path=str(path) if path else None,
                callback_url=callback_url,
                message=str(msg),
            )

        # 7B. local 模式：本程序用 verifier 换 token，保存完整凭据供后续导出。
        done = complete_local_codex_oauth(
            email=email,
            code=code,
            code_verifier=code_verifier or "",
            callback_url=callback_url,
            proxy=proxy,
        )
        logger.info(
            "[Codex] 成功：%s，plan=%s, account_id=%s, 已保存到 %s",
            done.get("email"),
            done.get("plan") or "unknown",
            done.get("account_id") or "unknown",
            done.get("path"),
        )
        return _codex_result(
            status="success",
            ok=True,
            email=done.get("email") or email,
            file_path=str(done.get("path") or ""),
            callback_url=callback_url,
            message=f"local plan={done.get('plan') or 'unknown'}",
        )
    except AccountUnusableError as exc:
        logger.warning(f"[Codex] 账号已废（{exc.error_code}）：{email}")
        return _codex_result(
            status="deactivated",
            email=email,
            message=f"账号已废（{exc.error_code}）",
        )
    except Exception as exc:
        if _is_cpa_callback_reauth_error(exc) and _cpa_reauth_round < 2:
            logger.warning(
                "[Codex][CPA] callback 返回 Timeout waiting for OAuth callback，重新开启第 %s/2 轮 Codex 授权：%s",
                _cpa_reauth_round + 1, email,
            )
            return run_codex_oauth(
                email,
                otp_provider=otp_provider,
                proxy=proxy,
                force=force,
                _cpa_reauth_round=_cpa_reauth_round + 1,
            )
        logger.warning(f"[Codex] 失败：{email}，{type(exc).__name__}: {str(exc)[:200]}")
        logger.debug("[Codex] 失败详情:", exc_info=True)
        return _codex_result(
            status="failed",
            email=email,
            message=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
