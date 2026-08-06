# -*- coding: utf-8 -*-
"""通过 RoxyBrowser 指纹浏览器执行 Codex OAuth 授权。"""
from __future__ import annotations

import logging
import random
import time
from contextvars import ContextVar
from urllib.parse import urlparse

from config import roxybrowser as _roxy_cfg
from core.email_provider import wait_for_otp
from core.humanize import delay as human_delay
from core import sms_provider
from core.openai_auth import AccountUnusableError, detect_account_unusable_response_body
from core.roxybrowser_client import RoxyBrowserClient
from core.roxy_registration import (
    _build_driver,
    _center_browser_window,
    _click_any,
    _click_continue,
    _find_any,
    _maybe_accept,
    _type_any,
    _type_email_address,
    _submit_email_step,
    _click_email_entry_option,
    _type_otp,
    _clear_otp_inputs,
    _email_otp_page_state,
    _is_email_verification_page,
    _is_login_password_page,
    _click_passwordless_signup_if_present,
)
from core.totp_login import (
    generate_totp_code,
    is_totp_page_driver,
    load_totp_secret,
)
from core.phone_utils import (
    classify_phone_failure_reason,
    country_dial_matches,
    dial_to_iso_candidates,
    digits_only,
    extract_option_dial_code,
    guess_dial_code,
    national_digits,
    normalize_e164,
    phone_visible_matches_expected,
)

_base_logger = logging.getLogger(__name__)
_CODEX_BROWSER_KIND: ContextVar[str] = ContextVar("codex_browser_kind", default="Roxy")


def _codex_prefix() -> str:
    return f"[Codex][{_CODEX_BROWSER_KIND.get()}]"


def _codex_driver_name() -> str:
    return _CODEX_BROWSER_KIND.get()


def _detect_browser_kind(opened=None) -> str:
    try:
        raw = getattr(opened, "raw", None) or {}
        if isinstance(raw, dict) and str(raw.get("driver") or "").lower().startswith("cloak"):
            return "Cloak"
    except Exception:
        pass
    return "Roxy"


