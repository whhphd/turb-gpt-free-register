# -*- coding: utf-8 -*-
"""登录阶段 TOTP（Authenticator App）自动填写辅助。

用途：
    账号已开启 2FA 且本地保存了 totp_secret 时，在 Codex/登录流里
    自动识别验证器页面并用 pyotp 生成 6 位动态码填写。

不负责注册时 enroll 2FA（那是 account_export.setup_2fa）。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

# URL path 命中才倾向 TOTP（只看 path，不看 query！）
# 历史 bug：query 里 redirect_uri=...%2Fauth%2Fcallback 含字面量 "%2fa"，
# 会被裸子串 "2fa" 误判成 TOTP，导致 oauth/authorize 登录页直接报无 totp_secret。
_TOTP_URL_PATH_HINTS = (
    "factor-totp",
    "factor/totp",
    "/totp",
    "totp-challenge",
    "mfa/totp",
    "mfa-challenge",
    "two-factor",
    "two_factor",
    "/2fa",
    "2fa/",
    "2fa-",
    "-2fa",
    "authenticator",
    "authenticate-app",
    "auth-app",
    "/mfa",
    "mfa/",
)

# 页面文案命中
_TOTP_TEXT_HINTS = (
    "authenticator app",
    "authentication app",
    "authenticator",
    "enter the code from your app",
    "enter the code from your authenticator",
    "from your authenticator",
    "verification app",
    "authentication code",
    "google authenticator",
    "microsoft authenticator",
    "auth app",
    "two-factor",
    "two factor",
    "2-step verification",
    "2 step verification",
    "身份验证器",
    "验证器应用",
    "验证器 app",
    "动态口令",
    "認証アプリ",
    "認証システム",
)

# 邮箱 OTP 文案（用于排除误判）
_EMAIL_OTP_TEXT_HINTS = (
    "check your email",
    "sent a code to",
    "sent an email",
    "we emailed",
    "email address",
    "temporary chatgpt login code",
    "resend email",
    "resend the email",
    "email verification",
    "code we sent to",
    "enter the code from your email",
    "检查邮箱",
    "发送到你的邮箱",
    "重新发送电子邮件",
    "邮件验证",
)


def normalize_totp_secret(secret: str | None) -> str:
    """清洗 Base32 secret：去空格/连字符，统一大写。"""
    raw = str(secret or "").strip()
    if not raw:
        return ""
    # 兼容 otpauth://totp/...&secret=XXXX
    if raw.lower().startswith("otpauth://"):
        m = re.search(r"[?&]secret=([A-Za-z2-7=]+)", raw, re.I)
        if m:
            raw = m.group(1)
    cleaned = re.sub(r"[\s\-]+", "", raw).upper()
    # 仅保留 Base32 字符
    cleaned = re.sub(r"[^A-Z2-7=]", "", cleaned)
    return cleaned


def load_totp_secret(email: str, explicit: str | None = None) -> str | None:
    """优先用显式传入 secret，否则从已注册账号库读取。"""
    secret = normalize_totp_secret(explicit)
    if secret:
        return secret
    email = (email or "").strip()
    if not email:
        return None
    try:
        from core import db

        acc = db.get_account_by_email(email) or {}
        secret = normalize_totp_secret(acc.get("totp_secret"))
        if secret:
            return secret
    except Exception as exc:
        logger.debug("[TOTP] 从账号库读取 secret 失败：%s", str(exc)[:160])
    return None


def generate_totp_code(secret: str, *, wait_near_boundary: bool = True) -> str:
    """生成当前 6 位 TOTP；靠近周期边界时等下一秒，避免刚过期。"""
    import pyotp

    secret_n = normalize_totp_secret(secret)
    if not secret_n:
        raise ValueError("empty totp secret")
    totp = pyotp.TOTP(secret_n)
    if wait_near_boundary:
        remaining = int(totp.interval) - (int(time.time()) % int(totp.interval))
        if remaining <= 2:
            time.sleep(remaining + 0.15)
    code = str(totp.now()).strip()
    if not re.fullmatch(r"\d{6}", code):
        # 少数配置不是 6 位，仍返回原始值
        return code
    return code


def page_text_blob(url: str = "", title: str = "", text: str = "", inputs: Any = None) -> str:
    attrs = ""
    if inputs:
        try:
            parts = []
            for i in inputs or []:
                if not isinstance(i, dict):
                    continue
                parts.append(
                    " ".join(
                        str(i.get(k) or "")
                        for k in ("type", "name", "id", "autocomplete", "inputmode", "placeholder", "ariaLabel", "aria-label")
                    )
                )
            attrs = " ".join(parts)
        except Exception:
            attrs = ""
    return " ".join([str(url or ""), str(title or ""), str(text or ""), attrs]).lower()


def _url_path_lower(url: str) -> str:
    """只取 URL path（小写），避免 query 百分号编码产生假命中。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        from urllib.parse import urlparse, unquote

        parsed = urlparse(raw)
        # path + fragment；query 一律忽略
        path = unquote(parsed.path or "")
        frag = unquote(parsed.fragment or "")
        return f"{path}#{frag}".lower() if frag else path.lower()
    except Exception:
        # 回退：截掉 ? 后的 query
        base = raw.split("?", 1)[0]
        return base.lower()


