# -*- coding: utf-8 -*-
"""已推送号池账号状态监控。

- 用 sub2api admin API 拉取号池 openai/oauth 账号
- 仅处理本地能匹配到的「我们的号」；外渠道 ignore
- 识别掉 RT / 需重授权；废号本地标记后跳过
- 可选：对可救号 Codex 补跑后回写号池凭据
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests

from config import sub2api as _cfg
from core import db
from core.sub2api_pool_push import (
    _api_base,
    _auth_headers,
    build_pool_account_from_codex_json,
)

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_LAST_SCAN: dict[str, Any] | None = None
_REPAIR_RUNNING: set[str] = set()  # email lower
_REPAIR_JOB: dict[str, Any] | None = None
_REPAIR_JOB_SEQ = 0

# 掉 RT / 凭据失效关键词（仅匹配 error_message / status 文本，勿扫 credentials_status JSON）
# 注意：不要用 "refresh_token"/"oauth" 等宽词，否则 has_refresh_token:true 会被误判。
_RT_BAD_HINTS = (
    "invalid_grant",
    "invalid_token",
    "token expired",
    "token_expired",
    "expired token",
    "access token expired",
    "refresh token expired",
    "refresh token invalid",
    "refresh token revoked",
    "no refresh token",
    "missing refresh token",
    "has_refresh_token\":false",
    "has_refresh_token: false",
    "rt invalid",
    "rt expired",
    "rt revoked",
    "revoked",
    "unauthorized",
    "unauthenticated",
    "reauth",
    "re-auth",
    "need reauth",
    "need login",
    "login required",
    "credentials invalid",
    "credential invalid",
    "credentials expired",
    "failed to refresh",
    "unable to refresh",
    "refresh failed",
)

_DEAD_HINTS = (
    "account_deactivated",
    "account_deleted",
    "account_banned",
    "deactivated",
    "banned",
    "disabled permanently",
    "user_deactivated",
    "账号已废",
    "已删除",
    "已封禁",
)

# 仅当「本机有记录」时算本站。号池侧 source（codex-register / sub_bundle_input 等）
# 不能单独当本站：那些号往往只有 OAuth、没有本机密码/接码素材，补跑必然失败。
# 可选：本机已无记录但 extra.import_source 明确是本工具推送标记时，仍标为本站（仅展示/标废，补跑仍看素材）。
_OUR_PUSH_IMPORT_SOURCES = (
    "codex_pool_push",
    "codex_pool_upload",
    "turb-gpt",
    "turb_gpt",
    "json_folder",
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _pool_group_id(explicit: int | None = None) -> int | None:
    if explicit is not None:
        try:
            return int(explicit)
        except Exception:
            return None
    try:
        return int(getattr(_cfg, "SUB2API_POOL_GROUP_ID", 0) or 0) or None
    except Exception:
        return None


def _extract_list_payload(body: Any) -> tuple[list[dict], int]:
    """兼容 {code,data:{items,total}} / {data:[...]} / {items:[...]}。"""
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)], len(body)
    if not isinstance(body, dict):
        return [], 0
    data = body.get("data") if "data" in body else body
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)], len(data)
    if not isinstance(data, dict):
        return [], 0
    items = data.get("items") or data.get("accounts") or data.get("list") or data.get("rows") or []
    if not isinstance(items, list):
        items = []
    total = data.get("total") or data.get("count") or len(items)
    try:
        total_i = int(total)
    except Exception:
        total_i = len(items)
    return [x for x in items if isinstance(x, dict)], total_i


def fetch_pool_accounts(
    *,
    group_id: int | None = None,
    platform: str = "openai",
    account_type: str = "oauth",
    page_size: int = 100,
    max_pages: int = 50,
    status: str | None = None,
) -> list[dict]:
    """分页拉取号池账号列表。"""
    base = _api_base()
    if not base:
        raise RuntimeError("未配置 SUB2API_API_BASE")
    if not _auth_headers().get("x-api-key") and "Authorization" not in _auth_headers() and len(_auth_headers()) <= 1:
        # 仍可能用自定义 header；只要有 base 就试
        key_hdr = str(getattr(_cfg, "SUB2API_API_AUTH_HEADER", "x-api-key") or "x-api-key")
        if key_hdr not in _auth_headers() or not _auth_headers().get(key_hdr):
            raise RuntimeError("未配置 SUB2API_API_KEY")

    gid = _pool_group_id(group_id)
    headers = _auth_headers()
    out: list[dict] = []
    page = 1
    page_size = max(1, min(200, int(page_size or 100)))
    max_pages = max(1, min(200, int(max_pages or 50)))

    while page <= max_pages:
        params: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "platform": platform,
            "type": account_type,
            "sort_by": "id",
            "sort_order": "desc",
        }
        if status:
            params["status"] = status
        if gid:
            params["group"] = gid
        url = urljoin(base + "/", "api/v1/admin/accounts")
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": (resp.text or "")[:300]}
        if resp.status_code != 200:
            raise RuntimeError(f"list accounts HTTP {resp.status_code}: {body}")
        items, total = _extract_list_payload(body)
        out.extend(items)
        if not items or len(out) >= total or len(items) < page_size:
            break
        page += 1
        time.sleep(0.05)
    return out


def _looks_like_email(value: Any) -> bool:
    s = _norm_email(value)
    return bool(s and "@" in s and " " not in s and not s.startswith("#"))


def _pool_email(acc: dict) -> str:
    """尽量从号池账号抽出真实邮箱。

    添加邮箱跑 Codex 导入时，name 可能是批次名（如 26.8.5.10.38 #19），
    真实邮箱在 extra.email / credentials.email / credentials.outlook_email。
    """
    creds = acc.get("credentials") if isinstance(acc.get("credentials"), dict) else {}
    extra = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}
    nested_extra = creds.get("extra") if isinstance(creds.get("extra"), dict) else {}

    candidates = [
        acc.get("email"),
        creds.get("email"),
        creds.get("outlook_email"),
        creds.get("name"),
        extra.get("email"),
        extra.get("name"),
        extra.get("email_key"),
        nested_extra.get("email"),
        nested_extra.get("name"),
        acc.get("name"),
        acc.get("notes"),
    ]
    for raw in candidates:
        e = _norm_email(raw)
        if _looks_like_email(e):
            return e
    # name 不是邮箱时不要把批次名当 email 返回
    name = _norm_email(acc.get("name") or "")
    return name if _looks_like_email(name) else ""


def _error_text(acc: dict) -> str:
    """仅拼错误/状态相关文本，避免 credentials_status 字段名造成误匹配。"""
    parts = [
        str(acc.get("status") or ""),
        str(acc.get("error_message") or acc.get("errorMessage") or ""),
        str(acc.get("temp_unschedulable_reason") or acc.get("tempUnschedulableReason") or ""),
        str(acc.get("last_error") or ""),
    ]
    return " ".join(parts).lower()


def classify_pool_account(acc: dict) -> str:
    """返回: ok / rt_bad / dead_hint / unknown。"""
    err_blob = _error_text(acc)
    status = str(acc.get("status") or "").strip().lower()
    err = str(acc.get("error_message") or acc.get("errorMessage") or "").strip()
    err_l = err.lower()
    creds_status = acc.get("credentials_status") if isinstance(acc.get("credentials_status"), dict) else {}
    has_rt = creds_status.get("has_refresh_token")
    if has_rt is None and isinstance(acc.get("credentials"), dict):
        rt = acc["credentials"].get("refresh_token") or acc["credentials"].get("refreshToken")
        has_rt = bool(str(rt or "").strip()) if rt is not None else None

    if any(h in err_blob for h in _DEAD_HINTS) or status in ("banned", "deleted"):
        return "dead_hint"

    # 结构化：明确无 RT
    if has_rt is False:
        return "rt_bad"

    # 错误文案像掉 RT / 需重登
    if err and any(h in err_l for h in _RT_BAD_HINTS):
        return "rt_bad"

    # 状态本身就是凭据类异常
    if status in ("unauthorized", "invalid", "error"):
        # error 但没有废号/RT 关键词时仍按 rt_bad 处理（号池 error 多数是 token 刷新失败）
        if not err or any(h in err_l for h in _RT_BAD_HINTS) or status in ("unauthorized", "invalid"):
            return "rt_bad"
        return "unknown"

    if status in ("active", "ok", "healthy", "") and not err:
        return "ok"
    if status in ("active", "ok", "healthy") and err:
        # 有 error_message 但状态仍 active：交人工
        return "unknown"
    if not err and status in ("disabled", "inactive", "paused"):
        return "unknown"
    return "unknown"


def _index_put(
    idx: dict[str, dict],
    email: str,
    *,
    source: str,
    account_id: Any = None,
    codex_status: str = "",
    filename: str = "",
    exported: bool = False,
) -> None:
    email = _norm_email(email)
    if not email or "@" not in email:
        return
    rec = idx.setdefault(email, {
        "email": email,
        "account_id": None,
        "codex_status": "",
        "filenames": [],
        "exported": False,
        "sources": [],
    })
    if account_id is not None and rec.get("account_id") is None:
        rec["account_id"] = account_id
    if codex_status and not rec.get("codex_status"):
        rec["codex_status"] = codex_status
    if filename and filename not in rec["filenames"]:
        rec["filenames"].append(filename)
    if exported:
        rec["exported"] = True
    if source and source not in rec["sources"]:
        rec["sources"].append(source)


def build_local_index() -> dict[str, dict]:
    """email -> {account_id, codex_status, filenames[], exported, sources}。

    覆盖：注册账号、Codex 凭证、邮箱池（Outlook/通用API/域名）。
    添加邮箱跑 Codex 的号，本地可能只有邮箱池记录、没有 registered_accounts。
    """
    idx: dict[str, dict] = {}
    # 注册账号表
    try:
        rows = db.list_accounts(limit=100000, offset=0, archived=False)
        for r in rows or []:
            _index_put(
                idx,
                r.get("email"),
                source="account",
                account_id=r.get("id"),
                codex_status=str(r.get("codex_status") or ""),
            )
    except Exception as exc:
        logger.warning("[号池监控] 读本地账号失败：%s", exc)

    # Codex 本地凭证 + 导出标记
    try:
        for r in db.list_codex_accounts() or []:
            fname = str(r.get("filename") or "").strip()
            _index_put(
                idx,
                r.get("email"),
                source="codex",
                filename=fname,
                exported=int(r.get("exported_count") or 0) > 0,
            )
    except Exception as exc:
        logger.warning("[号池监控] 读本地 codex 失败：%s", exc)

    # 邮箱池：添加邮箱后跑 Codex 的主素材来源（limit 要够大）
    try:
        for r in db.list_outlook_pool(limit=200000) or []:
            _index_put(idx, r.get("email"), source="outlook_pool")
    except Exception as exc:
        logger.warning("[号池监控] 读 outlook 池失败：%s", exc)
    try:
        for r in db.list_generic_api_email_pool(limit=200000) or []:
            _index_put(idx, r.get("email"), source="generic_api_pool")
    except Exception as exc:
        logger.warning("[号池监控] 读 generic_api 池失败：%s", exc)
    try:
        if hasattr(db, "list_domain_email_pool"):
            for r in db.list_domain_email_pool(limit=200000) or []:
                _index_put(idx, r.get("email"), source="domain_pool")
    except Exception as exc:
        logger.warning("[号池监控] 读 domain 池失败：%s", exc)
    return idx


def _is_our_pool_account(acc: dict, local: dict | None) -> bool:
    """是否本站号：必须以本机有记录为准。

    - 本机 registered / codex 文件 / 邮箱池 命中 → 本站
    - 仅号池 extra.source=codex-register、sub_bundle_input 等 → 外渠道，排除
      （这类号没有本机登录素材，检测出来也无法补跑）
    - 可选：import_source 为本工具推送标记时仍算本站（历史推送后本机文件被删的情况）
    """
    if local:
        return True
    extra = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}
    src = str(extra.get("import_source") or "").strip().lower()
    if src and any(s in src for s in _OUR_PUSH_IMPORT_SOURCES):
        return True
    return False


def assess_login_material(email: str) -> dict[str, Any]:
    """判断 Codex 补跑/重授权是否具备本地登录素材。

    支持两条路径（与 roxy/cloak Codex 授权一致）：
      1) password_totp：已注册账号带 password + totp_secret（邮箱----密码----2FA）
      2) email_otp：本地邮箱池可收信（generic_api / outlook / 域名 / 临时邮箱等）

    仅有邮箱字符串、两边都没有 → 不可补跑（当前 16 个失败就是这种情况）。
    """
    email = str(email or "").strip()
    out: dict[str, Any] = {
        "email": email,
        "repairable": False,
        "login_mode": "none",
        "has_password": False,
        "has_totp": False,
        "mail_source": "",
        "account_id": None,
        "reason": "email 为空",
    }
    if not email or "@" not in email:
        return out

    acc = None
    try:
        acc = db.get_account_by_email(email)
    except Exception:
        acc = None
    password = str((acc or {}).get("password") or "").strip()
    totp = str((acc or {}).get("totp_secret") or "").strip()
    # 兼容 original_email_line: email----password----totp
    if (not password or not totp) and acc and acc.get("original_email_line"):
        parts = re.split(r"-{2,}|\|{1,}|={2,}", str(acc.get("original_email_line") or ""))
        parts = [p.strip() for p in parts if str(p or "").strip()]
        if len(parts) >= 3 and "@" in parts[0]:
            if not password and len(parts) >= 2:
                password = parts[1]
            if not totp:
                # 常见 email----pwd----totp 或 email----pwd----xxx----totp
                cand = parts[2] if len(parts) == 3 else parts[-1]
                if cand and "@" not in cand and len(cand) >= 8:
                    totp = cand
    out["has_password"] = bool(password)
    out["has_totp"] = bool(totp)
    out["account_id"] = (acc or {}).get("id")
    email_source = str((acc or {}).get("email_source") or "").strip().lower()

    mail_source = ""
    try:
        from core.email_provider import resolve_email_source
        # resolve 在找不到时会回落到 EMAIL_SOURCE 第一项；这里再校验池内是否真有记录
        guessed = resolve_email_source(email)
    except Exception:
        guessed = ""

    # 严格校验：必须真在某个可收信池里
    try:
        if db.get_generic_api_email_by_email(email):
            mail_source = "generic_api"
        elif db.get_outlook_by_email(email):
            mail_source = "outlook"
        else:
            try:
                if db._find_domain_email(db._load_domain_pool(), email):
                    mail_source = "cloudflare_domain"
            except Exception:
                pass
            if not mail_source:
                try:
                    from core.gptmail_client import get_account_context as _g
                    if _g(email):
                        mail_source = "gptmail"
                except Exception:
                    pass
            if not mail_source:
                try:
                    from core.mailnest_client import get_account_context as _m
                    if _m(email):
                        mail_source = "mailnest"
                except Exception:
                    pass
            if not mail_source:
                try:
                    from core.cloudmail_client import get_account_context as _c
                    if _c(email):
                        mail_source = "cloudmail"
                except Exception:
                    pass
            if not mail_source:
                try:
                    from core.cf_temp_mail_client import get_account_context as _cf
                    if _cf(email):
                        mail_source = "cloudflare"
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("[号池监控] 校验收信素材失败 email=%s: %s", email, exc)

    out["mail_source"] = mail_source or ""
    out["email_source_guess"] = guessed or ""

    # 优先密码+2FA（添加邮箱跑 Codex 的主流格式）
    if password and totp:
        out["repairable"] = True
        out["login_mode"] = "password_totp"
        out["reason"] = "可用密码+2FA 登录补跑（无需邮箱接码）"
        return out

    if mail_source:
        out["repairable"] = True
        out["login_mode"] = "email_otp"
        out["reason"] = f"可用邮箱 OTP 收信（来源 {mail_source}）"
        return out

    if password and not totp:
        out["login_mode"] = "password_partial"
        out["reason"] = (
            "有密码但无 2FA 密钥；若登录要 TOTP 会失败。"
            "请用「邮箱----密码----2FA密钥」重新导入该账号"
        )
        return out

    if totp and not password:
        out["login_mode"] = "totp_partial"
        out["reason"] = "有 2FA 但无密码，且本地无收信池记录，无法补跑"
        return out

    out["login_mode"] = "none"
    out["reason"] = (
        "本地无登录素材：既没有「邮箱----密码----2FA」，"
        "也没有「邮箱----接码地址/Outlook」等收信记录。"
        "请先在邮箱池/账号页导入对应素材后再修复"
    )
    # email_source 字段仅作提示
    if email_source:
        out["reason"] += f"（账号 email_source={email_source}）"
    return out


def scan_pool(
    *,
    group_id: int | None = None,
    include_ok: bool = False,
    page_size: int = 100,
) -> dict[str, Any]:
    """扫描号池并分类。

    Returns:
      {
        ok, scanned_at, summary, items:[
          {pool_id, email, status, action, reason, local_*, ...}
        ]
      }
    """
    accounts = fetch_pool_accounts(group_id=group_id, page_size=page_size)
    local_idx = build_local_index()
    items: list[dict] = []
    summary = {
        "total_pool": len(accounts),
        "ours": 0,
        "ignored": 0,
        "ok": 0,
        "rt_bad": 0,
        "dead": 0,
        "unknown": 0,
        "local_dead_skip": 0,
        "repairable": 0,
        "need_material": 0,
    }

    for acc in accounts:
        email = _pool_email(acc)
        pool_id = acc.get("id") or acc.get("account_id")
        local = local_idx.get(email) if email else None
        ours = _is_our_pool_account(acc, local)
        if not ours:
            summary["ignored"] += 1
            items.append({
                "pool_id": pool_id,
                "email": email or str(acc.get("name") or ""),
                "pool_status": acc.get("status"),
                "error_message": acc.get("error_message") or acc.get("errorMessage") or "",
                "action": "ignore",
                "reason": "非本站匹配账号（外渠道）",
                "classification": "foreign",
            })
            continue

        summary["ours"] += 1
        local_status = str((local or {}).get("codex_status") or "").lower()
        if local_status == "deactivated":
            summary["local_dead_skip"] += 1
            summary["dead"] += 1
            items.append({
                "pool_id": pool_id,
                "email": email,
                "pool_status": acc.get("status"),
                "error_message": acc.get("error_message") or acc.get("errorMessage") or "",
                "action": "skip_dead",
                "reason": "本地已标记废号",
                "classification": "dead",
                "local_account_id": (local or {}).get("account_id"),
                "local_codex_status": local_status,
                "local_filenames": (local or {}).get("filenames") or [],
                "exported": bool((local or {}).get("exported")),
            })
            continue

        material = assess_login_material(email) if email else {
            "repairable": False, "login_mode": "none", "reason": "无邮箱",
        }
        cls = classify_pool_account(acc)
        if cls == "ok":
            summary["ok"] += 1
            if not include_ok:
                continue
            action, reason = "none", "号池状态正常"
        elif cls == "dead_hint":
            summary["dead"] += 1
            action, reason = "mark_dead", "号池错误信息疑似废号"
        elif cls == "rt_bad":
            summary["rt_bad"] += 1
            if material.get("repairable"):
                summary["repairable"] += 1
                action, reason = "reauth_repush", (
                    f"掉 RT，可补跑（{material.get('login_mode')}）：{material.get('reason')}"
                )
            else:
                summary["need_material"] += 1
                action, reason = "need_material", (
                    f"掉 RT，但本地缺登录素材，无法补跑：{material.get('reason')}"
                )
        else:
            summary["unknown"] += 1
            action, reason = "review", "状态不明，建议人工看 error_message"

        items.append({
            "pool_id": pool_id,
            "email": email,
            "pool_status": acc.get("status"),
            "error_message": (acc.get("error_message") or acc.get("errorMessage") or "")[:500],
            "schedulable": acc.get("schedulable"),
            "action": action,
            "reason": reason,
            "classification": cls,
            "repairable": bool(material.get("repairable")) if cls == "rt_bad" else None,
            "login_mode": material.get("login_mode") or "",
            "material_reason": material.get("reason") or "",
            "local_account_id": (local or {}).get("account_id") or material.get("account_id"),
            "local_codex_status": (local or {}).get("codex_status") or "",
            "local_filenames": (local or {}).get("filenames") or [],
            "exported": bool((local or {}).get("exported")),
            "credentials_status": acc.get("credentials_status") if isinstance(acc.get("credentials_status"), dict) else {},
            "extra_import_source": (
                (acc.get("extra") or {}).get("import_source")
                if isinstance(acc.get("extra"), dict) else ""
            ),
            "extra_source": (
                (acc.get("extra") or {}).get("source")
                if isinstance(acc.get("extra"), dict) else ""
            ),
            "local_sources": (local or {}).get("sources") or [],
            "pool_name": acc.get("name") or "",
        })

    result = {
        "ok": True,
        "scanned_at": _now(),
        "group_id": _pool_group_id(group_id),
        "api_base": _api_base(),
        "summary": summary,
        "items": items,
    }
    with _LOCK:
        global _LAST_SCAN
        _LAST_SCAN = result
    return result


def get_last_scan() -> dict[str, Any] | None:
    with _LOCK:
        return dict(_LAST_SCAN) if _LAST_SCAN else None


def delete_pool_account(pool_id: int) -> dict:
    """从 sub2api 号池删除账号。DELETE /api/v1/admin/accounts/:id"""
    pid = int(pool_id)
    base = _api_base()
    url = urljoin(base + "/", f"api/v1/admin/accounts/{pid}")
    try:
        resp = requests.delete(url, headers=_auth_headers(), timeout=30)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": (resp.text or "")[:200]}
        ok = resp.status_code in (200, 201, 204)
        # 部分实现删不存在也返回 200 success
        if not ok and resp.status_code == 404:
            return {"ok": True, "pool_id": pid, "deleted": False, "missing": True, "http": resp.status_code}
        return {
            "ok": ok,
            "pool_id": pid,
            "deleted": ok,
            "http": resp.status_code,
            "body": data,
            "error": None if ok else str(data)[:200],
        }
    except Exception as exc:
        logger.warning("[号池监控] 删除号池账号失败 pool_id=%s: %s", pid, exc)
        return {"ok": False, "pool_id": pid, "deleted": False, "error": f"{type(exc).__name__}: {exc}"}


def find_pool_ids_for_email(email: str, *, pool_id: int | None = None) -> list[int]:
    """解析要删除的号池 id：优先显式 pool_id，其次最近扫描，再按邮箱搜号池。"""
    ids: list[int] = []
    if pool_id is not None and str(pool_id).strip() != "":
        try:
            ids.append(int(pool_id))
        except Exception:
            pass

    key = _norm_email(email)
    if not key:
        return ids

    # last scan
    scan = get_last_scan() or {}
    for it in scan.get("items") or []:
        if _norm_email(it.get("email")) != key:
            continue
        if it.get("action") == "ignore":
            continue
        pid = it.get("pool_id")
        if pid is None:
            continue
        try:
            pid_i = int(pid)
        except Exception:
            continue
        if pid_i not in ids:
            ids.append(pid_i)

    if ids:
        return ids

    # 在线按邮箱搜（兼容 search / name 参数）
    try:
        base = _api_base()
        headers = _auth_headers()
        for param_key in ("search", "q", "name", "email"):
            url = urljoin(base + "/", "api/v1/admin/accounts")
            resp = requests.get(
                url,
                headers=headers,
                params={
                    "page": 1,
                    "page_size": 50,
                    "platform": "openai",
                    "type": "oauth",
                    param_key: email,
                },
                timeout=25,
            )
            if resp.status_code != 200:
                continue
            try:
                body = resp.json()
            except Exception:
                continue
            items, _ = _extract_list_payload(body)
            for acc in items:
                if _pool_email(acc) != key:
                    continue
                pid = acc.get("id") or acc.get("account_id")
                if pid is None:
                    continue
                try:
                    pid_i = int(pid)
                except Exception:
                    continue
                if pid_i not in ids:
                    ids.append(pid_i)
            if ids:
                break
    except Exception as exc:
        logger.warning("[号池监控] 按邮箱查找号池 id 失败 email=%s: %s", email, exc)
    return ids


def mark_local_dead(
    email: str,
    reason: str = "号池监控判定废号",
    *,
    pool_id: int | None = None,
    delete_pool: bool = True,
) -> dict:
    """本地标废；默认同时从号池删除对应账号。"""
    email = str(email or "").strip()
    if not email:
        return {"ok": False, "error": "email 为空"}
    db.update_account_codex_status(email, "deactivated", reason[:300])
    out: dict[str, Any] = {
        "ok": True,
        "email": email,
        "status": "dead",
        "codex_status": "deactivated",
        "reason": reason,
        "pool_deleted": [],
        "pool_delete_failed": [],
    }
    if not delete_pool:
        return out

    pids = find_pool_ids_for_email(email, pool_id=pool_id)
    if not pids:
        out["message"] = "本地已标废；未找到号池 id，跳过删除"
        out["pool_delete_skipped"] = True
        return out

    for pid in pids:
        res = delete_pool_account(pid)
        if res.get("ok"):
            out["pool_deleted"].append(pid)
        else:
            out["pool_delete_failed"].append(res)

    if out["pool_delete_failed"] and not out["pool_deleted"]:
        out["ok"] = False
        out["error"] = "本地已标废，但号池删除失败"
        out["message"] = out["error"]
    elif out["pool_delete_failed"]:
        out["message"] = (
            f"本地已标废；号池删除部分成功 deleted={out['pool_deleted']} "
            f"failed={len(out['pool_delete_failed'])}"
        )
    else:
        out["message"] = f"本地已标废，并已从号池删除 id={out['pool_deleted']}"
    return out


def _find_local_codex_file(email: str) -> str | None:
    email_l = _norm_email(email)
    from core.sub2api_pool_push import _extract_token_blob
    for r in db.list_codex_accounts() or []:
        if _norm_email(r.get("email")) != email_l:
            continue
        fname = str(r.get("filename") or "")
        if not fname or fname.endswith("-sub2-callback.json") or fname.endswith("-cpa-callback.json"):
            continue
        try:
            raw, real = db.read_codex_credential(fname)
            content = json.loads(raw)
            if not isinstance(content, dict):
                continue
            if content.get("access_token") or content.get("accessToken") or _extract_token_blob(content):
                return real or fname
        except Exception:
            continue
    return None


def _enable_pool_schedulable(pool_id: int) -> dict:
    """掉 RT 后 sub2 会关调度开关；修复后必须重新打开。

    优先专用接口 POST /accounts/:id/schedulable；并清理 temp-unschedulable。
    """
    base = _api_base()
    pid = int(pool_id)
    headers = _auth_headers()
    out: dict[str, Any] = {"pool_id": pid, "schedulable": False}

    # 1) 专用开关接口
    try:
        url = urljoin(base + "/", f"api/v1/admin/accounts/{pid}/schedulable")
        resp = requests.post(url, headers=headers, json={"schedulable": True}, timeout=20)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": (resp.text or "")[:200]}
        out["schedulable_api"] = {"http": resp.status_code, "body": data}
        if resp.status_code in (200, 201):
            out["schedulable"] = True
    except Exception as exc:
        out["schedulable_api"] = {"error": f"{type(exc).__name__}: {exc}"}

    # 2) PUT 兜底再写一次 schedulable
    if not out.get("schedulable"):
        try:
            url = urljoin(base + "/", f"api/v1/admin/accounts/{pid}")
            resp = requests.put(url, headers=headers, json={"schedulable": True}, timeout=20)
            try:
                data = resp.json()
            except Exception:
                data = {"raw": (resp.text or "")[:200]}
            out["schedulable_put"] = {"http": resp.status_code, "body": data}
            if resp.status_code in (200, 201):
                out["schedulable"] = True
        except Exception as exc:
            out["schedulable_put"] = {"error": f"{type(exc).__name__}: {exc}"}

    # 3) 清临时不可调度
    try:
        url = urljoin(base + "/", f"api/v1/admin/accounts/{pid}/temp-unschedulable")
        resp = requests.delete(url, headers=headers, timeout=15)
        out["clear_temp_unschedulable"] = {"http": resp.status_code}
    except Exception as exc:
        out["clear_temp_unschedulable"] = {"error": f"{type(exc).__name__}: {exc}"}

    if not out.get("schedulable"):
        logger.warning("[号池监控] 重新打开调度失败 pool_id=%s detail=%s", pid, out)
    return out


def _update_pool_credentials(pool_id: int, account_payload: dict) -> dict:
    """PUT 更新号池账号凭据（重推），并重新打开调度开关。"""
    base = _api_base()
    url = urljoin(base + "/", f"api/v1/admin/accounts/{int(pool_id)}")
    body = {
        "credentials": account_payload.get("credentials") or {},
    }
    if account_payload.get("expires_at") is not None:
        body["expires_at"] = account_payload.get("expires_at")
    # 清错误、恢复可调度（schedulable 单独再调一次专用接口，避免 PUT 被忽略）
    body["status"] = "active"
    body["error_message"] = ""
    body["schedulable"] = True
    resp = requests.put(url, headers=_auth_headers(), json=body, timeout=40)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": (resp.text or "")[:300]}
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"update account HTTP {resp.status_code}: {data}")
    # 清 error 接口可选
    try:
        clr = urljoin(base + "/", f"api/v1/admin/accounts/{int(pool_id)}/clear-error")
        requests.post(clr, headers=_auth_headers(), timeout=15)
    except Exception:
        pass
    # 掉 RT 后调度开关会被关掉，必须显式打开
    sched = _enable_pool_schedulable(int(pool_id))
    if isinstance(data, dict):
        data = dict(data)
        data["_schedulable_restore"] = sched
        return data
    return {"data": data, "_schedulable_restore": sched}


def repair_one(email: str, pool_id: int | None = None, *, do_reauth: bool = True) -> dict:
    """对单个本站账号：可选 Codex 补跑 → 用本地凭证更新号池。

    废号会本地标记并返回 dead，不推送。
    """
    email = str(email or "").strip()
    if not email:
        return {"ok": False, "error": "email 为空"}
    key = email.lower()
    with _LOCK:
        if key in _REPAIR_RUNNING:
            return {"ok": False, "error": "该账号正在修复中", "busy": True}
        _REPAIR_RUNNING.add(key)

    try:
        local = db.get_account_by_email(email)
        if local and str(local.get("codex_status") or "").lower() == "deactivated":
            return {
                "ok": False,
                "email": email,
                "status": "dead",
                "message": "本地已是废号，跳过",
            }

        # 补跑前预检：无密码+2FA、也无收信池 → 不空开浏览器
        material = assess_login_material(email)
        if do_reauth and not material.get("repairable"):
            return {
                "ok": False,
                "email": email,
                "status": "need_material",
                "login_mode": material.get("login_mode") or "none",
                "message": material.get("reason") or "本地无登录素材，无法补跑",
                "material": material,
            }

        reauth_result: dict[str, Any] = {"skipped": True, "ok": True}
        if do_reauth:
            from core import codex_retry_service
            if not codex_retry_service.reserve(email):
                return {"ok": False, "email": email, "error": "Codex 补跑占位失败（可能已在补跑）"}
            try:
                # run_worker 结束时会自己 release
                reauth_result = codex_retry_service.run_worker(email, batch_label="pool-monitor-repair")
            except Exception:
                try:
                    codex_retry_service.release(email)
                except Exception:
                    pass
                raise

            st = str(reauth_result.get("status") or "")
            if st == "deactivated" or (not reauth_result.get("ok") and "deactivat" in str(reauth_result.get("message") or "").lower()):
                # 补跑确认废号：本地标废 + 号池删除
                dead_res = mark_local_dead(
                    email,
                    reauth_result.get("message") or "补跑判定废号",
                    pool_id=pool_id,
                    delete_pool=True,
                )
                return {
                    "ok": False,
                    "email": email,
                    "status": "dead",
                    "message": dead_res.get("message") or reauth_result.get("message") or "废号",
                    "reauth": reauth_result,
                    "pool_deleted": dead_res.get("pool_deleted") or [],
                    "pool_delete_failed": dead_res.get("pool_delete_failed") or [],
                }
            if not reauth_result.get("ok"):
                return {
                    "ok": False,
                    "email": email,
                    "status": "reauth_failed",
                    "message": reauth_result.get("message") or "重授权失败",
                    "reauth": reauth_result,
                }

        # 找本地 codex 文件推送
        fname = _find_local_codex_file(email)
        if not fname and reauth_result.get("file_path"):
            from pathlib import Path
            fname = Path(str(reauth_result["file_path"])).name
        if not fname:
            return {
                "ok": False,
                "email": email,
                "status": "no_local_cred",
                "message": "重授权后未找到本地 codex 凭证文件",
                "reauth": reauth_result,
            }

        raw, real_fname = db.read_codex_credential(fname)
        content = json.loads(raw)
        account_payload = build_pool_account_from_codex_json(content, filename=real_fname)
        # 确定 pool_id
        pid = pool_id
        if pid is None:
            # 从 last scan 找
            scan = get_last_scan() or {}
            for it in scan.get("items") or []:
                if _norm_email(it.get("email")) == key and it.get("pool_id") is not None:
                    pid = it.get("pool_id")
                    break
        if pid is None:
            # 再扫一次该 email
            try:
                found = fetch_pool_accounts(page_size=50)
                for acc in found:
                    if _pool_email(acc) == key:
                        pid = acc.get("id")
                        break
            except Exception:
                pass
        if pid is None:
            # 无 pool id 则 batch 新建
            from core.sub2api_pool_push import push_codex_files_to_pool
            push_res = push_codex_files_to_pool([real_fname])
            ok_push = bool(push_res.get("ok") or push_res.get("success"))
            # batch 若返回 account_id，也确保调度打开
            sched = None
            try:
                for it in (push_res.get("results") or []):
                    if not isinstance(it, dict):
                        continue
                    if _norm_email(it.get("email")) == key and it.get("account_id") is not None:
                        sched = _enable_pool_schedulable(int(it["account_id"]))
                        break
                    if it.get("ok") and it.get("account_id") is not None and not sched:
                        sched = _enable_pool_schedulable(int(it["account_id"]))
            except Exception:
                pass
            return {
                "ok": ok_push,
                "email": email,
                "status": "reauth_repush_batch",
                "message": f"无 pool_id，已 batch 推送 success={push_res.get('success')} failed={push_res.get('failed')}",
                "reauth": reauth_result,
                "push": push_res,
                "schedulable": (sched or {}).get("schedulable"),
            }

        upd = _update_pool_credentials(int(pid), account_payload)
        try:
            db.mark_codex_exported(real_fname)
        except Exception:
            pass
        sched_ok = bool((upd or {}).get("_schedulable_restore", {}).get("schedulable"))
        return {
            "ok": True,
            "email": email,
            "pool_id": int(pid),
            "status": "repaired",
            "message": (
                "重授权并更新号池凭据成功，已重新打开调度"
                if sched_ok
                else "重授权并更新号池凭据成功，但打开调度可能失败，请检查"
            ),
            "reauth": {"status": reauth_result.get("status"), "ok": reauth_result.get("ok")},
            "update": {"ok": True},
            "schedulable": sched_ok,
            "filename": real_fname,
        }
    except Exception as exc:
        logger.exception("[号池监控] 修复失败 email=%s", email)
        return {"ok": False, "email": email, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        with _LOCK:
            _REPAIR_RUNNING.discard(key)


def _repair_job_snapshot() -> dict[str, Any] | None:
    with _LOCK:
        if not _REPAIR_JOB:
            return None
        return json.loads(json.dumps(_REPAIR_JOB, ensure_ascii=False, default=str))


def get_repair_job() -> dict[str, Any] | None:
    """返回当前/最近一次修复任务进度（含 running/done/error）。"""
    return _repair_job_snapshot()


def _set_repair_job(**fields: Any) -> None:
    global _REPAIR_JOB
    with _LOCK:
        if _REPAIR_JOB is None:
            return
        _REPAIR_JOB.update(fields)
        _REPAIR_JOB["updated_at"] = _now()


def _append_repair_result(item: dict) -> None:
    with _LOCK:
        if not _REPAIR_JOB:
            return
        results = list(_REPAIR_JOB.get("results") or [])
        results.append(item)
        ok_n = sum(1 for r in results if r.get("ok"))
        dead_n = sum(1 for r in results if r.get("status") == "dead")
        failed_n = len(results) - ok_n - dead_n
        _REPAIR_JOB["results"] = results
        _REPAIR_JOB["done"] = len(results)
        _REPAIR_JOB["success"] = ok_n
        _REPAIR_JOB["dead"] = dead_n
        _REPAIR_JOB["failed"] = failed_n
        _REPAIR_JOB["updated_at"] = _now()


def _run_one_target(t: dict, *, do_reauth: bool) -> dict:
    email = str((t or {}).get("email") or "").strip()
    if not email:
        return {"ok": False, "error": "email 为空"}
    action = str((t or {}).get("action") or "")
    pid = (t or {}).get("pool_id")
    try:
        pid_i = int(pid) if pid is not None and str(pid).strip() != "" else None
    except Exception:
        pid_i = None
    if action == "mark_dead":
        return mark_local_dead(
            email,
            str((t or {}).get("reason") or "号池监控标记废号"),
            pool_id=pid_i,
            delete_pool=True,
        )
    if action == "need_material":
        material = assess_login_material(email)
        return {
            "ok": False,
            "email": email,
            "pool_id": pid_i,
            "status": "need_material",
            "login_mode": material.get("login_mode") or "none",
            "message": material.get("reason") or "本地无登录素材，跳过补跑",
            "material": material,
        }
    return repair_one(email, pid_i, do_reauth=do_reauth)


def repair_many(
    targets: list[dict],
    *,
    do_reauth: bool = True,
    max_workers: int = 10,
    progress_cb=None,
) -> dict:
    """批量修复。targets: [{email, pool_id?}, ...]

    默认 10 线程并行（max_workers=10）。
    progress_cb(dict) 可选，用于后台任务推送进度。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    work: list[dict] = []
    for t in targets or []:
        email = str((t or {}).get("email") or "").strip()
        if not email:
            continue
        work.append(t)

    total = len(work)
    workers = max(1, min(20, int(max_workers or 10)))
    results: list[dict] = []
    active: dict[str, str] = {}  # email -> phase
    active_lock = threading.Lock()
    done_count = 0
    done_lock = threading.Lock()

    def _active_msg() -> str:
        with active_lock:
            emails = list(active.keys())
        if not emails:
            return f"并行修复中 workers={workers}，等待调度…"
        show = emails[:6]
        more = f" 等{len(emails)}个" if len(emails) > 6 else f" ({len(emails)}并行)"
        return f"并行修复中 workers={workers}：{', '.join(show)}{more if len(emails) > 1 else ''}"

    if progress_cb:
        progress_cb({
            "status": "running",
            "phase": "starting",
            "total": total,
            "done": 0,
            "current_index": 0,
            "current_email": "",
            "active_emails": [],
            "max_workers": workers,
            "message": f"准备并行修复 {total} 个账号（{workers} 线程）",
        })

    def _one(idx: int, t: dict) -> tuple[int, dict, list[str]]:
        email = str(t.get("email") or "").strip()
        action = str(t.get("action") or "")
        phase = "mark_dead" if action == "mark_dead" else "reauth_repush"
        with active_lock:
            active[email] = phase
        if progress_cb:
            with active_lock:
                active_list = list(active.keys())
            progress_cb({
                "status": "running",
                "phase": phase,
                "total": total,
                "current_index": idx,
                "current_email": email,
                "active_emails": active_list,
                "max_workers": workers,
                "message": _active_msg(),
            })
        try:
            one = _run_one_target(t, do_reauth=do_reauth)
        except Exception as exc:
            one = {"ok": False, "email": email, "error": f"{type(exc).__name__}: {exc}"}
        with active_lock:
            active.pop(email, None)
            active_list = list(active.keys())
        return idx, one, active_list

    if total == 0:
        out = {"ok": True, "success": 0, "dead": 0, "failed": 0, "total": 0, "results": []}
        if progress_cb:
            progress_cb({
                "status": "done",
                "phase": "finished",
                "total": 0,
                "done": 0,
                "message": "无待修复账号",
                "success": 0,
                "dead": 0,
                "failed": 0,
                "finished_at": _now(),
                "max_workers": workers,
            })
        return out

    # 结果按提交顺序回填，进度按完成数统计
    ordered: list[dict | None] = [None] * total
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pm-repair") as ex:
        futs = {ex.submit(_one, idx, t): idx for idx, t in enumerate(work, start=1)}
        for fut in as_completed(futs):
            try:
                ret = fut.result()
                idx, one = ret[0], ret[1]
                active_list = ret[2] if len(ret) > 2 else []
            except Exception as exc:
                idx = futs[fut]
                email = str(work[idx - 1].get("email") or "")
                one = {"ok": False, "email": email, "error": f"{type(exc).__name__}: {exc}"}
                with active_lock:
                    active.pop(email, None)
                    active_list = list(active.keys())
            ordered[idx - 1] = one
            with done_lock:
                done_count += 1
                cur_done = done_count
            if progress_cb:
                progress_cb({
                    "append_result": one,
                    "status": "running",
                    "phase": "item_done",
                    "total": total,
                    "done": cur_done,
                    "current_index": cur_done,
                    "current_email": one.get("email") or "",
                    "active_emails": active_list,
                    "max_workers": workers,
                    "message": (
                        f"[{cur_done}/{total}] 完成 {one.get('email') or '-'} → "
                        f"{'成功' if one.get('ok') else one.get('status') or one.get('error') or '失败'}"
                        f"；{_active_msg()}"
                    ),
                })

    results = [r for r in ordered if r is not None]
    ok_n = sum(1 for r in results if r.get("ok"))
    dead_n = sum(1 for r in results if r.get("status") == "dead")
    out = {
        "ok": ok_n > 0 and ok_n + dead_n == len(results),
        "success": ok_n,
        "dead": dead_n,
        "failed": len(results) - ok_n - dead_n,
        "total": len(results),
        "results": results,
        "max_workers": workers,
    }
    if progress_cb:
        progress_cb({
            "status": "done",
            "phase": "finished",
            "total": total,
            "done": len(results),
            "current_index": total,
            "current_email": "",
            "active_emails": [],
            "max_workers": workers,
            "message": f"修复结束（{workers} 线程）：成功 {ok_n}，废号 {dead_n}，失败 {out['failed']}",
            "success": ok_n,
            "dead": dead_n,
            "failed": out["failed"],
            "finished_at": _now(),
        })
    return out


