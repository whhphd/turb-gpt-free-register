# -*- coding: utf-8 -*-
"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from config import cloakbrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data
from core.cloakbrowser_driver import build_cloak_driver
from core.email_provider import wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay

# 复用 Roxy 注册流程里已维护好的页面操作函数。
from core.roxy_registration import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _clear_otp_inputs, _type_otp, _click_continue, _wait_after_email_otp_submit,
    _click_resend_email_otp, _complete_profile_page, _fetch_chatgpt_session, _check_manual_stop,
    _otp_flow_already_passed, _is_auth_route_error_page, _recover_auth_route_error,
    _restart_email_login_flow,
)

logger = logging.getLogger(__name__)


def run_cloak_registration(
    email: str,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
    *,
    keep_browser: bool = False,
    skip_codex: bool = False,
) -> dict:
    """CloakBrowser 自动化注册入口。

    keep_browser=True：注册成功后不关闭浏览器，结果里带回 driver/opened（调用方负责最终 quit）。
    skip_codex=True：强制跳过 Codex 自动授权（如 GCash 提链会话）。
    """
    driver = None
    opened = None
    create_acknowledged = False
    openai_password: str | None = None
    hold_browser = False  # 仅成功且 keep_browser 时置 True，finally 不再 quit
    try:
        driver, opened = build_cloak_driver(proxy=proxy)
        logger.info("[Cloak注册] 开始：%s，profile=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        logger.info("[Cloak注册] 打开登录页：https://chatgpt.com/auth/login")
        driver.get("https://chatgpt.com/auth/login")
        human_delay("navigate")
        _maybe_accept(driver)
        _check_manual_stop()

        next_state = _submit_email_and_wait_next(driver, email, attempts=3)
        _check_manual_stop()

        openai_password = None if next_state == "otp" else _fill_password_page_if_present(driver, email, timeout=25)
        _check_manual_stop()

        current_otp = otp_code
        max_otp_attempts = 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                logger.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Cloak注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    try:
                        _click_resend_email_otp(driver, timeout=25)
                    except Exception as resend_exc:
                        if "auth_route_error" in str(resend_exc) or _is_auth_route_error_page(driver):
                            logger.warning("[Cloak注册][OTP] 等码阶段遇路由错误，重开邮箱登录：%s", str(resend_exc)[:160])
                            _restart_email_login_flow(driver, email)
                            otp_after_ts = time.time()
                        else:
                            raise
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Cloak注册][OTP] 收到验证码：%s", current_otp)
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            logger.info("[Cloak注册][OTP] 已填写邮箱验证码")
            human_delay("otp_input")
            try:
                _click_continue(driver)
                logger.info("[Cloak注册][OTP] 已提交邮箱验证码，等待资料页或登录态")
            except Exception as exc:
                logger.info("[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

            outcome = _wait_after_email_otp_submit(driver, timeout=20)
            if outcome == "accepted" or _otp_flow_already_passed(driver):
                if outcome != "accepted":
                    logger.info("[Cloak注册][OTP] 等待结果=%s，但已离开验证码页，按通过处理 url=%s", outcome, getattr(driver, "current_url", ""))
                break

            # 废号：立刻终止，交给上层停用邮箱
            if outcome == "account_unusable":
                from core.openai_auth import AccountUnusableError
                from core.roxy_registration import _inspect_auth_page_failure

                info = _inspect_auth_page_failure(driver)
                code = info.get("error_code") or "account_deactivated"
                raise AccountUnusableError(
                    f"OTP 提交后账号已废 error_code={code} url={info.get('url') or getattr(driver, 'current_url', '')}",
                    error_code=code,
                )
            # 限流：抛给任务层退避 + 换出口重试（不降并发）
            if outcome == "rate_limit":
                from core.openai_auth import RateLimitError
                from core.roxy_registration import _inspect_auth_page_failure

                info = _inspect_auth_page_failure(driver)
                code = info.get("error_code") or "rate_limit_exceeded"
                raise RateLimitError(
                    f"OTP 提交后 Auth 限流 error_code={code} url={info.get('url') or getattr(driver, 'current_url', '')}",
                    error_code=code,
                )

            # OpenAI Oops/Route Error：点 Try again；失败则重开登录页，不要死等 resend
            if outcome == "route_error" or _is_auth_route_error_page(driver):
                logger.warning(
                    "[Cloak注册][OTP] 提交后路由错误（%s/%s），尝试恢复 url=%s",
                    otp_attempt, max_otp_attempts, getattr(driver, "current_url", ""),
                )
                rec = _recover_auth_route_error(driver)
                if rec.get("ok") and _otp_flow_already_passed(driver):
                    logger.info("[Cloak注册][OTP] Try again 后已离开错误页/验证码页，按通过继续")
                    break
                if otp_attempt >= max_otp_attempts:
                    raise RuntimeError(
                        f"auth_route_error: OpenAI OTP 路由错误连续失败 state_url={getattr(driver, 'current_url', '')}"
                    )
                try:
                    _restart_email_login_flow(driver, email)
                except Exception as restart_exc:
                    logger.warning("[Cloak注册][OTP] 重开登录失败，继续下一轮：%s", str(restart_exc)[:160])
                otp_after_ts = time.time()
                current_otp = None
                human_delay("api")
                continue

            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            logger.warning(
                "[Cloak注册][OTP] 验证码可能无效/超时，准备重新发送（%s/%s）url=%s",
                otp_attempt + 1,
                max_otp_attempts,
                getattr(driver, "current_url", ""),
            )
            otp_after_ts = time.time()
            try:
                resend = _click_resend_email_otp(driver, timeout=25)
            except Exception as resend_exc:
                from core.openai_auth import AccountUnusableError, RateLimitError

                if isinstance(resend_exc, (AccountUnusableError, RateLimitError)):
                    raise
                if "auth_route_error" in str(resend_exc) or _is_auth_route_error_page(driver):
                    logger.warning("[Cloak注册][OTP] 重发遇路由错误，重开邮箱登录：%s", str(resend_exc)[:160])
                    _restart_email_login_flow(driver, email)
                    otp_after_ts = time.time()
                    current_otp = None
                    human_delay("api")
                    continue
                raise
            if resend.get("skipped"):
                logger.info("[Cloak注册][OTP] 重发前发现已离开验证码页，按 OTP 通过继续")
                break
            human_delay("api")
            current_otp = None

        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            human_delay("post_auth")

        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        logger.info("[Cloak注册] 已拿到 accessToken：%s", email)

        # 2FA：设置里 ENABLE_2FA=True 时，用浏览器 cookie 走协议 enroll（再收一封邮箱 OTP）
        totp_secret = None
        try:
            from config import twofa as _twofa_live
            enable_2fa = bool(getattr(_twofa_live, "ENABLE_2FA", False))
        except Exception:
            enable_2fa = bool(getattr(_twofa_cfg, "ENABLE_2FA", False))
        if enable_2fa:
            try:
                from core.account_export import setup_2fa_from_browser_driver
                logger.info("[Cloak注册] ENABLE_2FA=True，开始设置 TOTP 2FA")
                totp_secret = setup_2fa_from_browser_driver(
                    driver,
                    email,
                    proxy=proxy,
                )
                logger.info(
                    "[Cloak注册] 2FA 设置完成 secret=%s...%s",
                    (totp_secret or "")[:4],
                    (totp_secret or "")[-4:],
                )
            except Exception as exc:
                logger.error("[Cloak注册] 2FA 设置失败：%s: %s", type(exc).__name__, exc)
                logger.debug("[Cloak注册] 2FA 失败详情", exc_info=True)
                logger.warning("[Cloak注册] 将继续保存账号（不含 TOTP secret），可后续手动设置")
                totp_secret = None
        else:
            logger.info("[Cloak注册] 已跳过 2FA 设置 (ENABLE_2FA=False)")

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        if skip_codex:
            codex_result = {
                "status": "skipped",
                "ok": True,
                "message": "skip_codex=True，跳过 Codex（GCash 提链等保活会话）",
            }
            logger.info("[Cloak注册][Codex] skip_codex=True，强制跳过 Codex OAuth")
        else:
            try:
                from config import codex as _codex_cfg
                if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                    from core.roxy_codex_oauth import run_roxy_codex_oauth
                    logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=True，复用当前 CloakBrowser 窗口执行 Codex 授权")
                    _check_manual_stop()
                    codex_result = run_roxy_codex_oauth(
                        email,
                        reuse_existing_profile=True,
                        existing_driver=driver,
                        existing_opened=opened,
                        force=True,
                        clear_existing_state=True,
                    )
                else:
                    logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
            except Exception as exc:
                codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=((opened.raw or {}).get("proxy") if opened else None) or proxy or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "cloakbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "codex": codex_result,
                "keep_browser": bool(keep_browser),
                "skip_codex": bool(skip_codex),
            },
        )
        # keep_browser 场景：账号已入库即算注册成功；Codex 本就 skip，不因 codex 失败关浏览器
        if keep_browser:
            hold_browser = True
            logger.info("[Cloak注册] keep_browser=True，保留浏览器登录态：%s", email)
            return {
                "success": True,
                "email": email,
                "account_id": account_id,
                "access_token": access_token,
                "totp_secret": totp_secret,
                "codex": codex_result,
                "error": None,
                "driver": driver,
                "opened": opened,
                "keep_browser": True,
            }
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {
            "success": bool(codex_ok),
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "codex": codex_result,
            "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}",
        }
    except Exception as exc:
        logger.error("[Cloak注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Cloak注册] 失败详情", exc_info=True)
        err_text = f"{type(exc).__name__}: {str(exc)[:300]}"
        try:
            from core.email_provider import release_email
            from core.openai_auth import (
                AccountUnusableError,
                RateLimitError,
                should_disable_registration_email_for_error,
            )

            # 废号：立即 disabled，避免被其它 worker 再领
            if isinstance(exc, AccountUnusableError) or should_disable_registration_email_for_error(exc):
                release_email(email, status="disabled", note=f"自动停用: {str(exc)[:180]}")
            elif create_acknowledged:
                release_email(email, status="failed", note=f"Cloak注册失败: {str(exc)[:180]}")
            elif isinstance(exc, RateLimitError):
                # 限流可重试：保持 used，交给 registration_service 退避后换出口重试
                logger.info("[Cloak注册] 限流失败，保持邮箱 used 供任务重试：%s", email)
            else:
                # 其它可重试失败也保持 used，避免中途被其它任务抢走
                logger.info("[Cloak注册] 可重试失败，保持邮箱 used：%s (%s)", email, type(exc).__name__)
        except Exception:
            pass
        return {"success": False, "email": email, "error": err_text}
    finally:
        # hold_browser：调用方接管 driver；全局 CLOAK_KEEP_BROWSER_OPEN 仍保留旧行为
        if driver is not None and not hold_browser and not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
