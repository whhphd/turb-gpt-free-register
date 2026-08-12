# -*- coding: utf-8 -*-
"""导入已有账号行解析（供邮箱池/已注册账号导入 → Codex 补跑）。

支持格式（---- 或 ==== 分隔，优先）：
  1) 邮箱----取码地址[----accessToken][----totp]
  2) 邮箱----MFA密钥----取码地址[----accessToken]   ← 邮箱+2FA+接码（无登录密码）
  3) 邮箱----密码----2FA密钥[----accessToken]
  4) 邮箱----密码----clientId----refreshToken[----accessToken][----totp]  (Outlook)

也兼容：
  - 竖线 | 分隔
  - 中文描述里的「邮箱-密码-2FA」在能可靠识别时（邮箱含 @，末段像 Base32 TOTP）
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from core.totp_login import normalize_totp_secret

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _is_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(str(value or "").strip()))


def _is_url(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if low.startswith(("http://", "https://")):
        return True
    # 有些取码地址不带协议，但像 host/path
    if "://" in raw:
        try:
            p = urlparse(raw if "://" in raw else f"https://{raw}")
            return bool(p.netloc or p.path)
        except Exception:
            return False
    if "/" in raw and "." in raw.split("/")[0]:
        return True
    return False


def _looks_like_totp(value: str) -> bool:
    secret = normalize_totp_secret(value)
    # Base32 通常 >= 16；放宽到 8 兼容短 secret / otpauth 已规范化
    if len(secret) < 8:
        return False
    # 排除纯数字（更像密码）
    if secret.isdigit():
        return False
    return True


def _looks_like_client_id(value: str) -> bool:
    s = str(value or "").strip()
    # Azure app client id 多为 UUID
    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        s,
    ):
        return True
    # 有些是纯 hex 32
    if re.fullmatch(r"[0-9a-fA-F]{32}", s):
        return True
    return False


def _looks_like_refresh_token(value: str) -> bool:
    s = str(value or "").strip()
    # MS refresh token 通常很长
    return len(s) >= 40 and ("." in s or s.startswith("M.") or s.startswith("1.") or "/" in s or "_" in s)


def split_account_line(line: str) -> list[str]:
    """按优先级拆字段。"""
    raw = str(line or "").strip()
    if not raw or raw.startswith("#"):
        return []
    # 去掉常见前缀噪声
    raw = raw.strip().lstrip("\ufeff")
    if "----" in raw:
        parts = [p.strip() for p in raw.split("----")]
    elif "====" in raw:
        parts = [p.strip() for p in raw.split("====")]
    elif "|" in raw:
        parts = [p.strip() for p in raw.split("|")]
    elif "\t" in raw:
        parts = [p.strip() for p in raw.split("\t")]
    else:
        # 单横线：仅当「邮箱-密码-2FA」可可靠识别时使用
        # 策略：最后一个 @ 前是邮箱主体，邮箱后第一个 - 切开，再取最后一段当 2FA
        if "@" in raw and raw.count("-") >= 2:
            # 找到邮箱结尾：第一个看起来像 email 的前缀
            m = re.match(r"^([^@\s]+@[^@\s]+\.[^@\s\-]+)-(.*)$", raw)
            if m:
                email = m.group(1).strip()
                rest = m.group(2).strip()
                # rest = password-2FA，从右侧拆一次
                if "-" in rest:
                    pwd, totp = rest.rsplit("-", 1)
                    parts = [email, pwd.strip(), totp.strip()]
                else:
                    parts = [email, rest]
            else:
                parts = [raw]
        else:
            parts = [raw]
    return [p for p in parts if p != ""]


def detect_import_kind(parts: list[str]) -> str:
    """返回 generic_api / password_totp / outlook / unknown。"""
    if not parts or not _is_email(parts[0]):
        return "unknown"
    n = len(parts)
    if n == 1:
        return "unknown"
    # 2 段：邮箱 + 取码地址 或 邮箱 + 密码（无 2FA 则 unknown，补跑难用）
    if n == 2:
        if _is_url(parts[1]):
            return "generic_api"
        return "unknown"
    # 3 段
    if n == 3:
        if _is_url(parts[1]):
            # email----url----token/totp
            return "generic_api"
        # email----MFA----取码地址（无登录密码，靠邮箱 OTP + 可选 TOTP）
        if _looks_like_totp(parts[1]) and _is_url(parts[2]):
            return "generic_api"
        if _looks_like_totp(parts[2]) and not _looks_like_client_id(parts[1]) and not _is_url(parts[2]):
            return "password_totp"
        # email----password----clientId 缺 refresh → unknown
        return "unknown"
    # 4+ 段
    if n >= 4:
        # outlook: email pass client refresh
        if _looks_like_client_id(parts[2]) or _looks_like_refresh_token(parts[3]):
            return "outlook"
        # email----url----token----totp
        if _is_url(parts[1]):
            return "generic_api"
        # email----MFA----url----token
        if _looks_like_totp(parts[1]) and _is_url(parts[2]):
            return "generic_api"
        # email----password----totp----token
        if _looks_like_totp(parts[2]):
            return "password_totp"
        if _looks_like_totp(parts[3]) and not _looks_like_refresh_token(parts[3]):
            return "password_totp"
        return "outlook"
    return "unknown"


def parse_import_account_line(line: str, *, preferred_source: str | None = None) -> dict | None:
    """解析单行，返回记录 dict（含 kind 字段）或 None。"""
    parts = split_account_line(line)
    if len(parts) < 2 or not _is_email(parts[0]):
        return None

    pref = (preferred_source or "").strip().lower()
    if pref in ("", "auto", "all"):
        kind = detect_import_kind(parts)
    else:
        kind = pref
        # 用户强制类型时做基本校验
        if kind == "generic_api":
            # 允许 email----url 或 email----mfa----url
            has_url = _is_url(parts[1]) or (len(parts) >= 3 and _is_url(parts[2]))
            if not has_url:
                return None
        if kind == "outlook" and len(parts) < 4:
            # 允许把 3 段当 password_totp 兜底
            if detect_import_kind(parts) == "password_totp":
                kind = "password_totp"
            else:
                return None
        if kind == "password_totp" and len(parts) < 3:
            return None

    if kind == "unknown":
        return None

    email = parts[0].strip()
    rec: dict = {"email": email, "kind": kind}

    if kind == "generic_api":
        # A) email----url[----token/totp][----totp]
        # B) email----mfa----url[----accessToken]
        if _is_url(parts[1]):
            rec["code_url"] = parts[1].strip()
            if len(parts) >= 3:
                if _looks_like_totp(parts[2]) and len(parts) == 3:
                    rec["totp_secret"] = normalize_totp_secret(parts[2])
                else:
                    rec["access_token"] = parts[2].strip()
            if len(parts) >= 4:
                rec["totp_secret"] = normalize_totp_secret(parts[3]) or parts[3].strip()
        elif len(parts) >= 3 and _looks_like_totp(parts[1]) and _is_url(parts[2]):
            rec["totp_secret"] = normalize_totp_secret(parts[1])
            rec["code_url"] = parts[2].strip()
            if len(parts) >= 4:
                rec["access_token"] = parts[3].strip()
        else:
            return None
        if not rec.get("code_url"):
            return None
        rec["source"] = "generic_api"
        return rec

    if kind == "password_totp":
        password = parts[1].strip()
        totp = ""
        token = ""
        if len(parts) >= 3 and _looks_like_totp(parts[2]):
            totp = normalize_totp_secret(parts[2])
            if len(parts) >= 4:
                token = parts[3].strip()
        elif len(parts) >= 4 and _looks_like_totp(parts[3]):
            # email----password----xxx----totp
            totp = normalize_totp_secret(parts[3])
            token = parts[2].strip() if not _looks_like_client_id(parts[2]) else ""
        else:
            return None
        if not password or not totp:
            return None
        rec["password"] = password
        rec["totp_secret"] = totp
        if token:
            rec["access_token"] = token
        rec["source"] = "password_totp"
        return rec

    if kind == "outlook":
        if len(parts) < 4:
            return None
        rec["password"] = parts[1].strip()
        rec["client_id"] = parts[2].strip()
        rec["refresh_token"] = parts[3].strip()
        if len(parts) >= 5:
            rec["access_token"] = parts[4].strip()
        if len(parts) >= 6:
            rec["totp_secret"] = normalize_totp_secret(parts[5]) or parts[5].strip()
        rec["source"] = "outlook"
        return rec

    return None


def parse_import_text(text: str, *, preferred_source: str | None = None) -> tuple[list[dict], list[str]]:
    """解析多行文本。返回 (records, errors)。"""
    records: list[dict] = []
    errors: list[str] = []
    for idx, line in enumerate(str(text or "").splitlines(), start=1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        rec = parse_import_account_line(raw, preferred_source=preferred_source)
        if not rec:
            errors.append(f"第{idx}行无法识别: {raw[:80]}")
            continue
        records.append(rec)
    return records, errors