def start_repair_job(
    targets: list[dict],
    *,
    do_reauth: bool = True,
    max_workers: int = 10,
) -> dict:
    """后台启动修复任务，立即返回 job 快照；前端轮询 get_repair_job。"""
    global _REPAIR_JOB, _REPAIR_JOB_SEQ
    work = [t for t in (targets or []) if str((t or {}).get("email") or "").strip()]
    if not work:
        return {"ok": False, "error": "targets 为空"}
    workers = max(1, min(20, int(max_workers or 10)))

    with _LOCK:
        if _REPAIR_JOB and str(_REPAIR_JOB.get("status") or "") == "running":
            return {
                "ok": False,
                "error": "已有修复任务在运行",
                "busy": True,
                "job": _repair_job_snapshot(),
            }
        _REPAIR_JOB_SEQ += 1
        job_id = f"pm-repair-{_REPAIR_JOB_SEQ}-{int(time.time())}"
        _REPAIR_JOB = {
            "ok": True,
            "job_id": job_id,
            "status": "running",
            "phase": "queued",
            "total": len(work),
            "done": 0,
            "success": 0,
            "dead": 0,
            "failed": 0,
            "current_index": 0,
            "current_email": "",
            "active_emails": [],
            "max_workers": workers,
            "message": f"已排队，共 {len(work)} 个（{workers} 线程并行）",
            "results": [],
            "started_at": _now(),
            "updated_at": _now(),
            "finished_at": None,
            "do_reauth": bool(do_reauth),
        }

    def _progress(evt: dict) -> None:
        if not isinstance(evt, dict):
            return
        append_one = evt.pop("append_result", None)
        if append_one is not None:
            _append_repair_result(append_one)
        # append 后再写其它字段，避免 results 被覆盖
        fields = {k: v for k, v in evt.items() if k != "results"}
        _set_repair_job(**fields)

    def _worker() -> None:
        try:
            repair_many(
                work,
                do_reauth=do_reauth,
                max_workers=workers,
                progress_cb=_progress,
            )
        except Exception as exc:
            logger.exception("[号池监控] 后台修复任务失败")
            _set_repair_job(
                status="error",
                phase="error",
                message=f"{type(exc).__name__}: {exc}",
                finished_at=_now(),
            )

    th = threading.Thread(target=_worker, name=f"pool-monitor-repair-{job_id}", daemon=True)
    th.start()
    snap = _repair_job_snapshot() or {}
    snap["ok"] = True
    snap["started"] = True
    return snap