class _CodexLogger:
    """把流程内部统一占位前缀替换成当前真实浏览器类型。"""
    def __init__(self, base):
        self._base = base

    def _msg(self, msg):
        return str(msg).replace("[Codex][Browser]", _codex_prefix())

    def debug(self, msg, *args, **kwargs):
        return self._base.debug(self._msg(msg), *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        return self._base.info(self._msg(msg), *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        return self._base.warning(self._msg(msg), *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        return self._base.error(self._msg(msg), *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        return self._base.exception(self._msg(msg), *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._base, name)


logger = _CodexLogger(_base_logger)


def _is_callback_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return (
        parsed.scheme in ("http", "https")
        and parsed.hostname in ("localhost", "127.0.0.1")
        and parsed.port == 1455
        and parsed.path == "/auth/callback"
    )


def _extract_callback_url_from_page(driver) -> str:
    """从当前页面提取 OAuth callback URL。

    浏览器跳转到 http://localhost:1455/auth/callback?... 时，本地没有服务监听会显示
    chrome-error://chromewebdata/。地址栏可能变成 chrome-error，但 Chromium 的
    performance navigation entry 仍保留原始 callback URL，可直接提取后提交 CPA。
    """
    try:
        current = str(driver.current_url or "")
        if _is_callback_url(current):
            return current
    except Exception:
        pass
    try:
        urls = driver.execute_script(r"""
        const out = [];
        const push = v => { if (v && typeof v === 'string') out.push(v); };
        try { push(location.href); } catch (e) {}
        try { push(document.URL); } catch (e) {}
        try { push(document.documentURI); } catch (e) {}
        try { for (const e of performance.getEntriesByType('navigation')) push(e.name); } catch (e) {}
        try { for (const e of performance.getEntries()) push(e.name); } catch (e) {}
        return [...new Set(out)];
        """) or []
        for url in urls:
            if _is_callback_url(str(url)):
                logger.info("[Codex][Browser] 已从浏览器性能记录提取 callback URL：%s", str(url)[:160])
                return str(url)
    except Exception as exc:
        logger.debug("[Codex][Browser] 从页面提取 callback URL 失败：%s", exc)
    return ""


def _extract_callback_url_from_any_window(driver) -> str:
    found = _extract_callback_url_from_page(driver)
    if found:
        return found
    try:
        for handle in list(getattr(driver, "window_handles", []) or []):
            try:
                driver.switch_to.window(handle)
                found = _extract_callback_url_from_page(driver)
                if found:
                    return found
            except Exception:
                continue
    except Exception:
        pass
    return ""


def _wait_for_callback(driver, timeout: int | None = None) -> str:
    end = time.time() + (timeout or int(_roxy_cfg.ROXY_CODEX_CALLBACK_TIMEOUT))
    last_url = ""
    while time.time() < end:
        try:
            current = str(driver.current_url or "")
            if current != last_url:
                logger.debug("[Codex][Browser] 当前 URL: %s", current)
                last_url = current
            callback = _extract_callback_url_from_any_window(driver)
            if callback:
                return callback
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"等待 Codex callback 超时，最后 URL={last_url}")


def _click_if_present(driver, selectors: list[str], timeout: int = 3) -> bool:
    try:
        _click_any(driver, selectors, timeout=timeout)
        return True
    except Exception:
        return False


def _account_password(email: str) -> str:
    """从已注册账号库读取登录密码（导入的 邮箱----密码----2FA 会写入）。"""
    try:
        from core import db
        acc = db.get_account_by_email(email) or {}
        return str(acc.get("password") or "").strip()
    except Exception:
        return ""


def _fill_login_password_if_present(driver, email: str, *, timeout: int = 12) -> bool:
    """若当前是登录密码页且账号有密码，则填写并提交。成功离开密码页返回 True。"""
    password = _account_password(email)
    if not password:
        return False
    end = time.time() + max(2, int(timeout))
    while time.time() < end:
        if not _is_login_password_page(driver):
            if (
                _is_email_verification_page(driver)
                or is_totp_page_driver(driver)
                or _codex_page_past_email_login(driver)
            ):
                return True
            time.sleep(0.3)
            continue
        try:
            filled = driver.execute_script(
                r"""
                const pwd = String(arguments[0] || '');
                const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                const inputs = [...document.querySelectorAll('input')].filter(visible);
                const box = inputs.find(el => (el.type || '').toLowerCase() === 'password')
                  || inputs.find(el => (el.autocomplete || '').toLowerCase() === 'current-password')
                  || inputs.find(el => /password/i.test(el.name || el.id || el.getAttribute('aria-label') || ''));
                if (!box) return {ok:false, reason:'no_password_input'};
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                box.focus();
                if (setter) setter.call(box, ''); else box.value = '';
                box.dispatchEvent(new Event('input', {bubbles:true}));
                if (setter) setter.call(box, pwd); else box.value = pwd;
                box.dispatchEvent(new Event('input', {bubbles:true}));
                box.dispatchEvent(new Event('change', {bubbles:true}));
                return {ok:true, valueLen: (box.value || '').length};
                """,
                password,
            ) or {}
        except Exception as exc:
            filled = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        if not filled.get("ok"):
            time.sleep(0.4)
            continue
        logger.info("[Codex][Browser] 已填写登录密码（导入账号密码），准备提交")
        human_delay("form")
        if not _submit_code_form(driver):
            try:
                driver.execute_script(
                    r"""
                    const form = document.querySelector('form');
                    if (!form) return false;
                    if (typeof form.requestSubmit === 'function') form.requestSubmit();
                    else form.submit();
                    return true;
                    """
                )
            except Exception:
                pass
        wait_end = time.time() + 12
        while time.time() < wait_end:
            if not _is_login_password_page(driver):
                logger.info(
                    "[Codex][Browser] 密码提交后页面：url=%s",
                    str(getattr(driver, "current_url", "") or "-")[:180],
                )
                return True
            time.sleep(0.4)
        logger.warning("[Codex][Browser] 密码提交后仍停在密码页")
        return False
    return False


def _maybe_click_passwordless_after_email(driver, email: str, timeout: int = 18) -> None:
    """
    Codex OAuth 提交邮箱后也可能跳到 /log-in/password 或 /create-account/password。
    - 账号有导入密码：优先填密码登录（再走 TOTP）
    - 否则点“使用一次性验证码”进入邮箱 OTP
    """
    end = time.time() + timeout
    last_url = ""
    clicked = False
    while time.time() < end:
        try:
            if _is_email_verification_page(driver) or is_totp_page_driver(driver):
                if clicked:
                    logger.info("[Codex][Browser] 已进入邮箱 OTP/TOTP 页")
                return
            url = str(driver.current_url or "")
            if url != last_url:
                logger.info("[Codex][Browser] 提交邮箱后检测密码/OTP 跳转：url=%s", url or "-")
                last_url = url
            lower = url.lower()
            if any(x in lower for x in ("phone", "workspace", "consent", "localhost:1455")):
                return
            # 有密码：优先密码登录
            if _is_login_password_page(driver) and _account_password(email):
                if _fill_login_password_if_present(driver, email, timeout=10):
                    # 密码后常见 TOTP
                    _fill_totp_if_present(driver, email, timeout=10)
                    return
            if "/password" in lower or "auth.openai.com" in lower:
                if _account_password(email) and _is_login_password_page(driver):
                    continue
                result = _click_passwordless_signup_if_present(driver)
                if result.get("ok"):
                    clicked = True
                    logger.info("[Codex][Browser] 已点击一次性验证码入口：email=%s detail=%s", email, result)
                    human_delay("form")
                    continue
        except Exception as exc:
            logger.debug("[Codex][Browser] 密码页一次性验证码入口探测失败：%s", str(exc)[:140])
        time.sleep(0.5)
    if clicked:
        logger.info("[Codex][Browser] 已点击一次性验证码入口，未立即检测到 OTP 页，继续后续 OTP 轮询")


def _wait_for_otp_input(driver, timeout: int = 30, email: str | None = None) -> None:
    """验证码已收到但 OTP 输入框可能尚未出现（点完一次性验证码后常有中间页/延迟渲染）。

    等待期间若仍停留在登录密码页：
      - 有导入密码 → 填密码
      - 否则补点一次性验证码入口（最多 2 次、间隔 6s）
    """
    end = time.time() + timeout
    passwordless_retries = 0
    while time.time() < end:
        if _is_email_verification_page(driver) or is_totp_page_driver(driver):
            return
        if _is_login_password_page(driver):
            if email and _account_password(email):
                if _fill_login_password_if_present(driver, email, timeout=8):
                    return
            if passwordless_retries < 2:
                passwordless_retries += 1
                result = _click_passwordless_signup_if_present(driver)
                if result.get("ok"):
                    logger.info("[Codex][Browser] 仍停留登录密码页，补点一次性验证码入口：%s", result.get("reason"))
                    human_delay("form")
                time.sleep(6)
                continue
        time.sleep(0.8)
    state = _email_otp_page_state(driver)
    logger.warning(
        "[Codex][Browser] 等待 OTP 输入框超时，页面 url=%s inputs=%s buttons=%s 文本前300字=%s",
        str(state.get("url") or ""),
        len(state.get("inputs") or []),
        [(b.get("text") or "")[:24] for b in (state.get("buttons") or [])][:8],
        str(state.get("text") or "")[:300],
    )
    raise RuntimeError("等待 OTP 输入框超时，页面未出现验证码输入框")


def _codex_page_past_email_login(driver) -> bool:
    """已经过了邮箱登录步骤（OTP/TOTP/手机/consent/callback）。"""
    try:
        url = str(driver.current_url or "").lower()
    except Exception:
        url = ""
    if _is_callback_url(url):
        return True
    if any(x in url for x in (
        "email-verification",
        "add-phone",
        "phone-verification",
        "/phone",
        "consent",
        "workspace",
        "about-you",
        "factor-totp",
        "/totp",
        "authenticator",
        "two-factor",
        "/mfa",
    )):
        return True
    try:
        if _is_email_verification_page(driver):
            return True
    except Exception:
        pass
    try:
        if is_totp_page_driver(driver):
            return True
    except Exception:
        pass
    return False


def _codex_page_still_on_login(driver) -> bool:
    """仍停在登录/授权入口，需要邮箱登录。"""
    try:
        url = str(driver.current_url or "").lower()
    except Exception:
        url = ""
    if _codex_page_past_email_login(driver):
        return False
    if any(x in url for x in ("/oauth/authorize", "/log-in", "/login", "/create-account", "identifier")):
        return True
    # URL 异常时：没有邮箱框也不在后续页，当登录卡住
    return True


def _ensure_email_filled_and_submitted(driver, email: str, auth_url: str, *, max_rounds: int = 3) -> None:
    """确保完成邮箱填写并提交；失败时重开授权页重试，禁止静默跳过。"""
    last_exc: Exception | None = None
    for round_no in range(1, max_rounds + 1):
        if _codex_page_past_email_login(driver):
            logger.info(
                "[Codex][Browser] 当前已在登录后续步骤，无需再填邮箱：url=%s",
                str(getattr(driver, "current_url", "") or "-"),
            )
            return

        try:
            # 先点邮箱入口（Continue with email），再填
            if _click_email_entry_option(driver):
                logger.info("[Codex][Browser] 已点击邮箱登录入口（第 %s/%s 轮）", round_no, max_rounds)
                human_delay("form")
            _type_email_address(driver, email, timeout=14)
            logger.info("[Codex][Browser] 已填写邮箱：%s（第 %s/%s 轮）", email, round_no, max_rounds)
            human_delay("form")
            _submit_email_step(driver)
            logger.info("[Codex][Browser] 已提交邮箱，等待邮箱 OTP 页面")
            _maybe_click_passwordless_after_email(driver, email, timeout=18)
            # 提交后若仍死在 log-in 且没有 OTP，下一轮重开
            if _codex_page_past_email_login(driver) or _is_email_verification_page(driver) or _is_login_password_page(driver):
                return
            # 给页面一点跳转时间
            end = time.time() + 8
            while time.time() < end:
                if (
                    _codex_page_past_email_login(driver)
                    or _is_email_verification_page(driver)
                    or _is_login_password_page(driver)
                ):
                    return
                time.sleep(0.5)
            # 若已离开 authorize/log-in，也算推进
            url = str(getattr(driver, "current_url", "") or "").lower()
            if url and not any(x in url for x in ("/oauth/authorize",)):
                return
            raise RuntimeError(
                f"提交邮箱后仍停在登录入口：url={getattr(driver, 'current_url', '') or '-'}"
            )
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "[Codex][Browser] 邮箱登录第 %s/%s 轮失败：%s: %s",
                round_no,
                max_rounds,
                type(exc).__name__,
                str(exc)[:180],
            )
            if round_no >= max_rounds:
                break
            # 重开授权地址再试，避免卡在半残页面
            try:
                logger.info("[Codex][Browser] 重新打开授权地址重试邮箱登录")
                driver.get(auth_url)
                human_delay("navigate")
                _maybe_accept(driver)
                time.sleep(1.0)
            except Exception as re_exc:
                logger.warning("[Codex][Browser] 重开授权地址失败：%s", str(re_exc)[:140])

    # 最后再判断一次：若已进入后续页则放过
    if _codex_page_past_email_login(driver):
        return
    state_url = str(getattr(driver, "current_url", "") or "-")
    raise RuntimeError(
        f"登录页卡住：无法完成邮箱填写/提交（已重试 {max_rounds} 次），"
        f"最后 URL={state_url}；{type(last_exc).__name__ if last_exc else 'Error'}: "
        f"{str(last_exc)[:180] if last_exc else ''}"
    )


def _submit_code_form(driver) -> bool:
    return bool(_click_if_present(driver, [
        "button[type='submit']",
        "//button[contains(., 'Continue')]",
        "//button[contains(., '继续')]",
        "//button[contains(., 'Verify')]",
        "//button[contains(., '验证')]",
        "//button[contains(., 'Confirm')]",
        "//button[contains(., '确认')]",
    ], timeout=8))


def _wait_after_totp_submit(driver, timeout: int = 30) -> str:
    """提交 TOTP 后等待离开验证器页。"""
    end = time.time() + timeout
    last_url = ""
    while time.time() < end:
        try:
            url = str(driver.current_url or "")
            if url != last_url:
                logger.info("[Codex][Browser] TOTP 提交后等待跳转：url=%s", url)
                last_url = url
            if _is_callback_url(url):
                return "accepted"
            if _has_strict_add_phone_form(driver) or _is_phone_code_page(driver):
                return "accepted"
            if not is_totp_page_driver(driver):
                # 已离开 TOTP 页（consent / workspace / 其它）
                if "email-verification" not in url.lower() or _is_email_verification_page(driver):
                    # 若落到邮箱 OTP，也算进入下一步
                    return "accepted"
                return "accepted"
            state = _email_otp_page_state(driver)
            invalid = any(str(i.get("ariaInvalid") or "").lower() == "true" for i in (state.get("inputs") or []))
            errors = [str(x) for x in (state.get("errors") or []) if str(x).strip()]
            body_text = str(state.get("text") or "").lower()
            error_hit = any(x in body_text for x in (
                "invalid code", "incorrect code", "wrong code", "expired",
                "验证码错误", "验证码无效", "验证码已过期", "コードが正しく", "無効",
            ))
            if invalid or errors or error_hit:
                return "invalid"
        except Exception:
            pass
        time.sleep(0.4)
    return "invalid" if is_totp_page_driver(driver) else "accepted"


def _fill_totp_if_present(
    driver,
    email: str,
    *,
    totp_secret: str | None = None,
    timeout: int = 15,
    max_attempts: int = 3,
) -> bool:
    """若当前/短时内出现 TOTP 页，则用账号 totp_secret 自动填写。

    返回 True 表示已处理 TOTP；False 表示未出现 TOTP 页。
    出现 TOTP 页但没有 secret 时抛错，避免静默卡死。
    """
    secret = load_totp_secret(email, totp_secret)
    end = time.time() + max(1, int(timeout))
    saw_totp = False
    while time.time() < end:
        if is_totp_page_driver(driver):
            saw_totp = True
            break
        # 已明显进入后续页，无需等 TOTP
        try:
            url = str(driver.current_url or "").lower()
        except Exception:
            url = ""
        if _is_callback_url(url) or any(x in url for x in ("add-phone", "phone-verification", "consent", "workspace")):
            return False
        if _codex_page_past_email_login(driver) and not _is_email_verification_page(driver) and not is_totp_page_driver(driver):
            # past email 且不是 otp/totp
            if not is_totp_page_driver(driver):
                return False
        time.sleep(0.35)

    if not saw_totp:
        return False

    if not secret:
        # 带上页面快照，便于区分「真 2FA」和「检测误判」
        try:
            from core.totp_login import driver_page_snapshot
            snap = driver_page_snapshot(driver)
            snap_brief = {
                "url": str(snap.get("url") or "")[:240],
                "title": str(snap.get("title") or "")[:80],
                "text": str(snap.get("text") or "")[:240],
                "inputs": snap.get("inputs") or [],
            }
        except Exception as snap_exc:
            snap_brief = {"error": f"{type(snap_exc).__name__}: {snap_exc}"}
        raise RuntimeError(
            f"检测到 TOTP/Authenticator 页面，但账号 {email} 无 totp_secret，无法自动填写 2FA；"
            f"page={snap_brief}"
        )

    last_outcome = "invalid"
    used_codes: set[str] = set()
    for attempt in range(1, max_attempts + 1):
        code = generate_totp_code(secret, wait_near_boundary=True)
        # 同一 30s 窗口内重复提交无意义，稍等下一码
        if code in used_codes:
            time.sleep(1.2)
            code = generate_totp_code(secret, wait_near_boundary=True)
        used_codes.add(code)
        logger.info(
            "[Codex][Browser] 检测到 TOTP 页，自动填写动态码（第 %s/%s 次）code=%s",
            attempt,
            max_attempts,
            code,
        )
        try:
            _clear_otp_inputs(driver)
            _type_otp(driver, code)
            human_delay("otp_input")
            if _submit_code_form(driver):
                logger.info("[Codex][Browser] 已提交 TOTP")
            else:
                logger.info("[Codex][Browser] TOTP 未找到显式提交按钮，继续等待页面状态")
            last_outcome = _wait_after_totp_submit(driver, timeout=30)
            logger.info("[Codex][Browser] TOTP 提交后状态：%s", last_outcome)
            if last_outcome == "accepted":
                return True
        except Exception as exc:
            logger.warning(
                "[Codex][Browser] TOTP 填写失败（%s/%s）：%s: %s",
                attempt,
                max_attempts,
                type(exc).__name__,
                str(exc)[:180],
            )
            last_outcome = "error"
        if attempt < max_attempts:
            # 等下一个 30s 窗口再试
            time.sleep(1.5)
    raise RuntimeError(f"TOTP 连续 {max_attempts} 次未通过，最后状态={last_outcome}")


def _fill_email_and_otp(driver, email: str, otp_provider, auth_url: str, totp_secret: str | None = None) -> None:
    otp_after_ts = time.time()
    logger.info("[Codex][Browser] 打开授权地址")
    logger.info("[Codex][Browser] 完整授权地址: %s", auth_url)
    driver.get(auth_url)
    human_delay("navigate")
    logger.info("[Codex][Browser] 授权页加载完成，检查是否需要邮箱登录")
    _maybe_accept(driver)

    # 必须真正完成邮箱登录；禁止“找不到邮箱框就当已登录”静默跳过。
    # 那会直接卡在 log-in 等到 callback 超时。
    _ensure_email_filled_and_submitted(driver, email, auth_url, max_rounds=3)

    # 邮箱后直接进入 TOTP（少数账号策略）
    if _fill_totp_if_present(driver, email, totp_secret=totp_secret, timeout=4):
        return

    # 密码+2FA 账号：密码页填密码后常直接到 TOTP，无需邮箱 OTP
    if _account_password(email) and _is_login_password_page(driver):
        if _fill_login_password_if_present(driver, email, timeout=12):
            if _fill_totp_if_present(driver, email, totp_secret=totp_secret, timeout=15):
                return
            if _codex_page_past_email_login(driver) and not _is_email_verification_page(driver):
                return

    # 若邮箱提交后已直接到 consent/callback（少数会话），不再等 OTP。
    if (
        _codex_page_past_email_login(driver)
        and not _is_email_verification_page(driver)
        and not is_totp_page_driver(driver)
        and not _is_login_password_page(driver)
    ):
        logger.info(
            "[Codex][Browser] 邮箱步骤后已进入后续授权页，跳过 OTP：url=%s",
            str(getattr(driver, "current_url", "") or "-"),
        )
        return

    # 提交邮箱后不再执行任何全局“继续/授权/分支”兜底点击；后续只等待验证码页。
    # 避免页面已进入 OAuth consent 时误点授权按钮。

    # 密码+2FA 且没有邮箱接码：不要空等邮箱 OTP
    if _account_password(email) and load_totp_secret(email, totp_secret):
        # 再给密码/TOTP 一次机会
        if _is_login_password_page(driver):
            _fill_login_password_if_present(driver, email, timeout=10)
        if _fill_totp_if_present(driver, email, totp_secret=totp_secret, timeout=12):
            return
        # 若仍无邮箱取码能力，提示明确错误而不是干等 90s×3
        try:
            from core.email_provider import resolve_email_source
            src = resolve_email_source(email)
        except Exception:
            src = ""
        if src not in ("generic_api", "outlook", "gptmail", "cloudflare", "cloudflare_domain", "mailnest", "cloudmail"):
            if is_totp_page_driver(driver) or _is_login_password_page(driver):
                raise RuntimeError(
                    f"密码+2FA 账号登录未完成（当前页仍需验证），email={email} url="
                    f"{getattr(driver, 'current_url', '') or '-'}"
                )

    used_codes: set[str] = set()
    max_otp_attempts = 3

    def _restart_email_otp_flow(reason: str) -> None:
        """Codex Auth 上直接点 resend 可能触发服务端 500；这里改为重新打开授权地址并提交邮箱。"""
        nonlocal otp_after_ts
        logger.info("[Codex][Browser] 重新触发邮箱 OTP：%s", reason)
        otp_after_ts = time.time()
        try:
            _ensure_email_filled_and_submitted(driver, email, auth_url, max_rounds=2)
            logger.info("[Codex][Browser] 已重新提交邮箱触发 OTP")
        except Exception as exc:
            # 如果重进授权地址后已经停在验证码/下一步页面，就不要再强行提交。
            if not _is_email_verification_page(driver) and not is_totp_page_driver(driver):
                logger.warning("[Codex][Browser] 重新提交邮箱失败，继续按当前页面轮询：%s", str(exc)[:180])
            else:
                logger.info("[Codex][Browser] 重开授权后已在邮箱 OTP/TOTP 页面")
        human_delay("api")

    for otp_attempt in range(1, max_otp_attempts + 1):
        # 等待邮箱 OTP 期间若已切到 TOTP，优先填 TOTP
        if _fill_totp_if_present(driver, email, totp_secret=totp_secret, timeout=2):
            return

        logger.info("[Codex][Browser] 等待邮箱 OTP：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
        try:
            code = _wait_for_fresh_email_otp(
                otp_provider,
                email,
                after_ts=otp_after_ts,
                used_codes=used_codes,
                timeout=90,
            )
        except Exception as exc:
            if _fill_totp_if_present(driver, email, totp_secret=totp_secret, timeout=3):
                return
            if otp_attempt >= max_otp_attempts:
                raise
            logger.warning(
                "[Codex][Browser] 一直未收到邮箱 OTP，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                otp_attempt + 1,
                max_otp_attempts,
                type(exc).__name__,
                str(exc)[:180],
            )
            _restart_email_otp_flow("等待验证码超时，避免点击 resend 导致 500")
            continue
        used_codes.add(str(code))
        logger.info("[Codex][Browser] 邮箱 OTP 收到：%s", code)
        # 若页面其实是 TOTP，不要把邮箱码填进去
        if is_totp_page_driver(driver):
            if _fill_totp_if_present(driver, email, totp_secret=totp_secret, timeout=8):
                return
        _wait_for_otp_input(driver, timeout=30, email=email)
        if is_totp_page_driver(driver):
            if _fill_totp_if_present(driver, email, totp_secret=totp_secret, timeout=8):
                return
        _clear_otp_inputs(driver)
        _type_otp(driver, code)
        logger.info("[Codex][Browser] 已填写邮箱 OTP")
        human_delay("otp_input")
        _install_email_otp_validate_hook(driver)
        if _submit_code_form(driver):
            logger.info("[Codex][Browser] 已提交邮箱 OTP，等待后续授权/手机号页面")
        else:
            logger.info("[Codex][Browser] 未找到显式提交按钮，继续等待页面状态")

        outcome = _wait_after_email_otp_submit(driver, timeout=45)
        logger.info("[Codex][Browser] 邮箱 OTP 提交后状态：%s", outcome)
        if outcome == "accepted":
            # 邮箱 OTP 通过后常见下一步：TOTP
            _fill_totp_if_present(driver, email, totp_secret=totp_secret, timeout=12)
            return
        if str(outcome).startswith("deactivated:"):
            error_code = str(outcome).split(":", 1)[1] or "account_deactivated"
            raise AccountUnusableError(f"账号已废（{error_code}）", error_code=error_code)

        if otp_attempt >= max_otp_attempts:
            raise RuntimeError("Codex 邮箱验证码连续错误/过期，已达到最大重试次数")

        logger.warning(
            "[Codex][Browser] 邮箱验证码错误/过期或页面未跳转，准备重新发送并重新获取最新验证码（%s/%s）",
            otp_attempt + 1,
            max_otp_attempts,
        )
        _restart_email_otp_flow("验证码错误/过期或页面未跳转，避免点击 resend 导致 500")



def _wait_for_fresh_email_otp(otp_provider, email: str, after_ts: float, used_codes: set[str] | None = None, timeout: int = 90) -> str:
    """获取一个未提交过的邮箱 OTP。

    通用 API 邮箱的取码接口有时会先返回缓存旧码；验证码错误后重发时，
    这里会拒绝复用已失败的 code，持续轮询直到出现新 code 或超时。
    """
    used_codes = {str(x) for x in (used_codes or set()) if x}
    end = time.time() + timeout
    last_code = ""
    while True:
        code = str(otp_provider(email, after_ts=after_ts) or "").strip()
        if code and code not in used_codes:
            return code
        last_code = code or last_code
        remaining = int(end - time.time())
        if remaining <= 0:
            raise RuntimeError(f"等待新的邮箱验证码超时，取码接口仍返回已失败验证码：{last_code or '-'}")
        logger.warning(
            "[Codex][Browser] 取码接口仍返回已提交过的旧 OTP=%s，继续等待最新验证码（剩余 %ss）",
            last_code or "-",
            remaining,
        )
        time.sleep(min(5, max(1, remaining)))


def _install_email_otp_validate_hook(driver) -> None:
    """
    在页面内 hook fetch/XHR，捕获 email-otp/validate 的接口响应体。

    指纹浏览器不能像纯协议模式一样直接拿 requests.Response，因此在提交邮箱 OTP 前
    注入此 hook，后续只读取接口 JSON error.code，不靠页面文字判断废号。
    """
    script = r"""
    (() => {
      window.__codexEmailOtpValidateResponses = [];
      if (window.__codexEmailOtpValidateHooked) return true;
      window.__codexEmailOtpValidateHooked = true;
      const hit = (url) => String(url || '').includes('/api/accounts/email-otp/validate');
      const save = (url, status, body) => {
        try {
          if (!hit(url)) return;
          window.__codexEmailOtpValidateResponses.push({
            url: String(url || ''),
            status: Number(status || 0),
            body: String(body || '').slice(0, 2000),
            ts: Date.now(),
          });
        } catch (e) {}
      };
      const origFetch = window.fetch;
      if (origFetch) {
        window.fetch = async function(input, init) {
          const resp = await origFetch.apply(this, arguments);
          try {
            const url = (typeof input === 'string') ? input : (input && input.url);
            if (hit(url)) {
              resp.clone().text().then(t => save(url, resp.status, t)).catch(() => {});
            }
          } catch (e) {}
          return resp;
        };
      }
      const origOpen = XMLHttpRequest.prototype.open;
      const origSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function(method, url) {
        this.__codexOtpValidateUrl = url;
        return origOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function() {
        try {
          this.addEventListener('loadend', function() {
            try {
              if (hit(this.__codexOtpValidateUrl)) save(this.__codexOtpValidateUrl, this.status, this.responseText);
            } catch (e) {}
          });
        } catch (e) {}
        return origSend.apply(this, arguments);
      };
      return true;
    })();
    """
    try:
        driver.execute_script(script)
    except Exception as exc:
        logger.debug("[Codex][Browser] 注入 email-otp/validate 响应 hook 失败：%s", exc)


def _read_email_otp_validate_dead_code(driver) -> str:
    try:
        rows = driver.execute_script("return window.__codexEmailOtpValidateResponses || [];") or []
    except Exception:
        return ""
    if not isinstance(rows, list):
        return ""
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        code = detect_account_unusable_response_body(str(row.get("body") or ""))
        if code:
            logger.warning(
                "[Codex][Browser] email-otp/validate 响应识别账号已废：code=%s status=%s",
                code,
                row.get("status"),
            )
            return code
    return ""


# 邮箱验证码页判断复用 roxy_registration 的强版本（URL + 输入框属性识别，
# 且明确排除 /log-in/password），不使用本地弱化版，避免点完一次性验证码后
# 页面已渲染 OTP 输入框却因 URL 不含 email-verification 而识别失败。

def _wait_after_email_otp_submit(driver, timeout: int = 45) -> str:
    """
    提交邮箱 OTP 后等待页面离开 /email-verification。

    返回：
      - accepted：已离开邮箱验证码页 / 进入手机号页 / 进入 callback；
      - invalid：页面明确报错、输入框标红，或长时间停留验证码页。
    """
    end = time.time() + timeout
    last_url = ""
    last_log = 0.0
    while time.time() < end:
        try:
            dead_code = _read_email_otp_validate_dead_code(driver)
            if dead_code:
                return f"deactivated:{dead_code}"
            url = str(driver.current_url or "")
            if url != last_url:
                logger.info("[Codex][Browser] 邮箱 OTP 后等待跳转：url=%s", url)
                last_url = url
            if _is_callback_url(url):
                return "accepted"
            if _has_strict_add_phone_form(driver) or _is_phone_code_page(driver):
                return "accepted"
            # 已经离开 email-verification，交给后续授权/手机号/consent 流程处理。
            if "email-verification" not in url.lower():
                return "accepted"

            state = _email_otp_page_state(driver)
            invalid = any(str(i.get("ariaInvalid") or "").lower() == "true" for i in (state.get("inputs") or []))
            errors = [str(x) for x in (state.get("errors") or []) if str(x).strip()]
            body_text = str(state.get("text") or "").lower()
            error_hit = any(x in body_text for x in (
                "invalid code", "incorrect code", "wrong code", "expired",
                "验证码错误", "验证码无效", "验证码已过期", "コードが正しく", "無効", "期限",
            ))
            if invalid or errors or error_hit:
                logger.warning(
                    "[Codex][Browser] 邮箱 OTP 提交后检测到错误/仍需验证码：errors=%s invalid=%s url=%s",
                    errors[:3],
                    invalid,
                    url,
                )
                return "invalid"

            if time.time() - last_log > 6:
                logger.info("[Codex][Browser] 邮箱 OTP 后仍在 email-verification，继续等待页面自动跳转")
                last_log = time.time()
        except Exception:
            pass
        time.sleep(0.5)
    logger.warning("[Codex][Browser] 邮箱 OTP 后等待跳转超时，当前 url=%s，按验证码无效/过期处理", getattr(driver, "current_url", ""))
    return "invalid"


def _phone_page_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const radios = [...document.querySelectorAll('input[type=radio]')].filter(visible).map(el => ({
          name: el.name || '', value: el.value || '', checked: !!el.checked, id: el.id || ''
        }));
        const inputs = [...document.querySelectorAll('input,select,textarea')].filter(visible).map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
          id: el.id || '', autocomplete: el.getAttribute('autocomplete') || '', placeholder: el.getAttribute('placeholder') || '',
          ariaInvalid: el.getAttribute('aria-invalid') || '', value: el.value || ''
        }));
        const forms = [...document.querySelectorAll('form')].map(f => ({action: f.getAttribute('action') || ''}));
        const bodyText = (document.body?.innerText || '').slice(0, 1200);
        return {url: location.href, radios, inputs, forms, bodyText};
        """) or {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "url": getattr(driver, 'current_url', '')}


def _sms_channel_selection_state(driver) -> dict:
    """读取当前 SMS/WhatsApp 通道选择状态。"""
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const radios = [...document.querySelectorAll('input[type=radio]')].filter(visible);
        const norm = v => String(v || '').toLowerCase().replace(/[\s_-]+/g, '');
        let hasSms = false, hasWhatsapp = false, smsChecked = false, whatsappChecked = false;
        for (const el of radios) {
          const v = norm(el.value);
          if (['sms','text','textmessage'].includes(v)) {
            hasSms = true;
            if (el.checked) smsChecked = true;
          }
          if (v.includes('whatsapp')) {
            hasWhatsapp = true;
            if (el.checked) whatsappChecked = true;
          }
        }
        return {hasSms, hasWhatsapp, smsChecked, whatsappChecked, radioCount: radios.length};
        """) or {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _select_sms_channel_or_raise(driver) -> None:
    # 国家下拉若仍展开，会挡住 SMS radio / 污染 bodyText
    _dismiss_phone_country_dropdown(driver)
    state = _phone_page_state(driver)
    ch = _sms_channel_selection_state(driver)
    has_whatsapp = bool(ch.get("hasWhatsapp"))
    has_sms = bool(ch.get("hasSms"))
    # 如果存在 WhatsApp 且没有 SMS/text 可选，当前接码平台无法读取 WhatsApp，直接换号。
    if has_whatsapp and not has_sms:
        raise RuntimeError(f"whatsapp_channel: 页面仅提供 WhatsApp 通道 state={state}")
    # 选择 SMS/text radio。无 radio 时可能默认 SMS。多轮点击，防止 React 重置。
    selected = {"ok": False}
    for attempt in range(3):
        selected = driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const radios = [...document.querySelectorAll('input[type=radio]')].filter(visible);
        const norm = v => String(v || '').toLowerCase().replace(/[\s_-]+/g, '');
        const sms = radios.find(el => ['sms','text','textmessage'].includes(norm(el.value)));
        if (!sms) return {ok:false, reason:'no_sms_radio'};
        // 先点 label（React Aria segmented control 更认这个）
        const id = sms.id;
        if (id) {
          const lab = document.querySelector(`label[for="${id}"]`);
          if (lab) { try { lab.click(); } catch (e) {} }
        }
        // 找相邻文案含 SMS 的 label
        for (const lab of document.querySelectorAll('label')) {
          const t = String(lab.textContent || '').toLowerCase();
          if (/\bsms\b|text message|ショート|短信/.test(t) && !/whatsapp/.test(t)) {
            try { lab.click(); } catch (e) {}
          }
        }
        try { sms.click(); } catch (e) {}
        sms.checked = true;
        sms.dispatchEvent(new Event('input', {bubbles:true}));
        sms.dispatchEvent(new Event('change', {bubbles:true}));
        // 同步取消 whatsapp
        for (const el of radios) {
          if (norm(el.value).includes('whatsapp')) {
            el.checked = false;
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
          }
        }
        return {ok:true, value: sms.value || 'sms', checked: !!sms.checked, attempt: arguments[0]};
        """, attempt) or {}
        time.sleep(0.2 + 0.15 * attempt)
        ch2 = _sms_channel_selection_state(driver)
        if ch2.get("smsChecked") and not ch2.get("whatsappChecked"):
            break
        if not ch2.get("hasSms"):
            break
    ch2 = _sms_channel_selection_state(driver)
    if selected.get("ok"):
        logger.info(
            "[Codex][Browser] 已选择 SMS 短信通道：%s after={sms=%s wa=%s}",
            selected, ch2.get("smsChecked"), ch2.get("whatsappChecked"),
        )
    # 有 WhatsApp 且最终仍勾在 WhatsApp → 明确换号，避免误发
    if ch2.get("hasWhatsapp") and ch2.get("whatsappChecked") and not ch2.get("smsChecked"):
        raise RuntimeError(
            f"whatsapp_channel: 选择 SMS 后仍停留在 WhatsApp 通道 state={state} channel={ch2}"
        )


def _is_phone_code_state(state: dict) -> bool:
    url = str(state.get('url') or '').lower()
    if 'email-verification' in url:
        # 邮箱 OTP 页面也会出现 autocomplete=one-time-code，不能误判成手机验证码页。
        return False
    if 'phone-verification' in url:
        return True
    forms = state.get('forms') or []
    form_actions = ' '.join(str(f.get('action') or '') for f in forms).lower()
    if 'phone-verification' in form_actions:
        return True
    inputs = state.get('inputs') or []
    attrs = ' '.join(' '.join(str(i.get(k) or '') for k in ('type','name','id','autocomplete','placeholder')) for i in inputs).lower()
    body = str(state.get('bodyText') or '').lower()
    has_code_input = 'one-time-code' in attrs or 'otp' in attrs or 'code' in attrs
    phone_hint = (
        'phone' in url or 'phone' in form_actions
        or 'check your phone' in body
        or 'verification code we just sent' in body
        or 'enter the verification code' in body and ('text message' in body or 'phone' in body)
        or 'resend text message' in body
        or 'sent to +' in body
    )
    return bool(phone_hint and has_code_input)


def _is_phone_code_page(driver) -> bool:
    return _is_phone_code_state(_phone_page_state(driver))


def _is_add_phone_page(driver) -> bool:
    state = _phone_page_state(driver)
    url = str(state.get('url') or '').lower()
    inputs = state.get('inputs') or []
    attrs = ' '.join(' '.join(str(i.get(k) or '') for k in ('type','name','id','autocomplete')) for i in inputs).lower()
    return 'add-phone' in url or 'type tel' in attrs or 'phone' in attrs or 'tel' in attrs


_PHONE_INPUT_SELECTORS = [
    "input[type='tel']",
    "input[name='phone']",
    "input[name='phone_number']",
    "input[autocomplete='tel']",
    "input[id*='phone']",
    "input[placeholder*='Phone']",
    "input[placeholder*='phone']",
]


def _has_strict_add_phone_form(driver) -> bool:
    try:
        return bool(driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const form = document.querySelector('form[action*="/add-phone" i]')
          || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
        if (!form) return false;
        return !![...form.querySelectorAll('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]')].find(visible);
        """))
    except Exception:
        return False