def is_totp_challenge(url: str = "", title: str = "", text: str = "", inputs: Any = None) -> bool:
    """根据 URL path / 标题 / 正文判断是否为 Authenticator TOTP 挑战页。

    注意：绝不能对完整 URL（含 query）做裸子串匹配 "2fa"：
    oauth redirect_uri 里的 `%2Fauth` 会命中 `2fa`。
    """
    path_l = _url_path_lower(url)
    # 正文/标题/输入属性，不要把整段 URL query 拼进 blob
    blob = page_text_blob(url="", title=title, text=text, inputs=inputs)

    # 仍在 OAuth 授权入口：绝不是 TOTP 挑战页
    if "/oauth/authorize" in path_l or path_l.rstrip("/").endswith("/oauth/authorize"):
        return False

    url_hit = any(h in path_l for h in _TOTP_URL_PATH_HINTS)
    # 单独的 "/factor" 过宽，必须伴随 totp/mfa/2fa/authenticator
    if (not url_hit) and "/factor" in path_l:
        url_hit = any(x in path_l for x in ("totp", "mfa", "2fa", "authenticator", "two-factor", "two_factor"))

    text_hit = any(h in blob for h in _TOTP_TEXT_HINTS)
    email_hit = any(h in blob for h in _EMAIL_OTP_TEXT_HINTS)
    # 登录入口常见文案，单独出现不能当 TOTP
    login_hit = any(
        h in blob
        for h in (
            "log in or sign up",
            "continue with email",
            "email address",
            "create your account",
            "sign up",
            "log in",
            "ログイン",
            "サインアップ",
            "メールアドレス",
        )
    )

    if url_hit and not email_hit:
        return True
    if text_hit and not email_hit and not login_hit:
        return True
    # 同时出现 authenticator + email 文案时：优先看更强信号
    if text_hit and email_hit:
        strong = any(
            h in blob
            for h in (
                "authenticator app",
                "authentication app",
                "from your authenticator",
                "身份验证器",
                "验证器应用",
                "認証アプリ",
            )
        )
        if strong:
            return True
    return False


def is_email_otp_challenge(url: str = "", title: str = "", text: str = "", inputs: Any = None) -> bool:
    """明确的邮箱 OTP 页（排除 TOTP）。"""
    if is_totp_challenge(url=url, title=title, text=text, inputs=inputs):
        return False
    url_l = str(url or "").lower()
    if "email-verification" in url_l:
        return True
    blob = page_text_blob(url=url, title=title, text=text, inputs=inputs)
    if any(h in blob for h in _EMAIL_OTP_TEXT_HINTS):
        return True
    return False


def driver_page_snapshot(driver) -> dict:
    """从 Selenium driver 抓取判定 TOTP 所需的页面快照。"""
    try:
        url = str(getattr(driver, "current_url", "") or "")
    except Exception:
        url = ""
    try:
        snap = driver.execute_script(
            r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
              type: el.getAttribute('type') || '',
              name: el.getAttribute('name') || '',
              id: el.id || '',
              autocomplete: el.getAttribute('autocomplete') || '',
              inputmode: el.getAttribute('inputmode') || '',
              placeholder: el.getAttribute('placeholder') || '',
              ariaLabel: el.getAttribute('aria-label') || ''
            }));
            return {
              url: location.href,
              title: document.title || '',
              text: (document.body && document.body.innerText || '').slice(0, 1500),
              inputs
            };
            """
        ) or {}
    except Exception as exc:
        snap = {"url": url, "title": "", "text": "", "inputs": [], "error": f"{type(exc).__name__}: {exc}"}
    if not snap.get("url"):
        snap["url"] = url
    return snap


def is_totp_page_driver(driver) -> bool:
    snap = driver_page_snapshot(driver)
    return is_totp_challenge(
        url=str(snap.get("url") or ""),
        title=str(snap.get("title") or ""),
        text=str(snap.get("text") or ""),
        inputs=snap.get("inputs"),
    )


def playwright_page_snapshot(page) -> dict:
    """从 Playwright page 抓取快照。"""
    try:
        url = str(page.url or "")
    except Exception:
        url = ""
    try:
        snap = page.evaluate(
            r"""
            () => {
              const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
              const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
                type: el.getAttribute('type') || '',
                name: el.getAttribute('name') || '',
                id: el.id || '',
                autocomplete: el.getAttribute('autocomplete') || '',
                inputmode: el.getAttribute('inputmode') || '',
                placeholder: el.getAttribute('placeholder') || '',
                ariaLabel: el.getAttribute('aria-label') || ''
              }));
              return {
                url: location.href,
                title: document.title || '',
                text: (document.body && document.body.innerText || '').slice(0, 1500),
                inputs
              };
            }
            """
        ) or {}
    except Exception as exc:
        try:
            text = (page.locator("body").inner_text(timeout=800) or "")[:1500]
        except Exception:
            text = ""
        snap = {"url": url, "title": "", "text": text, "inputs": [], "error": f"{type(exc).__name__}: {exc}"}
    if not snap.get("url"):
        snap["url"] = url
    return snap


def is_totp_page_playwright(page) -> bool:
    snap = playwright_page_snapshot(page)
    return is_totp_challenge(
        url=str(snap.get("url") or ""),
        title=str(snap.get("title") or ""),
        text=str(snap.get("text") or ""),
        inputs=snap.get("inputs"),
    )