# ============================================================
# 自动巡检：扫描 → 删废 → 补跑(≤10) → 日志 / 按日统计
# ============================================================

from pathlib import Path

_AUTO_DIR = Path(__file__).resolve().parent.parent / "data" / "pool_monitor"
_AUTO_STATE_PATH = _AUTO_DIR / "auto_state.json"
_AUTO_LOG_PATH = _AUTO_DIR / "auto.log"
_AUTO_RUNS_DIR = _AUTO_DIR / "runs"
_AUTO_DAILY_DIR = _AUTO_DIR / "daily"

_DEFAULT_INTERVAL_SEC = 15 * 60
_DEFAULT_MAX_REAUTH = 10
_DEFAULT_MAX_WORKERS = 3
_AUTO_RUNS_KEEP = 60
_AUTO_LOG_MAX_BYTES = 2 * 1024 * 1024

_AUTO_LOCK = threading.RLock()
_AUTO_THREAD: threading.Thread | None = None
_AUTO_STOP = threading.Event()
_AUTO_RUNNING = False
_AUTO_WAKE = threading.Event()  # 打开开关 / run_now 时打断 sleep


def _auto_default_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "interval_sec": _DEFAULT_INTERVAL_SEC,
        "max_reauth_per_cycle": _DEFAULT_MAX_REAUTH,
        "max_workers": _DEFAULT_MAX_WORKERS,
        "delete_local_dead_in_pool": True,
        "updated_at": None,
        "last_run": None,
        "next_run_at": None,
        "current_run": None,
    }