def _auth_origin(driver) -> str:
    try:
        parsed = urlparse(str(driver.current_url or ""))
        if parsed.scheme and parsed.netloc and parsed.hostname and parsed.hostname.endswith("openai.com"):
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return "https://auth.openai.com"


def _ensure_add_phone_input(driver, *, reason: str = ""):
    """确保当前页面回到 add-phone，并返回手机号输入框。

    换号时如果还停留在 phone-verification/OTP 页，必须先回到手机号页，
    再把新号码重新写入页面并重新提交。
    """
    if _has_strict_add_phone_form(driver):
        return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=2)

    current = str(getattr(driver, "current_url", "") or "")
    if "email-verification" in current.lower():
        logger.info("[Codex][Browser] 当前仍在 email-verification，先等待授权流程自动跳转，避免 invalid_auth_step")
        _wait_after_email_otp_submit(driver, timeout=45)
        if _has_strict_add_phone_form(driver):
            return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=2)
        current = str(getattr(driver, "current_url", "") or "")

    target = _auth_origin(driver).rstrip("/") + "/add-phone"
    logger.info(
        "[Codex][Browser] 当前不在手机号输入页，准备重新打开 add-phone 后换号：reason=%s url=%s target=%s",
        reason or "retry", current, target,
    )
    try:
        driver.get(target)
        human_delay("navigate")
        return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=10)
    except Exception as first_exc:
        # 某些流程不允许直接打开 /add-phone，尝试浏览器返回到上一页。
        logger.info("[Codex][Browser] 直接打开 add-phone 未拿到输入框，尝试 history back：%s", str(first_exc)[:160])
        try:
            driver.back()
            human_delay("navigate")
            return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=8)
        except Exception as back_exc:
            raise RuntimeError(
                f"无法回到手机号输入页以重新换号: direct={type(first_exc).__name__}: {first_exc}; "
                f"back={type(back_exc).__name__}: {back_exc}; state={_phone_page_state(driver)}"
            )


def _force_hidden_phone_e164(driver, e164: str) -> str:
    """强制写回隐藏 phoneNumber，避免国家码错选后被改成 +1。"""
    try:
        val = driver.execute_script(
            r"""
            const e164 = String(arguments[0] || '').trim();
            const form = document.querySelector('form[action*="/add-phone" i]')
              || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
            if (!form) return '';
            let hidden = form.querySelector('input[name="phoneNumber"]');
            if (!hidden) {
              hidden = document.createElement('input');
              hidden.type = 'hidden';
              hidden.name = 'phoneNumber';
              form.appendChild(hidden);
            }
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
            if (setter) setter.call(hidden, e164); else hidden.value = e164;
            hidden.dispatchEvent(new Event('input', {bubbles:true}));
            hidden.dispatchEvent(new Event('change', {bubbles:true}));
            return hidden.value || e164;
            """,
            e164,
        )
        return str(val or e164)
    except Exception:
        return e164


def _read_phone_country_state(driver) -> dict:
    """读取 add-phone 当前国家/区号 UI 状态（select / combobox 文案）。"""
    try:
        raw = driver.execute_script(
            r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const form = document.querySelector('form[action*="/add-phone" i]')
              || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
            const root = form || document;
            const out = {
              selectCount: 0,
              selectValue: '',
              selectText: '',
              selectOptionCount: 0,
              selectSamples: [],
              comboboxText: '',
              comboboxCount: 0,
              openerSamples: [],
              listboxOpen: false,
              optionCount: 0,
              optionSamples: [],
            };
            const selects = [...root.querySelectorAll('select')].filter(visible);
            out.selectCount = selects.length;
            if (selects[0]) {
              const select = selects[0];
              const opts = [...select.options];
              out.selectOptionCount = opts.length;
              out.selectValue = String(select.value || '');
              const cur = select.selectedIndex >= 0 ? opts[select.selectedIndex] : null;
              out.selectText = String(cur?.textContent || cur?.label || '').replace(/\s+/g, ' ').trim();
              out.selectSamples = opts.slice(0, 8).map(o => ({
                value: String(o.value || ''),
                text: String(o.textContent || o.label || '').replace(/\s+/g, ' ').trim().slice(0, 60),
              }));
            }
            const openers = [...root.querySelectorAll(
              'button[aria-haspopup="listbox"], [role="combobox"], button[aria-label*="country" i], button[aria-label*="国" i], button[aria-label*="dial" i], button[aria-label*="コード" i], button[aria-label*="国番号" i]'
            )].filter(visible);
            out.comboboxCount = openers.length;
            out.openerSamples = openers.slice(0, 4).map(el => String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\s+/g,' ').trim().slice(0, 80));
            if (openers[0]) {
              out.comboboxText = out.openerSamples[0] || '';
              out.listboxOpen = String(openers[0].getAttribute('aria-expanded') || '').toLowerCase() === 'true';
            }
            const options = [...document.querySelectorAll(
              '[role="listbox"] [role="option"], [role="option"], li[data-key], div[data-key]'
            )].filter(visible);
            out.optionCount = options.length;
            out.optionSamples = options.slice(0, 8).map(el => String(el.textContent || '').replace(/\s+/g,' ').trim().slice(0, 80));
            return out;
            """
        ) or {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    if not isinstance(raw, dict):
        return {}
    # Python 侧解析区号（支持日文国名 / ISO）
    select_dial = extract_option_dial_code(
        raw.get("selectText"),
        value=raw.get("selectValue"),
    )
    combo_dial = extract_option_dial_code(raw.get("comboboxText"))
    dial = select_dial or combo_dial
    selected_text = str(raw.get("selectText") or raw.get("comboboxText") or "").strip()
    return {
        **raw,
        "dialCode": dial,
        "selectedText": selected_text,
    }


def _dismiss_phone_country_dropdown(driver) -> None:
    try:
        driver.execute_script(
            r"""
            try { document.activeElement && document.activeElement.blur && document.activeElement.blur(); } catch (e) {}
            document.body && document.body.click && document.body.click();
            """
        )
    except Exception:
        pass
    try:
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except Exception:
        pass
    time.sleep(0.12)


def _try_native_select_country(driver, want: str, iso_candidates: list[str]) -> dict:
    """尝试 native <select> 选国（支持 ISO / +dial / 国名）。"""
    try:
        result = driver.execute_script(
            r"""
            const want = String(arguments[0] || '').replace(/\D+/g, '');
            const isos = (arguments[1] || []).map(x => String(x || '').toUpperCase());
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const form = document.querySelector('form[action*="/add-phone" i]')
              || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
            const root = form || document;
            const selects = [...root.querySelectorAll('select')].filter(visible);
            if (!selects.length) return {ok:false, reason:'no_select'};

            const parseCode = (opt) => {
              const text = String(opt?.textContent || opt?.label || '').replace(/\s+/g, ' ').trim();
              const value = String(opt?.value || '').trim();
              const data = String(opt?.getAttribute?.('data-key') || opt?.getAttribute?.('data-value') || '').trim();
              const blob = [text, value, data].join(' ');
              let m = blob.match(/\(\s*\+(\d{1,4})\s*\)/) || blob.match(/\+(\d{1,4})\b/);
              if (m) return m[1];
              if (/^\+?\d{1,4}$/.test(value)) return value.replace(/\D+/g, '');
              const iso = (value.match(/^[A-Za-z]{2}$/) || data.match(/^[A-Za-z]{2}$/) || text.match(/\b([A-Z]{2})\b/));
              if (iso) return 'ISO:' + String(iso[1] || iso[0]).toUpperCase();
              return '';
            };
            const setSelect = (select, opt) => {
              const proto = window.HTMLSelectElement?.prototype;
              const setter = proto && Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              if (setter) setter.call(select, opt.value); else select.value = opt.value;
              try { select.selectedIndex = [...select.options].indexOf(opt); } catch (e) {}
              select.dispatchEvent(new Event('input', {bubbles:true}));
              select.dispatchEvent(new Event('change', {bubbles:true}));
              try { select.dispatchEvent(new Event('blur', {bubbles:true})); } catch (e) {}
            };

            for (const select of selects) {
              const options = [...select.options];
              let hit = null;
              for (const opt of options) {
                const code = parseCode(opt);
                if (code === want) { hit = opt; break; }
                if (code.startsWith('ISO:') && isos.includes(code.slice(4))) { hit = opt; break; }
                const valueU = String(opt.value || '').toUpperCase();
                if (isos.includes(valueU)) { hit = opt; break; }
              }
              // 仍未命中：用 Python 传入的国名不在这里；返回样本
              if (!hit) {
                return {
                  ok: false,
                  reason: 'select_no_match',
                  selectCount: selects.length,
                  optionCount: options.length,
                  sample: options.slice(0, 8).map(o => ({
                    value: String(o.value||''),
                    text: String(o.textContent||o.label||'').replace(/\s+/g,' ').trim().slice(0,50)
                  })),
                };
              }
              const changed = select.value !== hit.value;
              setSelect(select, hit);
              return {
                ok: true,
                method: 'select',
                selectedChanged: changed,
                dialCode: want,
                selectedText: String(hit.textContent || hit.label || '').replace(/\s+/g,' ').trim(),
                selectedValue: String(hit.value || ''),
              };
            }
            return {ok:false, reason:'no_select'};
            """,
            want,
            iso_candidates,
        ) or {}
        if not isinstance(result, dict):
            return {"ok": False, "reason": "bad_script"}
        # 日文国名：Python 侧再扫一遍 option 文本
        if not result.get("ok") and result.get("sample"):
            for item in result.get("sample") or []:
                if not isinstance(item, dict):
                    continue
                code = extract_option_dial_code(item.get("text"), value=item.get("value"))
                if code == want:
                    # 用 value 精确再设一次
                    pick = driver.execute_script(
                        r"""
                        const wantValue = String(arguments[0] || '');
                        const wantText = String(arguments[1] || '');
                        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                        const form = document.querySelector('form[action*="/add-phone" i]')
                          || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
                        const root = form || document;
                        for (const select of [...root.querySelectorAll('select')].filter(visible)) {
                          const hit = [...select.options].find(o =>
                            String(o.value||'') === wantValue
                            || String(o.textContent||o.label||'').replace(/\s+/g,' ').trim() === wantText
                          );
                          if (!hit) continue;
                          const proto = window.HTMLSelectElement?.prototype;
                          const setter = proto && Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                          if (setter) setter.call(select, hit.value); else select.value = hit.value;
                          select.dispatchEvent(new Event('input', {bubbles:true}));
                          select.dispatchEvent(new Event('change', {bubbles:true}));
                          return {
                            ok: true,
                            method: 'select_name',
                            selectedChanged: true,
                            dialCode: arguments[2],
                            selectedText: String(hit.textContent || hit.label || '').replace(/\s+/g,' ').trim(),
                            selectedValue: String(hit.value || ''),
                          };
                        }
                        return {ok:false, reason:'select_name_miss'};
                        """,
                        item.get("value") or "",
                        item.get("text") or "",
                        want,
                    ) or {}
                    if isinstance(pick, dict) and pick.get("ok"):
                        return pick
            # 完整 options 可能 >8，再拉全量用 Python 匹配
            all_opts = driver.execute_script(
                r"""
                const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                const form = document.querySelector('form[action*="/add-phone" i]')
                  || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
                const root = form || document;
                const select = [...root.querySelectorAll('select')].filter(visible)[0];
                if (!select) return [];
                return [...select.options].map(o => ({
                  value: String(o.value||''),
                  text: String(o.textContent||o.label||'').replace(/\s+/g,' ').trim()
                }));
                """
            ) or []
            for item in all_opts:
                if not isinstance(item, dict):
                    continue
                code = extract_option_dial_code(item.get("text"), value=item.get("value"))
                if code != want and str(item.get("value") or "").upper() not in iso_candidates:
                    continue
                pick = driver.execute_script(
                    r"""
                    const wantValue = String(arguments[0] || '');
                    const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                    const form = document.querySelector('form[action*="/add-phone" i]')
                      || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
                    const root = form || document;
                    const select = [...root.querySelectorAll('select')].filter(visible)[0];
                    if (!select) return {ok:false};
                    const hit = [...select.options].find(o => String(o.value||'') === wantValue);
                    if (!hit) return {ok:false};
                    const proto = window.HTMLSelectElement?.prototype;
                    const setter = proto && Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(select, hit.value); else select.value = hit.value;
                    select.dispatchEvent(new Event('input', {bubbles:true}));
                    select.dispatchEvent(new Event('change', {bubbles:true}));
                    return {
                      ok:true, method:'select_fullscan', selectedChanged:true,
                      dialCode: arguments[1],
                      selectedText: String(hit.textContent||hit.label||'').replace(/\s+/g,' ').trim(),
                      selectedValue: String(hit.value||''),
                    };
                    """,
                    item.get("value") or "",
                    want,
                ) or {}
                if isinstance(pick, dict) and pick.get("ok"):
                    return pick
        return result
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _open_phone_country_picker(driver) -> dict:
    """点开国家 combobox/listbox（必须与后续 wait 分步，React 异步渲染）。"""
    try:
        return driver.execute_script(
            r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const form = document.querySelector('form[action*="/add-phone" i]')
              || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
            const root = form || document;
            const openers = [
              ...root.querySelectorAll('button[aria-haspopup="listbox"], [role="combobox"], button[aria-label*="country" i], button[aria-label*="国" i], button[aria-label*="dial" i], button[aria-label*="コード" i], button[aria-label*="国番号" i]'),
              ...root.querySelectorAll('select'),
            ].filter(visible);
            // 电话输入框左侧按钮：找 tel input 前一个 button
            const tel = [...root.querySelectorAll('input[type="tel"], input[autocomplete="tel"]')].find(visible);
            if (tel) {
              let p = tel.parentElement;
              for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
                const btns = [...p.querySelectorAll('button,[role="combobox"]')].filter(visible);
                for (const b of btns) {
                  if (!openers.includes(b)) openers.unshift(b);
                }
              }
            }
            if (!openers.length) return {ok:false, reason:'no_opener'};
            const opener = openers[0];
            try { opener.focus(); } catch (e) {}
            try { opener.click(); } catch (e) {
              try {
                opener.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                opener.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
                opener.dispatchEvent(new MouseEvent('click', {bubbles:true}));
              } catch (e2) {}
            }
            return {
              ok: true,
              text: String(opener.innerText || opener.textContent || opener.getAttribute('aria-label') || '').replace(/\s+/g,' ').trim().slice(0,80),
              tag: opener.tagName,
              expanded: String(opener.getAttribute('aria-expanded') || ''),
              openerCount: openers.length,
            };
            """
        ) or {"ok": False, "reason": "bad_script"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _type_country_filter(driver, want: str) -> dict:
    """若国家列表有搜索框，输入区号过滤。"""
    queries = [f"+{want}", want]
    for q in queries:
        try:
            ok = driver.execute_script(
                r"""
                const q = String(arguments[0] || '');
                const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                const inputs = [
                  ...document.querySelectorAll('input[type="search"], input[role="searchbox"], [role="listbox"] input, [data-testid*="search" i] input, input[placeholder*="Search" i], input[placeholder*="search" i], input[placeholder*="国" i], input[aria-label*="Search" i], input[aria-label*="search" i]')
                ].filter(visible);
                // combobox 自身可输入
                const combos = [...document.querySelectorAll('[role="combobox"]')].filter(visible);
                const targets = [...inputs, ...combos.filter(el => el.tagName === 'INPUT')];
                if (!targets.length) return false;
                const el = targets[0];
                const proto = window.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                el.focus();
                if (setter) setter.call(el, ''); else el.value = '';
                el.dispatchEvent(new Event('input', {bubbles:true}));
                if (setter) setter.call(el, q); else el.value = q;
                el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:q}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                return true;
                """,
                q,
            )
            if ok:
                time.sleep(0.35)
                return {"ok": True, "query": q}
        except Exception:
            continue
    # 键盘直接往已打开的 combobox 打字（不要 ESC，否则会关掉刚打开的列表）
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver).send_keys(f"+{want}").perform()
        time.sleep(0.3)
        return {"ok": True, "query": f"+{want}", "method": "keys"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _click_country_list_option(driver, want: str, iso_candidates: list[str]) -> dict:
    """在已打开的 listbox 中点选目标区号。"""
    try:
        raw_opts = driver.execute_script(
            r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const options = [...document.querySelectorAll(
              '[role="listbox"] [role="option"], [role="option"], li[data-key], div[data-key], [role="listbox"] li, [role="listbox"] div'
            )].filter(visible);
            return options.map((el, idx) => ({
              idx,
              text: String(el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120),
              value: String(el.getAttribute('data-key') || el.getAttribute('data-value') || el.getAttribute('value') || ''),
              aria: String(el.getAttribute('aria-label') || ''),
            }));
            """
        ) or []
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "optionCount": 0}

    if not isinstance(raw_opts, list):
        raw_opts = []

    hit_idx = None
    hit_text = ""
    for item in raw_opts:
        if not isinstance(item, dict):
            continue
        text = " ".join(
            str(item.get(k) or "") for k in ("text", "value", "aria")
        )
        code = extract_option_dial_code(
            item.get("text") or item.get("aria"),
            value=item.get("value"),
            data_key=item.get("value"),
        )
        value_u = str(item.get("value") or "").upper()
        if (
            code == want
            or value_u in iso_candidates
            or f"+{want}" in text
            or extract_option_dial_code(text) == want
        ):
            hit_idx = item.get("idx")
            hit_text = str(item.get("text") or "")[:80]
            break

    if hit_idx is None:
        return {
            "ok": False,
            "reason": "list_option_not_found",
            "optionCount": len(raw_opts),
            "sample": [str(x.get("text") or "")[:50] for x in raw_opts[:8] if isinstance(x, dict)],
        }

    try:
        clicked = driver.execute_script(
            r"""
            const wantIdx = Number(arguments[0]);
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const options = [...document.querySelectorAll(
              '[role="listbox"] [role="option"], [role="option"], li[data-key], div[data-key], [role="listbox"] li, [role="listbox"] div'
            )].filter(visible);
            const el = options[wantIdx];
            if (!el) return false;
            try { el.scrollIntoView({block:'nearest'}); } catch (e) {}
            try { el.click(); return true; } catch (e) {}
            try {
              el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
              el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
              el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
              return true;
            } catch (e2) { return false; }
            """,
            int(hit_idx),
        )
    except Exception as exc:
        return {"ok": False, "reason": f"click_fail:{type(exc).__name__}: {exc}"}

    if not clicked:
        return {"ok": False, "reason": "click_failed", "optionCount": len(raw_opts)}
    return {
        "ok": True,
        "method": "listbox",
        "selectedChanged": True,
        "dialCode": want,
        "selectedText": hit_text,
        "optionCount": len(raw_opts),
    }


def _keyboard_pick_country(driver, want: str) -> dict:
    """键盘兜底：打开后输入 +区号，方向键 + Enter。"""
    try:
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.common.action_chains import ActionChains
        _open_phone_country_picker(driver)
        time.sleep(0.25)
        ActionChains(driver).send_keys(f"+{want}").pause(0.25).send_keys(Keys.ARROW_DOWN).pause(0.1).send_keys(Keys.ENTER).perform()
        time.sleep(0.35)
        return {"ok": True, "method": "keyboard", "selectedChanged": True, "dialCode": want}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _select_phone_country_dial(driver, dial_code: str) -> dict:
    """强制把 add-phone 国家/区号选成目标 dial_code。

    关键修复（彻底版）：
    1. 先读当前 UI 区号，已正确则跳过；
    2. native <select>：支持 ISO / +dial / 国名（含日文）；
    3. React Aria combobox：Python 分步 open → wait → filter → click（禁止同一次 JS 点开即查选项）；
    4. 键盘兜底；
    5. 必须以 UI 回读区号校验成功才 ok=True。
    """
    want = digits_only(dial_code)
    if not want:
        return {"ok": False, "reason": "empty_dial"}
    iso_candidates = dial_to_iso_candidates(want)

    def _verified(method: str, base: dict | None = None) -> dict | None:
        state = _read_phone_country_state(driver)
        if country_dial_matches(state.get("dialCode"), want):
            out = {
                "ok": True,
                "method": method,
                "selectedChanged": True if method != "already" else False,
                "dialCode": want,
                "selectedText": str(state.get("selectedText") or "")[:80],
                "ui": {k: state.get(k) for k in ("selectValue", "selectText", "comboboxText", "selectOptionCount", "optionCount")},
            }
            if isinstance(base, dict):
                out["selectedChanged"] = bool(base.get("selectedChanged", out["selectedChanged"]))
                if base.get("selectedText"):
                    out["selectedText"] = str(base.get("selectedText"))[:80]
            return out
        return None

    # 0) 已是目标区号
    already = _verified("already")
    if already:
        already["selectedChanged"] = False
        _dismiss_phone_country_dropdown(driver)
        return already

    attempts: list[dict] = []

    # 1) native select
    native = _try_native_select_country(driver, want, iso_candidates)
    attempts.append({
        "step": "select",
        "ok": native.get("ok"),
        "reason": native.get("reason"),
        "optionCount": native.get("optionCount"),
        "sample": native.get("sample"),
    })
    if native.get("ok"):
        time.sleep(0.4)
        hit = _verified("select", native)
        if hit:
            _dismiss_phone_country_dropdown(driver)
            return hit

    # 2) combobox/listbox 分步
    for round_i in range(2):
        _dismiss_phone_country_dropdown(driver)
        opened = _open_phone_country_picker(driver)
        attempts.append({"step": f"open_{round_i}", **opened})
        # 关键：给 React portal 渲染时间（旧代码同一次 JS 点开即查 → optionCount=0）
        deadline = time.time() + 2.5
        option_count = 0
        while time.time() < deadline:
            st = _read_phone_country_state(driver)
            option_count = int(st.get("optionCount") or 0)
            if option_count > 0 or st.get("listboxOpen"):
                if option_count > 0:
                    break
            time.sleep(0.15)

        # 过滤搜索
        filt = _type_country_filter(driver, want)
        attempts.append({"step": f"filter_{round_i}", **filt})
        time.sleep(0.25)

        picked = _click_country_list_option(driver, want, iso_candidates)
        attempts.append({"step": f"pick_{round_i}", **{k: picked.get(k) for k in ("ok", "reason", "optionCount", "sample", "selectedText")}})
        if picked.get("ok"):
            time.sleep(0.45)
            hit = _verified("listbox", picked)
            if hit:
                _dismiss_phone_country_dropdown(driver)
                return hit

        # 若选项仍为空，再等一轮
        if option_count == 0:
            time.sleep(0.35)

    # 3) 键盘兜底
    kb = _keyboard_pick_country(driver, want)
    attempts.append({"step": "keyboard", **kb})
    time.sleep(0.45)
    hit = _verified("keyboard", kb)
    if hit:
        _dismiss_phone_country_dropdown(driver)
        return hit

    final = _read_phone_country_state(driver)
    _dismiss_phone_country_dropdown(driver)
    return {
        "ok": False,
        "reason": "dial_option_not_found",
        "want": want,
        "iso": iso_candidates,
        "selectCount": final.get("selectCount"),
        "optionCount": final.get("optionCount"),
        "selectOptionCount": final.get("selectOptionCount"),
        "currentDial": final.get("dialCode") or "",
        "selectedText": final.get("selectedText") or "",
        "sample": final.get("optionSamples") or final.get("selectSamples") or [],
        "attempts": attempts[-6:],
    }


def _set_phone_value(driver, phone: str, *, timeout: int = 10) -> dict:
    """填写 add-phone 表单。

    关键修复：
    - 先按 E.164 强制选择国家/区号（不能停在默认美国 +1）；
    - 非美号国家选择失败必须硬失败，禁止仅靠 hidden 蒙混；
    - 再写国内号；始终强制 hidden phoneNumber = 完整 E.164；
    - 校验：hidden 正确 + 国家区号正确（非美号）。
    """
    if not _has_strict_add_phone_form(driver):
        raise RuntimeError(f"当前不是 add-phone 手机号输入页，不能填写手机号: state={_phone_page_state(driver)}")

    e164 = normalize_e164(phone)
    e164_digits = digits_only(e164)
    guessed_cc = guess_dial_code(e164_digits)
    preferred_national = national_digits(e164_digits, guessed_cc)

    # 1) 强制国家码
    country_sel = _select_phone_country_dial(driver, guessed_cc) if guessed_cc else {"ok": True, "method": "skip", "dialCode": ""}
    if country_sel.get("ok"):
        logger.info(
            "[Codex][Browser] 已选择手机国家/区号：+%s method=%s text=%s changed=%s",
            country_sel.get("dialCode") or guessed_cc,
            country_sel.get("method"),
            str(country_sel.get("selectedText") or "")[:40],
            country_sel.get("selectedChanged"),
        )
        time.sleep(0.55)
    else:
        # 非 +1：国家选不中几乎必然 invalid_phone / 截断，直接失败换号
        # +1 且页面已是美国（日文「アメリカ合衆国」）可继续
        ui = _read_phone_country_state(driver)
        ui_dial = digits_only(ui.get("dialCode"))
        ui_text = str(ui.get("selectedText") or "")
        us_ok = guessed_cc == "1" and (
            ui_dial in ("", "1")
            or "アメリカ" in ui_text
            or "united states" in ui_text.lower()
            or "usa" in ui_text.lower()
        )
        if not us_ok:
            raise RuntimeError(
                "country_select_failed: 国家/区号选择失败 want=+"
                f"{guessed_cc} detail="
                f"{ {k: country_sel.get(k) for k in ('reason','selectCount','optionCount','selectOptionCount','currentDial','selectedText','sample','attempts')} } "
                f"ui={ {k: ui.get(k) for k in ('dialCode','selectedText','selectValue','comboboxText','selectOptionCount','optionCount')} } "
                f"state={_phone_page_state(driver)}"
            )
        logger.warning(
            "[Codex][Browser] 国家/区号选择未显式命中 want=+%s，但判定为美/加默认可用 ui=%s detail=%s",
            guessed_cc or "-",
            {k: ui.get(k) for k in ("dialCode", "selectedText")},
            {k: country_sel.get(k) for k in ("reason", "selectCount", "optionCount", "sample")},
        )
        country_sel = {
            "ok": True,
            "method": "us_default",
            "selectedChanged": False,
            "dialCode": "1",
            "selectedText": ui_text or "US",
        }

    last_result: dict | None = None
    candidates = []
    if preferred_national and preferred_national != e164_digits:
        candidates.append(("national", preferred_national))
    candidates.append(("e164", e164))
    if preferred_national and preferred_national != e164_digits:
        candidates.append(("national_retry", preferred_national))
        # 去掉国内号前导 0
        if preferred_national.startswith("0") and len(preferred_national) > 6:
            candidates.append(("national_no0", preferred_national.lstrip("0") or preferred_national))

    for mode, fill_value in candidates:
        # 每轮再确认一次国家码（React 可能重置）
        if guessed_cc:
            again = _select_phone_country_dial(driver, guessed_cc)
            if again.get("ok"):
                country_sel = again
                if again.get("selectedChanged"):
                    time.sleep(0.35)
            elif guessed_cc != "1":
                raise RuntimeError(
                    f"country_select_failed: 填号前国家码丢失 want=+{guessed_cc} detail={again}"
                )

        result = driver.execute_script(
            r"""
            const rawPhone = String(arguments[0] || '').trim();
            const forcedDial = String(arguments[1] || '').trim();
            const preferredVisible = String(arguments[2] || '').trim();
            const e164 = rawPhone.startsWith('+') ? rawPhone : ('+' + rawPhone.replace(/\D+/g, ''));
            const digits = e164.replace(/\D+/g, '');
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const form = document.querySelector('form[action*="/add-phone" i]')
              || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
            if (!form) return {ok:false, error:'missing_add_phone_form', url: location.href};
            const phoneInput = [...form.querySelectorAll(
              'input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]'
            )].find(visible);
            if (!phoneInput) return {ok:false, error:'missing_phone_input', url: location.href};

            let hidden = form.querySelector('input[name="phoneNumber"]');
            if (!hidden) {
              hidden = document.createElement('input');
              hidden.type = 'hidden';
              hidden.name = 'phoneNumber';
              form.appendChild(hidden);
            }

            let dialCode = forcedDial;
            let selectedText = '';
            const select = form.querySelector('select');
            if (select && select.selectedIndex >= 0) {
              const opt = select.options[select.selectedIndex];
              selectedText = String(opt.textContent || opt.label || '').replace(/\s+/g, ' ').trim();
            }
            const combo = form.querySelector('button[aria-haspopup="listbox"], [role="combobox"]');
            if (combo && !selectedText) {
              selectedText = String(combo.innerText || combo.textContent || '').replace(/\s+/g,' ').trim();
            }

            let visibleValue = preferredVisible || e164;
            if (!preferredVisible && dialCode && digits.startsWith(dialCode) && digits.length > dialCode.length + 3) {
              visibleValue = digits.slice(dialCode.length) || e164;
            }

            const setNativeValue = (el, value) => {
              const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              el.focus();
              if (setter) setter.call(el, ''); else el.value = '';
              el.dispatchEvent(new Event('input', {bubbles:true}));
              el.dispatchEvent(new Event('change', {bubbles:true}));
              if (setter) setter.call(el, value); else el.value = value;
              el.dispatchEvent(new Event('input', {bubbles:true}));
              el.dispatchEvent(new Event('change', {bubbles:true}));
              try {
                el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data: String(value)}));
              } catch (e) {}
            };

            phoneInput.scrollIntoView({block:'center'});
            setNativeValue(phoneInput, visibleValue);
            // 始终强制 hidden = 完整 E.164（提交真相源）
            const hsetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
            if (hsetter) hsetter.call(hidden, e164); else hidden.value = e164;
            hidden.dispatchEvent(new Event('input', {bubbles:true}));
            hidden.dispatchEvent(new Event('change', {bubbles:true}));
            phoneInput.blur();
            document.body?.focus?.();
            return {
              ok: true,
              e164,
              visibleValue,
              actualVisible: phoneInput.value || '',
              hiddenValue: hidden.value || '',
              dialCode,
              selectedText,
              fillMode: preferredVisible ? 'preferred' : 'auto',
              url: location.href,
            };
            """,
            e164,
            guessed_cc,
            fill_value,
        )
        last_result = result if isinstance(result, dict) else {"ok": False, "error": "bad_script_result"}
        if not last_result.get("ok"):
            continue

        time.sleep(0.45)
        # 再强制 hidden，防止 React 在 change 后又改回 +1
        hidden_forced = _force_hidden_phone_e164(driver, e164)
        last_result["hiddenValue"] = hidden_forced

        try:
            reread = driver.execute_script(
                r"""
                const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                const form = document.querySelector('form[action*="/add-phone" i]')
                  || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
                if (!form) return null;
                const phoneInput = [...form.querySelectorAll(
                  'input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]'
                )].find(visible);
                const hidden = form.querySelector('input[name="phoneNumber"]');
                return {
                  actualVisible: phoneInput ? (phoneInput.value || '') : '',
                  hiddenValue: hidden ? (hidden.value || '') : '',
                };
                """
            ) or {}
            if reread:
                last_result["actualVisible"] = reread.get("actualVisible", last_result.get("actualVisible"))
                # 若 reread hidden 被污染，再次强制
                hv = str(reread.get("hiddenValue") or "")
                if digits_only(hv) != e164_digits:
                    last_result["hiddenValue"] = _force_hidden_phone_e164(driver, e164)
                else:
                    last_result["hiddenValue"] = hv
        except Exception:
            pass

        # 回读真实 UI 国家区号
        ui_country = _read_phone_country_state(driver)
        selected_country_dial = digits_only(ui_country.get("dialCode")) or digits_only(country_sel.get("dialCode")) or guessed_cc
        if ui_country.get("selectedText"):
            last_result["selectedText"] = ui_country.get("selectedText")
        last_result["dialCode"] = selected_country_dial
        last_result["countryUi"] = {
            k: ui_country.get(k) for k in ("dialCode", "selectedText", "selectValue", "comboboxText")
        }

        actual = str(last_result.get("actualVisible") or "").strip()
        hidden_value = str(last_result.get("hiddenValue") or "").strip()
        dial = str(selected_country_dial or guessed_cc or "")

        # 最终再写一次 hidden
        if digits_only(hidden_value) != e164_digits:
            hidden_value = _force_hidden_phone_e164(driver, e164)
            last_result["hiddenValue"] = hidden_value

        # 非美号：国家区号必须正确，否则 hidden 正确也会被前端按 +1 校验截断
        if guessed_cc and guessed_cc != "1" and not country_dial_matches(selected_country_dial, guessed_cc):
            # 再强选一次
            retry_sel = _select_phone_country_dial(driver, guessed_cc)
            country_sel = retry_sel if retry_sel.get("ok") else country_sel
            ui_country = _read_phone_country_state(driver)
            selected_country_dial = digits_only(ui_country.get("dialCode")) or selected_country_dial
            last_result["dialCode"] = selected_country_dial
            if not country_dial_matches(selected_country_dial, guessed_cc):
                raise RuntimeError(
                    f"country_select_failed: 填号后国家仍不对 want=+{guessed_cc} "
                    f"got=+{selected_country_dial or '-'} text={ui_country.get('selectedText')!r} "
                    f"visible={actual!r} hidden={hidden_value!r}"
                )

        matched = phone_visible_matches_expected(
            actual,
            e164,
            dial_code=guessed_cc or dial,
            hidden_value=hidden_value or e164,
            selected_country_dial=selected_country_dial,
            require_country_match=bool(guessed_cc and guessed_cc != "1"),
        )
        if matched:
            last_result["fillMode"] = mode
            last_result["matched"] = True
            last_result["countrySelect"] = country_sel
            logger.info(
                "[Codex][Browser] 手机号已写入 mode=%s e164=%s visible=%s hidden=%s dial=%s country=%s",
                mode,
                e164,
                actual,
                hidden_value,
                dial or "-",
                str(last_result.get("selectedText") or country_sel.get("selectedText") or "-")[:40],
            )
            return last_result

        logger.info(
            "[Codex][Browser] 手机号填写后未对齐，换写法重试 mode=%s actual=%s hidden=%s expected=%s dial=%s country_ui=%s",
            mode,
            actual,
            hidden_value,
            e164,
            dial or "-",
            last_result.get("countryUi"),
        )

    actual = str((last_result or {}).get("actualVisible") or "").strip()
    hidden_value = str((last_result or {}).get("hiddenValue") or "").strip()
    raise RuntimeError(
        f"phone_fill_mismatch: 手机号校验失败 expected={e164_digits} "
        f"actual={actual} hidden={hidden_value} result={last_result} "
        f"country={country_sel} state={_phone_page_state(driver)}"
    )


def _blur_active_input_and_wait(driver, *, label: str = "输入完成") -> None:
    """输入手机号后移开焦点，并给前端校验/格式化留处理时间。"""
    try:
        driver.execute_script(r"""
        const active = document.activeElement;
        if (active && typeof active.blur === 'function') active.blur();
        document.body?.focus?.();
        document.dispatchEvent(new Event('change', {bubbles:true}));
        """)
    except Exception:
        pass
    seconds = random.uniform(1.8, 3.2)
    logger.info("[Codex][Browser] %s，已移开焦点，等待页面处理 %.1f 秒", label, seconds)
    time.sleep(seconds)


def _verify_add_phone_value_before_submit(driver, expected_e164: str) -> dict:
    expected = normalize_e164(expected_e164)
    expected_digits = digits_only(expected)
    expected_dial = guess_dial_code(expected)
    # 提交前最后一次强制 hidden，避免 blur 后被 React 改成 +1
    hidden_value = _force_hidden_phone_e164(driver, expected)
    result = driver.execute_script(
        r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const form = document.querySelector('form[action*="/add-phone" i]')
          || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
        if (!form) return {ok:false, error:'missing_add_phone_form', url: location.href};
        const input = [...form.querySelectorAll(
          'input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]'
        )].find(visible);
        const hidden = form.querySelector('input[name="phoneNumber"]');
        const visibleValue = String(input?.value || '').trim();
        const hiddenValue = String(hidden?.value || '').trim();
        const digits = value => String(value || '').replace(/\D+/g, '');
        return {
          ok: true,
          visibleValue, hiddenValue,
          visibleDigits: digits(visibleValue),
          hiddenDigits: digits(hiddenValue),
          hasHidden: !!hidden,
          url: location.href
        };
        """
    ) or {}
    if not result.get("ok"):
        raise RuntimeError(
            f"phone_fill_mismatch: 手机号提交前读取失败 result={result} state={_phone_page_state(driver)}"
        )
    visible_value = str(result.get("visibleValue") or "")
    hidden_value = str(result.get("hiddenValue") or hidden_value or "")
    if digits_only(hidden_value) != expected_digits:
        hidden_value = _force_hidden_phone_e164(driver, expected)
        result["hiddenValue"] = hidden_value
        result["hiddenDigits"] = digits_only(hidden_value)

    ui_country = _read_phone_country_state(driver)
    selected_country_dial = digits_only(ui_country.get("dialCode"))
    # 非美号提交前必须国家正确
    if expected_dial and expected_dial != "1" and not country_dial_matches(selected_country_dial, expected_dial):
        # 再试一次选国
        retry = _select_phone_country_dial(driver, expected_dial)
        ui_country = _read_phone_country_state(driver)
        selected_country_dial = digits_only(ui_country.get("dialCode"))
        if not country_dial_matches(selected_country_dial, expected_dial):
            raise RuntimeError(
                f"country_select_failed: 提交前国家/区号仍不对 want=+{expected_dial} "
                f"got=+{selected_country_dial or '-'} text={ui_country.get('selectedText')!r} "
                f"retry={retry} visible={visible_value} hidden={hidden_value}"
            )
        # 选国成功后可能重置号码，重新强制 hidden
        hidden_value = _force_hidden_phone_e164(driver, expected)
        result["hiddenValue"] = hidden_value

    if not phone_visible_matches_expected(
        visible_value,
        expected,
        dial_code=expected_dial,
        hidden_value=hidden_value or expected,
        selected_country_dial=selected_country_dial or expected_dial,
        require_country_match=bool(expected_dial and expected_dial != "1"),
    ):
        raise RuntimeError(
            f"phone_fill_mismatch: 手机号提交前校验失败 expected={expected} "
            f"visible={visible_value} hidden={hidden_value} "
            f"country=+{selected_country_dial or '-'} text={ui_country.get('selectedText')!r} "
            f"state={_phone_page_state(driver)}"
        )
    result["expected"] = expected
    result["expectedDigits"] = expected_digits
    result["countryDial"] = selected_country_dial or expected_dial
    result["countryText"] = ui_country.get("selectedText")
    result["matched"] = True
    return result