def _ensure_auto_dirs() -> None:
    _AUTO_DIR.mkdir(parents=True, exist_ok=True)
    _AUTO_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _AUTO_DAILY_DIR.mkdir(parents=True, exist_ok=True)


def _read_json_file(path: Path, default: Any = None) -> Any:
    try:
        if not path.is_file():
            return default
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return default
        return json.loads(raw)
    except Exception as exc:
        logger.warning("[号池自动巡检] 读文件失败 %s: %s", path, exc)
        return default


def _write_json_file(path: Path, data: Any) -> None:
    _ensure_auto_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_auto_state() -> dict[str, Any]:
    """读取持久化开关与调度状态。"""
    base = _auto_default_state()
    with _AUTO_LOCK:
        data = _read_json_file(_AUTO_STATE_PATH, default=None)
        if not isinstance(data, dict):
            return dict(base)
        out = dict(base)
        out.update(data)
        out["enabled"] = bool(out.get("enabled"))
        try:
            out["interval_sec"] = max(60, int(out.get("interval_sec") or _DEFAULT_INTERVAL_SEC))
        except Exception:
            out["interval_sec"] = _DEFAULT_INTERVAL_SEC
        try:
            out["max_reauth_per_cycle"] = max(0, min(50, int(
                out.get("max_reauth_per_cycle") if out.get("max_reauth_per_cycle") is not None
                else _DEFAULT_MAX_REAUTH
            )))
        except Exception:
            out["max_reauth_per_cycle"] = _DEFAULT_MAX_REAUTH
        try:
            out["max_workers"] = max(1, min(10, int(out.get("max_workers") or _DEFAULT_MAX_WORKERS)))
        except Exception:
            out["max_workers"] = _DEFAULT_MAX_WORKERS
        out["delete_local_dead_in_pool"] = bool(out.get("delete_local_dead_in_pool", True))
        out["running"] = bool(_AUTO_RUNNING)
        return out