def _wait_page_settle_after_submit() -> None:
    """点击提交后先等待页面处理，再检查发送状态。"""
    seconds = random.uniform(2.0, 4.0)
    logger.info("[Codex][Browser] 已点击提交，等待页面发送/跳转处理 %.1f 秒后检查状态", seconds)
    time.sleep(seconds)


def _refresh_add_phone_for_retry(driver, *, reason: str = "") -> None:
    """发送失败/换号前刷新手机号页，避免旧错误状态和旧号码残留。"""
    try:
        logger.info("[Codex][Browser] 发送失败/准备换号，刷新手机号页面：%s", reason or "retry")
        driver.refresh()
        human_delay("navigate")
        try:
            _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=8)
            return
        except Exception:
            pass
        # 如果刷新后仍不在输入页，强制回 add-phone。
        target = _auth_origin(driver).rstrip("/") + "/add-phone"
        logger.info("[Codex][Browser] 刷新后未找到手机号输入框，重新打开：%s", target)
        driver.get(target)
        human_delay("navigate")
        _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=8)
    except Exception as exc:
        logger.info("[Codex][Browser] 刷新手机号页失败，下一轮会再次尝试回到 add-phone：%s", str(exc)[:180])


def _click_add_phone_continue_button(driver, *, timeout: int = 10) -> dict:
    """点击 add-phone 表单里的 Continue/続行 按钮。

    参考 FlowPilot 的 getAddPhoneSubmitButton + simulateClick：优先在 add-phone form 内找
    enabled submit，点击失败时用 form.requestSubmit(button) 兜底。
    """
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            btn = driver.execute_script(r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const enabled = el => {
              if (!el) return false;
              if (el.disabled) return false;
              if (String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return false;
              return true;
            };
            const form = document.querySelector('form[action*="/add-phone" i]')
              || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
            if (!form) return null;
            const buttons = [...form.querySelectorAll('button[type="submit"], input[type="submit"]')];
            return buttons.find(b => visible(b) && enabled(b) && (b.getAttribute('data-dd-action-name') || '').toLowerCase() === 'continue')
              || buttons.find(b => visible(b) && enabled(b))
              || buttons.find(b => visible(b))
              || null;
            """)
            if btn:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(random.uniform(0.3, 0.8))
                try:
                    text = str(getattr(btn, 'text', '') or btn.get_attribute('value') or btn.get_attribute('data-dd-action-name') or '').strip()
                except Exception:
                    text = ''
                try:
                    btn.click()
                    _wait_page_settle_after_submit()
                    return {"ok": True, "method": "click", "text": text}
                except Exception as click_exc:
                    last = click_exc
                    submitted = driver.execute_script(r"""
                    const btn = arguments[0];
                    const form = btn?.form || btn?.closest?.('form');
                    if (form && typeof form.requestSubmit === 'function') {
                      form.requestSubmit(btn);
                      return true;
                    }
                    if (btn && typeof btn.click === 'function') {
                      btn.click();
                      return true;
                    }
                    return false;
                    """, btn)
                    if submitted:
                        _wait_page_settle_after_submit()
                        return {"ok": True, "method": "requestSubmit", "text": text, "click_error": str(click_exc)[:160]}
        except Exception as exc:
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"submit_missing: add-phone Continue/続行 submit button not found last={last} state={_phone_page_state(driver)}")


def _force_submit_add_phone_form(driver) -> dict:
    """add-phone 页面点击按钮没生效时，直接 requestSubmit 当前 form。"""
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const form = document.querySelector('form[action*="/add-phone" i]')
          || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
        if (!form) return {ok:false, reason:'missing_form', url: location.href};
        const btn = [...form.querySelectorAll('button[type="submit"],input[type="submit"]')]
          .find(el => visible(el) && !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true')
          || form.querySelector('button[type="submit"],input[type="submit"]');
        if (btn) btn.scrollIntoView({block:'center'});
        if (typeof form.requestSubmit === 'function') form.requestSubmit(btn || undefined);
        else if (btn && typeof btn.click === 'function') btn.click();
        else form.submit();
        return {ok:true, method: btn ? 'requestSubmit(button)' : 'requestSubmit(form)', url: location.href};
        """) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "url": getattr(driver, "current_url", "")}


def _wait_after_phone_send(driver, timeout: int = 12) -> str:
    end = time.time() + timeout
    last = {}
    force_submitted = False
    sms_reasserted = False
    while time.time() < end:
        time.sleep(1)
        # 国家 listbox 若仍展开，bodyText 会变成整表国家名，误导分类
        try:
            st = _read_phone_country_state(driver)
            if int(st.get("optionCount") or 0) > 5 or st.get("listboxOpen"):
                _dismiss_phone_country_dropdown(driver)
        except Exception:
            pass
        last = _phone_page_state(driver)
        # 必须优先判断验证码页：页面文案里可能包含 send/limit/check 等词，不能把
        # “Check your phone / Enter the verification code...” 误判成发送失败。
        if _is_phone_code_state(last):
            return 'code_page'
        body = str(last.get('bodyText') or '')
        ch = _sms_channel_selection_state(driver)
        # WhatsApp 被 React 回勾：先抢救一次 SMS 再提交，而不是立刻判死
        if (
            not sms_reasserted
            and ch.get("hasWhatsapp")
            and ch.get("whatsappChecked")
            and not ch.get("smsChecked")
            and _is_add_phone_page(driver)
        ):
            logger.warning("[Codex][Browser] 提交后检测到 WhatsApp 回勾，重新选择 SMS 并再提交一次")
            try:
                _select_sms_channel_or_raise(driver)
                _force_submit_add_phone_form(driver)
                sms_reasserted = True
                force_submitted = True
                time.sleep(2)
                continue
            except Exception as exc:
                logger.warning("[Codex][Browser] SMS 重选失败：%s", str(exc)[:160])
                sms_reasserted = True
        reason = _classify_phone_page_failure(last)
        if reason:
            raise RuntimeError(f"{reason}: {body[:240]}")
        # 仍在 add-phone 且字段有 aria-invalid，认为号码被拒。
        if _is_add_phone_page(driver):
            invalid = any(str(i.get('ariaInvalid') or '').lower() == 'true' for i in (last.get('inputs') or []))
            if invalid:
                raise RuntimeError(f"invalid_phone: add-phone input aria-invalid state={last}")
            # Cloak/React-Aria 场景下 btn.click 可能只聚焦没触发表单提交；补一次 requestSubmit。
            if not force_submitted and time.time() > end - timeout + 3:
                info = _force_submit_add_phone_form(driver)
                logger.info("[Codex][Browser] add-phone 点击后仍停留本页，补执行 form.requestSubmit：%s", info)
                force_submitted = True
                time.sleep(2)
    if _is_phone_code_state(last) or _is_phone_code_page(driver):
        return 'code_page'
    if _is_add_phone_page(driver):
        raise RuntimeError(f"send_not_accepted: 提交后仍停留在 add-phone state={last}")
    return 'unknown'