def save_auto_state(patch: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    """合并写回 auto_state.json。"""
    with _AUTO_LOCK:
        cur = load_auto_state()
        # load 会带 running，不落盘
        cur.pop("running", None)
        if patch:
            cur.update(patch)
        if fields:
            cur.update(fields)
        cur["updated_at"] = _now()
        cur.pop("running", None)
        _write_json_file(_AUTO_STATE_PATH, cur)
        cur["running"] = bool(_AUTO_RUNNING)
        return cur


def _append_auto_log(line: str) -> None:
    try:
        _ensure_auto_dirs()
        msg = f"{_now()} {line.rstrip()}\n"
        with open(_AUTO_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg)
        # 简单截断
        if _AUTO_LOG_PATH.stat().st_size > _AUTO_LOG_MAX_BYTES:
            raw = _AUTO_LOG_PATH.read_text(encoding="utf-8", errors="ignore")
            keep = raw[-(_AUTO_LOG_MAX_BYTES // 2):]
            _AUTO_LOG_PATH.write_text(keep, encoding="utf-8")
    except Exception as exc:
        logger.debug("[号池自动巡检] 写 log 失败: %s", exc)


def _prune_auto_runs(keep: int = _AUTO_RUNS_KEEP) -> None:
    try:
        files = sorted(_AUTO_RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[max(1, int(keep)):]:
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _daily_default(date_key: str | None = None) -> dict[str, Any]:
    return {
        "date": date_key or _today_key(),
        "runs": 0,
        "skipped": 0,
        "scan_ours": 0,
        "scan_rt_bad": 0,
        "scan_dead": 0,
        "scan_repairable": 0,
        "scan_need_material": 0,
        "reauth_attempted": 0,
        "reauth_success": 0,
        "reauth_failed": 0,
        "dead_marked": 0,
        "pool_deleted": 0,
        "residual_pool_deleted": 0,
        "need_material_seen": 0,
        "errors": 0,
        "updated_at": None,
    }


def _merge_daily_stats(delta: dict[str, Any]) -> dict[str, Any]:
    date_key = str(delta.get("date") or _today_key())
    path = _AUTO_DAILY_DIR / f"{date_key}.json"
    with _AUTO_LOCK:
        cur = _read_json_file(path, default=None)
        if not isinstance(cur, dict):
            cur = _daily_default(date_key)
        else:
            base = _daily_default(date_key)
            base.update(cur)
            cur = base
        for k, v in (delta or {}).items():
            if k in ("date", "updated_at"):
                continue
            try:
                cur[k] = int(cur.get(k) or 0) + int(v or 0)
            except Exception:
                pass
        cur["date"] = date_key
        cur["updated_at"] = _now()
        _write_json_file(path, cur)
        return cur


def collect_auto_targets(
    items: list[dict] | None,
    *,
    max_reauth: int = _DEFAULT_MAX_REAUTH,
    delete_local_dead_in_pool: bool = True,
) -> dict[str, list[dict]]:
    """从扫描结果收集自动处理目标。

    Returns:
      {
        dead: [{email, pool_id, action: mark_dead, reason}],
        reauth: [{email, pool_id, action: reauth_repush}],  # 已截断
        residual_delete: [{email, pool_id}],  # 本地已废仍在号池
        need_material: [...],  # 仅统计，不自动处理
        reauth_total: int,  # 截断前可补跑总数
      }
    """
    dead: list[dict] = []
    reauth: list[dict] = []
    residual: list[dict] = []
    need_mat: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        email = str(it.get("email") or "").strip()
        if not email:
            continue
        action = str(it.get("action") or "")
        pid = it.get("pool_id")
        try:
            pid_i = int(pid) if pid is not None and str(pid).strip() != "" else None
        except Exception:
            pid_i = None
        if action == "mark_dead":
            dead.append({
                "email": email,
                "pool_id": pid_i,
                "action": "mark_dead",
                "reason": str(it.get("reason") or "号池自动巡检标废"),
            })
        elif action == "reauth_repush":
            reauth.append({
                "email": email,
                "pool_id": pid_i,
                "action": "reauth_repush",
            })
        elif action == "skip_dead" and delete_local_dead_in_pool and pid_i is not None:
            residual.append({"email": email, "pool_id": pid_i})
        elif action == "need_material":
            need_mat.append({"email": email, "pool_id": pid_i, "action": "need_material"})

    def _sort_key(t: dict) -> tuple:
        pid = t.get("pool_id")
        try:
            pid_n = int(pid) if pid is not None else 10**12
        except Exception:
            pid_n = 10**12
        return (pid_n, str(t.get("email") or "").lower())

    dead.sort(key=_sort_key)
    reauth.sort(key=_sort_key)
    residual.sort(key=_sort_key)
    reauth_total = len(reauth)
    cap = max(0, int(max_reauth))
    reauth_capped = reauth[:cap]
    return {
        "dead": dead,
        "reauth": reauth_capped,
        "residual_delete": residual,
        "need_material": need_mat,
        "reauth_total": reauth_total,
        "reauth_capped": max(0, reauth_total - len(reauth_capped)),
    }


def _is_manual_repair_busy() -> bool:
    job = get_repair_job() or {}
    return str(job.get("status") or "") == "running"


def run_auto_cycle(*, force: bool = False) -> dict[str, Any]:
    """执行一轮自动巡检：扫描 → 删废 → 补跑。

    force=True 时忽略 enabled（用于手动「立即跑一轮」）。
    """
    global _AUTO_RUNNING
    state = load_auto_state()
    if not force and not state.get("enabled"):
        return {"ok": True, "status": "disabled", "message": "自动巡检未开启"}

    with _AUTO_LOCK:
        if _AUTO_RUNNING:
            out = {
                "ok": True,
                "status": "skipped",
                "reason": "busy_auto",
                "message": "上一轮自动巡检仍在运行",
                "started_at": _now(),
                "finished_at": _now(),
            }
            _append_auto_log(f"[skip] busy_auto")
            _merge_daily_stats({"skipped": 1})
            return out
        if _is_manual_repair_busy():
            out = {
                "ok": True,
                "status": "skipped",
                "reason": "busy_manual_repair",
                "message": "手动修复任务进行中，本轮跳过",
                "started_at": _now(),
                "finished_at": _now(),
            }
            _append_auto_log(f"[skip] busy_manual_repair")
            _merge_daily_stats({"skipped": 1})
            # 延后 2 分钟再试，避免连 skip
            try:
                from datetime import timedelta
                nxt = (datetime.now() + timedelta(minutes=2)).isoformat(timespec="seconds")
            except Exception:
                nxt = _now()
            save_auto_state(last_run=out, next_run_at=nxt)
            return out
        _AUTO_RUNNING = True

    run_id = f"pm-auto-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{int(time.time()) % 10000}"
    started = _now()
    interval = int(state.get("interval_sec") or _DEFAULT_INTERVAL_SEC)
    max_reauth = int(state.get("max_reauth_per_cycle") if state.get("max_reauth_per_cycle") is not None else _DEFAULT_MAX_REAUTH)
    max_workers = int(state.get("max_workers") or _DEFAULT_MAX_WORKERS)
    delete_residual = bool(state.get("delete_local_dead_in_pool", True))

    run: dict[str, Any] = {
        "ok": True,
        "run_id": run_id,
        "status": "running",
        "started_at": started,
        "finished_at": None,
        "force": bool(force),
        "config": {
            "max_reauth_per_cycle": max_reauth,
            "max_workers": max_workers,
            "delete_local_dead_in_pool": delete_residual,
            "interval_sec": interval,
        },
        "scan_summary": None,
        "targets": None,
        "residual_results": [],
        "repair_results": [],
        "summary": {},
        "error": None,
        "message": "running",
    }
    save_auto_state(current_run={"run_id": run_id, "started_at": started, "status": "running"})
    _append_auto_log(f"[start] {run_id} force={bool(force)} max_reauth={max_reauth}")

    try:
        scan = scan_pool(include_ok=False)
        scan_summary = dict(scan.get("summary") or {})
        run["scan_summary"] = scan_summary
        run["scanned_at"] = scan.get("scanned_at")
        run["group_id"] = scan.get("group_id")

        picked = collect_auto_targets(
            scan.get("items") or [],
            max_reauth=max_reauth,
            delete_local_dead_in_pool=delete_residual,
        )
        run["targets"] = {
            "dead": len(picked["dead"]),
            "reauth": len(picked["reauth"]),
            "reauth_total": picked["reauth_total"],
            "reauth_capped": picked["reauth_capped"],
            "residual_delete": len(picked["residual_delete"]),
            "need_material": len(picked["need_material"]),
            "reauth_emails": [t.get("email") for t in picked["reauth"]],
            "dead_emails": [t.get("email") for t in picked["dead"]],
        }
        _append_auto_log(
            f"[scan] {run_id} ours={scan_summary.get('ours')} rt_bad={scan_summary.get('rt_bad')} "
            f"dead={scan_summary.get('dead')} repairable={scan_summary.get('repairable')} "
            f"→ auto dead={len(picked['dead'])} reauth={len(picked['reauth'])}/"
            f"{picked['reauth_total']} residual={len(picked['residual_delete'])}"
        )

        residual_results: list[dict] = []
        residual_deleted = 0
        for t in picked["residual_delete"]:
            pid = t.get("pool_id")
            email = t.get("email")
            try:
                res = delete_pool_account(int(pid))
            except Exception as exc:
                res = {"ok": False, "pool_id": pid, "error": f"{type(exc).__name__}: {exc}"}
            res = dict(res)
            res["email"] = email
            res["status"] = "residual_pool_delete"
            residual_results.append(res)
            if res.get("ok") and res.get("deleted", True):
                residual_deleted += 1
        run["residual_results"] = residual_results

        # 先删废，再补跑（避免补跑耗时占用废号）
        repair_targets = list(picked["dead"]) + list(picked["reauth"])
        repair_out: dict[str, Any]
        if repair_targets:
            repair_out = repair_many(
                repair_targets,
                do_reauth=True,
                max_workers=max_workers,
            )
        else:
            repair_out = {
                "ok": True, "success": 0, "dead": 0, "failed": 0, "total": 0, "results": [],
            }
        run["repair_results"] = repair_out.get("results") or []
        run["repair"] = {
            "success": repair_out.get("success"),
            "dead": repair_out.get("dead"),
            "failed": repair_out.get("failed"),
            "total": repair_out.get("total"),
        }

        results = list(run["repair_results"])
        reauth_emails = {str(t.get("email") or "").lower() for t in picked["reauth"]}
        dead_emails = {str(t.get("email") or "").lower() for t in picked["dead"]}
        reauth_success = 0
        reauth_failed = 0
        dead_marked = 0
        pool_deleted = residual_deleted
        for r in results:
            em = str(r.get("email") or "").lower()
            if em in dead_emails or str(r.get("status") or "") == "dead":
                if r.get("ok") or str(r.get("status") or "") == "dead":
                    dead_marked += 1
                deleted = r.get("pool_deleted") or []
                if isinstance(deleted, list):
                    pool_deleted += len(deleted)
                continue
            if em in reauth_emails:
                if r.get("ok"):
                    reauth_success += 1
                else:
                    reauth_failed += 1

        summary = {
            "scan_ours": int(scan_summary.get("ours") or 0),
            "scan_rt_bad": int(scan_summary.get("rt_bad") or 0),
            "scan_dead": int(scan_summary.get("dead") or 0),
            "scan_repairable": int(scan_summary.get("repairable") or 0),
            "scan_need_material": int(scan_summary.get("need_material") or 0),
            "reauth_attempted": len(picked["reauth"]),
            "reauth_success": reauth_success,
            "reauth_failed": reauth_failed,
            "reauth_deferred": int(picked["reauth_capped"] or 0),
            "dead_marked": dead_marked,
            "pool_deleted": pool_deleted,
            "residual_pool_deleted": residual_deleted,
            "need_material_seen": len(picked["need_material"]),
        }
        run["summary"] = summary
        run["status"] = "done"
        run["message"] = (
            f"扫描本站{summary['scan_ours']}；补跑 {reauth_success}/{len(picked['reauth'])} 成功"
            f"（本轮上限{max_reauth}，余{picked['reauth_capped']}下轮）；"
            f"标废{dead_marked}；号池删除{pool_deleted}"
        )
        run["ok"] = True
        _append_auto_log(f"[done] {run_id} {run['message']}")

        daily_delta = {
            "date": _today_key(),
            "runs": 1,
            "scan_ours": summary["scan_ours"],
            "scan_rt_bad": summary["scan_rt_bad"],
            "scan_dead": summary["scan_dead"],
            "scan_repairable": summary["scan_repairable"],
            "scan_need_material": summary["scan_need_material"],
            "reauth_attempted": summary["reauth_attempted"],
            "reauth_success": summary["reauth_success"],
            "reauth_failed": summary["reauth_failed"],
            "dead_marked": summary["dead_marked"],
            "pool_deleted": summary["pool_deleted"],
            "residual_pool_deleted": summary["residual_pool_deleted"],
            "need_material_seen": summary["need_material_seen"],
        }
        daily = _merge_daily_stats(daily_delta)
        run["daily"] = daily

    except Exception as exc:
        logger.exception("[号池自动巡检] 周期失败")
        run["ok"] = False
        run["status"] = "error"
        run["error"] = f"{type(exc).__name__}: {exc}"
        run["message"] = run["error"]
        _append_auto_log(f"[error] {run_id} {run['error']}")
        _merge_daily_stats({"date": _today_key(), "runs": 1, "errors": 1})
    finally:
        finished = _now()
        run["finished_at"] = finished
        try:
            _ensure_auto_dirs()
            run_path = _AUTO_RUNS_DIR / f"{run_id}.json"
            # 结果可能很大，repair_results 截断每条 reason
            slim = dict(run)
            slim_results = []
            for r in (run.get("repair_results") or [])[:100]:
                if not isinstance(r, dict):
                    continue
                slim_results.append({
                    "ok": r.get("ok"),
                    "email": r.get("email"),
                    "status": r.get("status"),
                    "pool_id": r.get("pool_id"),
                    "error": (str(r.get("error") or "")[:200] or None),
                    "message": (str(r.get("message") or "")[:200] or None),
                    "pool_deleted": r.get("pool_deleted"),
                })
            slim["repair_results"] = slim_results
            residual_slim = []
            for r in (run.get("residual_results") or [])[:100]:
                if isinstance(r, dict):
                    residual_slim.append({
                        "ok": r.get("ok"), "email": r.get("email"),
                        "pool_id": r.get("pool_id"), "deleted": r.get("deleted"),
                        "error": (str(r.get("error") or "")[:120] or None),
                    })
            slim["residual_results"] = residual_slim
            _write_json_file(run_path, slim)
            _prune_auto_runs()
        except Exception as exc:
            logger.warning("[号池自动巡检] 写 run 文件失败: %s", exc)

        try:
            from datetime import timedelta
            nxt = (datetime.now() + timedelta(seconds=interval)).isoformat(timespec="seconds")
        except Exception:
            nxt = finished
        last_public = {
            "run_id": run_id,
            "status": run.get("status"),
            "started_at": run.get("started_at"),
            "finished_at": finished,
            "message": run.get("message"),
            "summary": run.get("summary") or {},
            "ok": run.get("ok"),
            "error": run.get("error"),
        }
        save_auto_state(last_run=last_public, next_run_at=nxt, current_run=None)
        with _AUTO_LOCK:
            _AUTO_RUNNING = False

    return run


def set_auto_enabled(
    enabled: bool,
    *,
    fire_immediately: bool = True,
    interval_sec: int | None = None,
    max_reauth_per_cycle: int | None = None,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """打开/关闭自动巡检（持久化）。打开时可立即跑一轮。"""
    patch: dict[str, Any] = {"enabled": bool(enabled)}
    if interval_sec is not None:
        patch["interval_sec"] = max(60, int(interval_sec))
    if max_reauth_per_cycle is not None:
        patch["max_reauth_per_cycle"] = max(0, min(50, int(max_reauth_per_cycle)))
    if max_workers is not None:
        patch["max_workers"] = max(1, min(10, int(max_workers)))

    if enabled:
        if fire_immediately:
            patch["next_run_at"] = datetime.now().isoformat(timespec="seconds")
        else:
            interval = int(patch.get("interval_sec") or load_auto_state().get("interval_sec") or _DEFAULT_INTERVAL_SEC)
            from datetime import timedelta
            patch["next_run_at"] = (datetime.now() + timedelta(seconds=interval)).isoformat(timespec="seconds")
    else:
        patch["next_run_at"] = None

    state = save_auto_state(patch)
    _append_auto_log(f"[switch] enabled={bool(enabled)} fire_immediately={bool(fire_immediately)}")
    ensure_auto_monitor_started()
    _AUTO_WAKE.set()

    if enabled and fire_immediately:
        # 后台立刻触发一轮，避免阻塞 API
        def _kick() -> None:
            try:
                # 稍等 state 落盘
                time.sleep(0.2)
                run_auto_cycle(force=False)
            except Exception:
                logger.exception("[号池自动巡检] 立即触发失败")
        threading.Thread(target=_kick, name="pool-monitor-auto-kick", daemon=True).start()

    return get_auto_status()


def trigger_auto_run_now() -> dict[str, Any]:
    """无论开关，立即后台跑一轮（force）。"""
    ensure_auto_monitor_started()

    def _kick() -> None:
        try:
            run_auto_cycle(force=True)
        except Exception:
            logger.exception("[号池自动巡检] run_now 失败")

    threading.Thread(target=_kick, name="pool-monitor-auto-now", daemon=True).start()
    return get_auto_status()


def get_auto_daily(date: str | None = None, *, days: int = 7) -> dict[str, Any]:
    """取单日或最近 N 日统计。"""
    _ensure_auto_dirs()
    if date:
        path = _AUTO_DAILY_DIR / f"{str(date).strip()}.json"
        data = _read_json_file(path, default=None)
        if not isinstance(data, dict):
            data = _daily_default(str(date).strip())
        return {"ok": True, "daily": data, "items": [data]}

    items: list[dict] = []
    files = sorted(_AUTO_DAILY_DIR.glob("*.json"), key=lambda p: p.name, reverse=True)
    for p in files[: max(1, min(60, int(days or 7)))]:
        data = _read_json_file(p, default=None)
        if isinstance(data, dict):
            items.append(data)
        else:
            items.append(_daily_default(p.stem))
    # 若今天还没有文件，补空壳
    today = _today_key()
    if not any(str(x.get("date")) == today for x in items):
        items.insert(0, _daily_default(today))
    return {"ok": True, "items": items, "daily": items[0] if items else _daily_default(today)}


def list_auto_runs(*, limit: int = 20) -> dict[str, Any]:
    """最近 run 摘要列表。"""
    _ensure_auto_dirs()
    lim = max(1, min(100, int(limit or 20)))
    files = sorted(_AUTO_RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:lim]
    items = []
    for p in files:
        data = _read_json_file(p, default=None)
        if not isinstance(data, dict):
            continue
        items.append({
            "run_id": data.get("run_id") or p.stem,
            "status": data.get("status"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "message": data.get("message"),
            "summary": data.get("summary") or {},
            "ok": data.get("ok"),
            "error": data.get("error"),
            "targets": data.get("targets"),
        })
    return {"ok": True, "items": items, "total": len(items)}


def get_auto_log_tail(*, lines: int = 80) -> dict[str, Any]:
    """文本日志尾部。"""
    n = max(1, min(500, int(lines or 80)))
    if not _AUTO_LOG_PATH.is_file():
        return {"ok": True, "lines": [], "text": ""}
    try:
        raw = _AUTO_LOG_PATH.read_text(encoding="utf-8", errors="ignore")
        parts = raw.splitlines()
        tail = parts[-n:]
        return {"ok": True, "lines": tail, "text": "\n".join(tail)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "lines": [], "text": ""}


def get_auto_status() -> dict[str, Any]:
    """页面用：开关 + 上次/下次 + 今日统计。"""
    state = load_auto_state()
    daily = get_auto_daily(days=1)
    today = (daily.get("daily") if isinstance(daily, dict) else None) or _daily_default()
    return {
        "ok": True,
        "enabled": bool(state.get("enabled")),
        "running": bool(state.get("running") or _AUTO_RUNNING),
        "interval_sec": int(state.get("interval_sec") or _DEFAULT_INTERVAL_SEC),
        "max_reauth_per_cycle": int(state.get("max_reauth_per_cycle") if state.get("max_reauth_per_cycle") is not None else _DEFAULT_MAX_REAUTH),
        "max_workers": int(state.get("max_workers") or _DEFAULT_MAX_WORKERS),
        "delete_local_dead_in_pool": bool(state.get("delete_local_dead_in_pool", True)),
        "next_run_at": state.get("next_run_at"),
        "last_run": state.get("last_run"),
        "current_run": state.get("current_run"),
        "updated_at": state.get("updated_at"),
        "today": today,
    }


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00").split("+")[0])
    except Exception:
        return None


def _auto_loop() -> None:
    logger.info("[号池自动巡检] 守护线程已启动")
    while not _AUTO_STOP.is_set():
        try:
            state = load_auto_state()
            if state.get("enabled"):
                next_at = _parse_iso(state.get("next_run_at"))
                now = datetime.now()
                if next_at is None:
                    # 已开启但没有 next：补一个立即执行点
                    save_auto_state(next_run_at=now.isoformat(timespec="seconds"))
                    next_at = now
                if now >= next_at and not _AUTO_RUNNING:
                    run_auto_cycle(force=False)
        except Exception:
            logger.exception("[号池自动巡检] 守护循环异常")
            try:
                _merge_daily_stats({"date": _today_key(), "errors": 1})
            except Exception:
                pass
        # 可被开关/立即执行打断
        _AUTO_WAKE.wait(timeout=5)
        _AUTO_WAKE.clear()
    logger.info("[号池自动巡检] 守护线程退出")


def ensure_auto_monitor_started() -> None:
    """确保守护线程在跑（create_app / 开关时调用）。"""
    global _AUTO_THREAD
    with _AUTO_LOCK:
        _ensure_auto_dirs()
        # 若无 state 文件，写默认关
        if not _AUTO_STATE_PATH.is_file():
            save_auto_state(_auto_default_state())
        if _AUTO_THREAD is not None and _AUTO_THREAD.is_alive():
            return
        _AUTO_STOP.clear()
        _AUTO_THREAD = threading.Thread(
            target=_auto_loop,
            name="pool-monitor-auto",
            daemon=True,
        )
        _AUTO_THREAD.start()