def _submit_phone_otp_form(driver) -> dict:
    """提交 phone-verification OTP：优先 form 内 Continue，失败 requestSubmit 兜底。"""
    try:
        info = driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const enabled = el => {
          if (!el) return false;
          if (el.disabled) return false;
          if (String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return false;
          return true;
        };
        const form = document.querySelector('form[action*="phone-verification" i], form[action*="phone-otp" i], form[action*="/phone" i]')
          || [...document.querySelectorAll('form')].find(f => {
               const a = (f.getAttribute('action') || '') + ' ' + (f.id || '');
               return /phone|otp|verif/i.test(a);
             })
          || document.querySelector('form');
        if (!form) return {ok:false, reason:'no_form', url: location.href};
        const buttons = [...form.querySelectorAll('button[type="submit"],input[type="submit"],button')].filter(visible);
        const score = (b) => {
          if (!enabled(b)) return -1;
          const t = [b.innerText, b.textContent, b.value, b.getAttribute('aria-label'), b.getAttribute('data-dd-action-name')].join(' ').toLowerCase();
          if (/(continue|verify|confirm|submit|next|続行|確認|验证|继续|次へ)/i.test(t)) return 100;
          if ((b.getAttribute('type') || '').toLowerCase() === 'submit') return 90;
          return 10;
        };
        const btn = buttons.map(b => [score(b), b]).filter(x => x[0] > 0).sort((a,b)=>b[0]-a[0])[0]?.[1];
        if (btn) {
          try { btn.scrollIntoView({block:'center'}); } catch (e) {}
          try { btn.click(); return {ok:true, method:'click', text:(btn.innerText||btn.value||'').slice(0,40), url:location.href}; }
          catch (e1) {
            try {
              if (typeof form.requestSubmit === 'function') { form.requestSubmit(btn); return {ok:true, method:'requestSubmit(btn)', url:location.href}; }
            } catch (e2) {}
          }
        }
        try {
          if (typeof form.requestSubmit === 'function') { form.requestSubmit(); return {ok:true, method:'requestSubmit(form)', url:location.href}; }
          form.submit();
          return {ok:true, method:'form.submit', url:location.href};
        } catch (e3) {
          return {ok:false, reason: String(e3 && e3.message || e3), url: location.href};
        }
        """) or {}
        return info if isinstance(info, dict) else {"ok": False, "reason": "bad_script"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _wait_after_phone_otp_submit(driver, timeout: int = 20) -> str:
    """手机验证码提交后等待结果。

    成功时通常会跳出 phone-verification，进入 consent/workspace/callback；不能在提交后
    3 秒立刻读取旧页面文案并按 send_limited 判失败。只有明确仍在手机号流程且出现错误时
    才返回失败。
    """
    end = time.time() + timeout
    last = {}
    last_log = 0.0
    while time.time() < end:
        time.sleep(1)
        current = str(getattr(driver, "current_url", "") or "")
        if _is_callback_url(current):
            return "callback"
        last = _phone_page_state(driver)
        # 已离开手机验证码/加手机号页面，说明验证码被接受，后续交给 consent/callback 流程。
        if not _is_phone_code_state(last) and not _is_add_phone_page(driver):
            return "left_phone_flow"
        # 仍在验证码页时，只把明确错误当失败；普通 Check your phone 页面继续等。
        if _is_phone_code_state(last):
            inputs = last.get('inputs') or []
            invalid = any(str(i.get('ariaInvalid') or '').lower() == 'true' for i in inputs)
            body = str(last.get('bodyText') or '').lower()
            if invalid or any(k in body for k in (
                'invalid code', 'incorrect code', 'wrong code', 'expired code',
                'code is invalid', 'code was invalid', '验证码无效', '验证码错误', '验证码已过期',
                '認証コードが無効', 'コードが正しく',
            )):
                raise RuntimeError(f"invalid_phone_code: {(last.get('bodyText') or '')[:240]}")
            if time.time() - last_log > 8:
                logger.info(
                    "[Codex][Browser] 手机 OTP 提交后仍在验证码页，继续等待 url=%s",
                    current[:160],
                )
                last_log = time.time()
            continue
        reason = _classify_phone_page_failure(last)
        if reason:
            raise RuntimeError(f"{reason}: {(last.get('bodyText') or '')[:240]}")
    # 超时：仍停验证码页 = 未通过，不能当成功
    current = str(getattr(driver, "current_url", "") or "")
    if _is_callback_url(current):
        return "callback"
    last = _phone_page_state(driver)
    if not _is_phone_code_state(last) and not _is_add_phone_page(driver):
        return "left_phone_flow"
    if _is_phone_code_state(last):
        return "still_code_page"
    return "unknown"


def _classify_phone_page_failure(state: dict) -> str:
    if _is_phone_code_state(state):
        return ''
    radios = state.get('radios') or []
    whatsapp_checked = any(
        'whatsapp' in str(r.get('value', '')).lower().replace(' ', '') and r.get('checked')
        for r in radios
    )
    sms_checked = any(
        str(r.get('value', '')).lower().replace(' ', '') in ('sms', 'text', 'textmessage', 'text_message', 'text-message')
        and r.get('checked')
        for r in radios
    )
    reason = classify_phone_failure_reason(
        body_text=str(state.get('bodyText') or ''),
        whatsapp_checked=whatsapp_checked if radios else None,
        sms_checked=sms_checked if radios else None,
        radios_present=bool(radios),
    )
    return reason

def _sleep_before_phone_retry(attempt: int, max_retries: int, *, prefix: str = "[Codex][Browser]") -> None:
    """换号前随机等待，至少 3 秒，避免连续提交号码过快。"""
    if attempt >= max_retries:
        return
    seconds = random.uniform(3.0, 8.0)
    logger.info("%s 换号前随机等待 %.1f 秒", prefix, seconds)
    time.sleep(seconds)


def _do_phone_verification_if_present(driver) -> None:
    """如果页面要求手机号验证，则用当前 sms_provider 自动完成。"""
    provider = str(getattr(sms_provider._cfg, "SMS_PROVIDER", "") or "").strip().lower() if hasattr(sms_provider, "_cfg") else ""
    http = sms_provider._http()
    max_retries = int(getattr(sms_provider._cfg, "SMS_MAX_RETRIES", 10) or 10) if hasattr(sms_provider, "_cfg") else 10
    try:
        # 如果页面没有手机号输入框，直接返回。
        try:
            end_detect = time.time() + 8
            while time.time() < end_detect and not _has_strict_add_phone_form(driver):
                # 如果已经在验证码页，说明手机步骤之前已提交过；继续处理验证码页，不应当跳过。
                if _is_phone_code_page(driver):
                    break
                time.sleep(0.5)
            if not (_has_strict_add_phone_form(driver) or _is_phone_code_page(driver)):
                raise RuntimeError("not_phone_flow")
        except Exception:
            logger.info("[Codex][Browser] 未检测到手机号验证页，跳过手机步骤")
            return

        last_err = None
        for attempt in range(1, max_retries + 1):
            activation_id = None
            try:
                activation_id, phone = sms_provider.acquire_number(http)
                logger.info("[Codex][Browser] 手机验证尝试 %s/%s，provider=%s，号码=+%s", attempt, max_retries, provider, phone)
                logger.info("[Codex][Browser] 准备手机号输入页，重新设置新手机号")
                _ensure_add_phone_input(driver, reason=f"attempt-{attempt}")
                phone_fill = _set_phone_value(driver, f"+{phone}", timeout=10)
                logger.info(
                    "[Codex][Browser] 已重新设置手机号：e164=%s visible=%s hidden=%s dialCode=%s country=%s",
                    phone_fill.get("e164"), phone_fill.get("actualVisible"), phone_fill.get("hiddenValue") or "-",
                    phone_fill.get("dialCode") or "-", (str(phone_fill.get("selectedText") or "-") + (" [changed]" if phone_fill.get("selectedChanged") else "")),
                )
                _blur_active_input_and_wait(driver, label="手机号输入完成")
                phone_verify = _verify_add_phone_value_before_submit(driver, str(phone_fill.get("e164") or f"+{phone}"))
                logger.info("[Codex][Browser] 手机号提交前校验通过：visible=%s hidden=%s country=+%s text=%s",
                            phone_verify.get("visibleValue"), phone_verify.get("hiddenValue") or "-",
                            phone_verify.get("countryDial") or "-", str(phone_verify.get("countryText") or "-")[:40])
                logger.info("[Codex][Browser] 检查并选择 SMS 短信通道")
                _dismiss_phone_country_dropdown(driver)
                _select_sms_channel_or_raise(driver)
                _blur_active_input_and_wait(driver, label="短信通道确认完成")
                # 提交前再锁一次 SMS（blur/格式化后 React 可能回勾 WhatsApp）
                _select_sms_channel_or_raise(driver)
                submit_info = _click_add_phone_continue_button(driver, timeout=10)
                logger.info("[Codex][Browser] 已点击手机号 Continue/続行 按钮：%s，等待进入短信验证码页", submit_info)
                _wait_page_settle_after_submit()

                # 等待页面进入 phone-verification；若号码无效/无法发送/WhatsApp 通道，立即换号。
                _wait_after_phone_send(driver, timeout=15)
                logger.info("[Codex][Browser] 已进入手机验证码页")

                # setStatus=1 失败（如 BAD_STATUS）绝不能换号：号码可能已在路上收码
                sms_provider.mark_sms_sent(activation_id, http=http)
                logger.info(
                    "[Codex][Browser] 短信已发送，开始轮询验证码 activation_id=%s wait=%ss interval=%ss",
                    activation_id, sms_provider._cfg.SMS_CODE_WAIT, sms_provider._cfg.SMS_POLL_INTERVAL
                )
                sms_code = str(sms_provider.wait_for_sms_code(activation_id, http) or "").strip()
                if not sms_code:
                    raise RuntimeError(f"empty_phone_otp: activation_id={activation_id}")
                logger.info("[Codex][Browser] 手机 OTP 收到：%s", sms_code)

                otp_outcome = "unknown"
                for otp_try in range(1, 3):
                    _clear_otp_inputs(driver)
                    _type_otp(driver, sms_code)
                    logger.info("[Codex][Browser] 已填写手机 OTP（第 %s/2 次提交）", otp_try)
                    human_delay("otp_input")
                    submit_info = _submit_phone_otp_form(driver)
                    if not submit_info.get("ok"):
                        # 旧路径兜底
                        clicked = _click_if_present(
                            driver,
                            [
                                "button[type='submit']",
                                "input[type='submit']",
                                "//button[contains(., 'Continue')]",
                                "//button[contains(., 'Verify')]",
                                "//button[contains(., '続行')]",
                                "//button[contains(., '验证')]",
                            ],
                            timeout=6,
                        )
                        submit_info = {"ok": clicked, "method": "legacy_click" if clicked else "none"}
                    if not submit_info.get("ok"):
                        raise RuntimeError(
                            f"verify_submit_missing: phone verification submit not found "
                            f"submit={submit_info} state={_phone_page_state(driver)}"
                        )
                    logger.info("[Codex][Browser] 已提交手机 OTP：%s，等待验证结果", submit_info)
                    otp_outcome = _wait_after_phone_otp_submit(driver, timeout=30)
                    logger.info("[Codex][Browser] 手机 OTP 提交后状态：%s", otp_outcome)
                    if otp_outcome in ("callback", "left_phone_flow"):
                        break
                    if otp_outcome == "still_code_page" and otp_try < 2:
                        logger.warning(
                            "[Codex][Browser] 手机 OTP 提交后仍停在验证码页，重填并再提交一次 code=%s",
                            sms_code,
                        )
                        time.sleep(1.0)
                        continue
                    break

                # 仍停在验证码页 = 码无效/未真正提交成功，不能 complete 后假装过关去空等 callback
                if otp_outcome == "still_code_page":
                    raise RuntimeError(
                        f"invalid_phone_code: 手机 OTP 提交后仍停留在验证码页 code={sms_code} "
                        f"state={_phone_page_state(driver)}"
                    )
                if otp_outcome not in ("callback", "left_phone_flow"):
                    raise RuntimeError(
                        f"phone_otp_not_accepted: outcome={otp_outcome} code={sms_code} "
                        f"state={_phone_page_state(driver)}"
                    )
                sms_provider.complete(activation_id, http)
                return
            except Exception as exc:
                last_err = exc
                logger.warning("[Codex][Browser] 手机验证尝试失败，换号：%s", str(exc)[:240])
                if activation_id:
                    # OpenAI 发码拒绝/号码无效：冷却该供应商槽，而不是整国。
                    # 禁止用整段异常字符串里的 "whatsapp"（state dump 常含该词）误判。
                    ch = _sms_channel_selection_state(driver)
                    reason = classify_phone_failure_reason(
                        error_message=str(exc),
                        whatsapp_checked=ch.get("whatsappChecked") if ch.get("radioCount") else None,
                        sms_checked=ch.get("smsChecked") if ch.get("radioCount") else None,
                        radios_present=bool(ch.get("radioCount")),
                    ) or "send_reject"
                    # 统一冷却 reason 命名
                    if reason == "whatsapp_channel":
                        reason = "whatsapp"
                    try:
                        logger.info(
                            "[Codex][Browser] 标记号码槽位冷却 activation_id=%s reason=%s channel=%s",
                            activation_id, reason, ch,
                        )
                        sms_provider.mark_activation_send_rejected(activation_id, reason=reason)
                    except Exception:
                        pass
                    try:
                        sms_provider.cancel(activation_id, http)
                    except Exception:
                        pass
                if "invalid_auth_step" in str(exc):
                    raise RuntimeError(
                        "手机号流程进入 invalid_auth_step，说明授权状态还未从 email-verification 正常跳转或已失效；"
                        "已停止继续换号，避免继续消耗号码"
                    ) from exc
                # 如果已经离开手机号/验证码相关页面，认为通过或不再需要；
                # 如果仍在 phone-verification，则下一轮必须回 add-phone 重新填新号码再提交。
                try:
                    if _is_phone_code_page(driver):
                        logger.info("[Codex][Browser] 当前仍在手机验证码页，下一轮将返回 add-phone 重新设置新号码")
                    else:
                        _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=2)
                except Exception:
                    if _is_add_phone_page(driver) or _is_phone_code_page(driver):
                        logger.info("[Codex][Browser] 仍处于手机号流程，继续换号重试")
                    else:
                        logger.info("[Codex][Browser] 手机输入页已消失，继续后续流程")
                        return
                if attempt < max_retries:
                    _refresh_add_phone_for_retry(driver, reason=str(exc)[:120])
                _sleep_before_phone_retry(attempt, max_retries)
        raise RuntimeError(f"Roxy 手机验证重试 {max_retries} 次仍失败，最后错误：{last_err}")
    finally:
        try:
            http.close()
        except Exception:
            pass


def _finish_consent_workspace(driver) -> str:
    """点击 Codex consent/workspace 页面里的继续/允许按钮，直到 callback。"""
    end = time.time() + int(_roxy_cfg.ROXY_CODEX_CALLBACK_TIMEOUT)
    last_url = ""
    last_log = 0.0
    stuck_phone_since = None
    while time.time() < end:
        callback = _extract_callback_url_from_any_window(driver)
        if callback:
            return callback
        current = str(driver.current_url or "")
        if current != last_url:
            logger.info("[Codex][Browser] 等待 callback/consent 当前 url=%s", current[:200])
            last_url = current
            stuck_phone_since = None

        # 仍停在手机验证码页：说明 OTP 其实没过，别空等到 callback 超时
        if _is_phone_code_page(driver) or _is_add_phone_page(driver):
            if stuck_phone_since is None:
                stuck_phone_since = time.time()
            elif time.time() - stuck_phone_since > 12:
                raise RuntimeError(
                    f"phone_otp_not_accepted: 等待 callback 时仍停在手机号流程 url={current[:200]} "
                    f"state={_phone_page_state(driver)}"
                )
        else:
            stuck_phone_since = None

        clicked = False
        for selectors in [
            ["//button[contains(., 'Allow')]", "//button[contains(., 'Authorize')]", "//button[contains(., 'Continue')]"],
            ["//button[contains(., 'Select')]", "//button[contains(., 'Use workspace')]", "//button[contains(., 'Confirm')]"],
            ["//button[contains(., '允许')]", "//button[contains(., '授权')]", "//button[contains(., '继续')]", "//button[contains(., '确认')]"],
            ["button[type='submit']"],
        ]:
            if _click_if_present(driver, selectors, timeout=2):
                clicked = True
                human_delay("form")
                break
        if not clicked:
            if time.time() - last_log > 15:
                logger.info("[Codex][Browser] 仍在等待授权确认/callback，url=%s", current[:200])
                last_log = time.time()
            time.sleep(0.8)
    return _wait_for_callback(driver, timeout=5)




def clear_roxy_browser_auth_state(driver) -> None:
    """清空当前 Roxy 浏览器里的 OpenAI/ChatGPT 登录态与缓存，用于注册后复用同一环境跑 Codex。"""
    origins = [
        "https://auth.openai.com",
        "https://chatgpt.com",
        "https://openai.com",
        "https://platform.openai.com",
    ]
    logger.info("[Codex][Browser] 复用注册窗口：开始清理 Cookie / localStorage / sessionStorage / cache")
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
        logger.info("[Codex][Browser] 已清理浏览器 Cookie")
    except Exception as exc:
        logger.info("[Codex][Browser] 清理 Cookie 失败，继续尝试其它缓存：%s", str(exc)[:160])
    try:
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        logger.info("[Codex][Browser] 已清理浏览器 Cache")
    except Exception as exc:
        logger.info("[Codex][Browser] 清理 Cache 失败，继续：%s", str(exc)[:160])
    for origin in origins:
        try:
            driver.execute_cdp_cmd("Storage.clearDataForOrigin", {
                "origin": origin,
                "storageTypes": "all",
            })
            logger.info("[Codex][Browser] 已清理站点数据：%s", origin)
        except Exception as exc:
            logger.debug("[Codex][Browser] 清理站点数据失败 %s: %s", origin, exc)
    try:
        driver.get("about:blank")
    except Exception:
        pass
    time.sleep(1.0)
    logger.info("[Codex][Browser] 注册窗口登录态清理完成，准备开始 Codex 授权")

def _run_roxy_codex_oauth_once(
    email: str,
    otp_provider=None,
    proxy: str | None = None,
    force: bool = False,
    existing_driver=None,
    existing_opened=None,
    reuse_existing_profile: bool = False,
    clear_existing_state: bool = True,
    totp_secret: str | None = None,
) -> dict:
    """指纹浏览器 Codex OAuth 入口。

    existing_driver/existing_opened 用于“注册成功后立刻跑 Codex”：
    复用注册时的 Roxy 窗口，不新建环境，只清理浏览器状态后开始授权。
    """
    from core import codex_oauth as proto

    if not force and not proto._cfg.ENABLE_CODEX_AUTO:
        return proto._codex_result(status="skipped", message="ENABLE_CODEX_AUTO=False")
    if not email:
        return proto._codex_result(status="skipped", message="email 为空")
    if otp_provider is None:
        otp_provider = wait_for_otp

    client = None if reuse_existing_profile else RoxyBrowserClient()
    opened = existing_opened if reuse_existing_profile else client.open_profile()
    browser_kind_token = _CODEX_BROWSER_KIND.set(_detect_browser_kind(opened))
    driver = existing_driver if reuse_existing_profile else None
    owns_driver = not reuse_existing_profile
    try:
        auth_source = proto._codex_auth_url_source()
        code_verifier = None
        sub2_auth = None
        if auth_source == "cpa":
            cpa_auth = proto._request_cpa_authorize_url()
            state = cpa_auth["state"]
            auth_url = cpa_auth["auth_url"]
            logger.info("[Codex][Browser] 当前使用 CPA 授权地址: %s", auth_url)
        elif auth_source == "sub2":
            sub2_auth = proto._request_sub2_authorize_url()
            state = sub2_auth["state"]
            auth_url = sub2_auth["auth_url"]
            logger.info("[Codex][Browser] 当前使用 sub2api 授权地址: %s", auth_url)
        elif auth_source == "local":
            code_verifier, code_challenge = proto._generate_pkce()
            state = proto._generate_state()
            auth_url = proto._build_authorize_url(state, code_challenge, prompt="login")
            logger.info("[Codex][Browser] 当前使用本地 PKCE 授权地址: %s", auth_url)
        else:
            raise RuntimeError(f"[Codex][Browser] 不支持的 CODEX_AUTH_URL_SOURCE={auth_source!r}")

        if not driver:
            driver = _build_driver(opened)
            _center_browser_window(driver)
        driver.set_page_load_timeout(int(_roxy_cfg.ROXY_SELENIUM_TIMEOUT))
        logger.info("[Codex][Browser] 开始授权：%s，profile=%s，reuse_existing_profile=%s", email, opened.profile_id, reuse_existing_profile)
        if reuse_existing_profile and clear_existing_state:
            clear_roxy_browser_auth_state(driver)

        secret = load_totp_secret(email, totp_secret)
        if secret:
            logger.info("[Codex][Browser] 账号已配置 TOTP secret，登录流将自动填写 2FA")
        _fill_email_and_otp(driver, email, otp_provider, auth_url, totp_secret=secret)
        # 兜底：邮箱 OTP 后到手机号前再扫一次 TOTP
        _fill_totp_if_present(driver, email, totp_secret=secret, timeout=8)
        human_delay("api")
        logger.info("[Codex][Browser] 检查是否需要手机号验证")
        _do_phone_verification_if_present(driver)
        logger.info("[Codex][Browser] 手机验证处理完成/无需处理，等待授权确认和 callback")
        callback_url = _finish_consent_workspace(driver)
        code = proto._extract_code(callback_url, state)
        logger.info("[Codex][Browser] 已捕获 callback code：%s...", code[:24])

        if auth_source == "cpa":
            submit_payload = proto._submit_cpa_callback(callback_url)
            path = proto._save_cpa_local_record(
                email=email,
                callback_url=callback_url,
                auth_url=auth_url,
                state=state,
                submit_payload=submit_payload,
            )
            msg = submit_payload.get("message") or submit_payload.get("status_message") or "CPA callback submitted"
            return proto._codex_result(
                status="success",
                ok=True,
                email=email,
                file_path=str(path) if path else None,
                callback_url=callback_url,
                message=f"{_codex_driver_name()}: {msg}",
            )

        if auth_source == "sub2":
            submit_payload = proto._submit_sub2_callback(
                callback_url,
                session_id=(sub2_auth or {}).get("session_id", ""),
                redirect_uri=(proto.parse_qs(proto.urlparse(auth_url or "").query).get("redirect_uri") or [""])[0],
                name=email,
            )
            path = proto._save_sub2_local_record(
                email=email,
                callback_url=callback_url,
                auth_url=auth_url,
                state=state,
                submit_payload=submit_payload,
            )
            msg = submit_payload.get("message") or submit_payload.get("status_message") or "sub2 callback uploaded"
            return proto._codex_result(
                status="success",
                ok=True,
                email=email,
                file_path=str(path) if path else None,
                callback_url=callback_url,
                message=f"{_codex_driver_name()}: {msg}",
            )

        done = proto.complete_local_codex_oauth(
            email=email,
            code=code,
            code_verifier=code_verifier or "",
            callback_url=callback_url,
            proxy=proxy,
        )
        return proto._codex_result(
            status="success",
            ok=True,
            email=done.get("email") or email,
            file_path=str(done.get("path") or ""),
            callback_url=callback_url,
            message=f"{_codex_driver_name()} local plan={done.get('plan') or 'unknown'}",
        )
    except AccountUnusableError as exc:
        logger.warning("[Codex][Browser] 账号已废：%s，%s", email, exc.error_code)
        return proto._codex_result(
            status="deactivated",
            email=email,
            message=f"账号已废（{exc.error_code or 'account_deactivated'}）",
        )
    except Exception as exc:
        logger.warning("[Codex][Browser] 失败：%s，%s: %s", email, type(exc).__name__, str(exc)[:240])
        logger.debug("[Codex][Browser] 失败详情", exc_info=True)
        return proto._codex_result(status="failed", email=email, message=f"{type(exc).__name__}: {str(exc)[:220]}")
    finally:
        # 注册后复用窗口时，driver/profile 生命周期由注册流程统一清理，
        # 这里不能 quit/delete，否则会提前销毁注册环境。
        if owns_driver and driver and not bool(_roxy_cfg.ROXY_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
        if owns_driver and client and not bool(_roxy_cfg.ROXY_KEEP_BROWSER_OPEN):
            client.cleanup_profile(opened)
        try:
            _CODEX_BROWSER_KIND.reset(browser_kind_token)
        except Exception:
            pass


def run_roxy_codex_oauth(
    email: str,
    otp_provider=None,
    proxy: str | None = None,
    force: bool = False,
    existing_driver=None,
    existing_opened=None,
    reuse_existing_profile: bool = False,
    clear_existing_state: bool = True,
    totp_secret: str | None = None,
) -> dict:
    """指纹浏览器 Codex OAuth 入口；CPA callback 409 timeout 时重新开启一轮授权。"""
    from core import codex_oauth as proto

    max_rounds = 2
    last_result = None
    for round_no in range(1, max_rounds + 1):
        if round_no > 1:
            logger.warning(
                "[Codex][Browser] CPA callback 返回 Timeout waiting for OAuth callback，重新开启第 %s/%s 轮 Codex 授权：%s",
                round_no, max_rounds, email,
            )
        result = _run_roxy_codex_oauth_once(
            email=email,
            otp_provider=otp_provider,
            proxy=proxy,
            force=force,
            existing_driver=existing_driver,
            existing_opened=existing_opened,
            reuse_existing_profile=reuse_existing_profile,
            clear_existing_state=clear_existing_state,
            totp_secret=totp_secret,
        )
        last_result = result
        if result.get("ok"):
            return result
        msg = result.get("message") or result.get("error") or ""
        if not proto._is_cpa_callback_reauth_error(msg):
            return result
    if last_result:
        last_result = dict(last_result)
        last_result["message"] = f"CPA callback 超时，已重新授权 {max_rounds} 轮仍失败：{last_result.get('message') or ''}"
        return last_result
    return proto._codex_result(status="failed", email=email, message="CPA callback 超时，重新授权失败")
