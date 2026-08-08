# -*- coding: utf-8 -*-
"""已注册账号查活：重新登录，成功拿到最新 ChatGPT accessToken 即视为正常。

登录路径：
  1) 账号有 password（尤其 password_totp 导入）→ 密码登录，必要时 TOTP/2FA
  2) 否则 → 邮箱 OTP（outlook / 取码地址等可收信来源）
"""
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from core.session import BrowserSession
from core.chatgpt_auth import get_providers, get_csrf_token, signin_openai
from core.openai_auth import (
    follow_authorize,
    send_email_otp,
    validate_email_otp,
    submit_login_email,
    verify_login_password,
    verify_login_totp,
    extract_auth_continue,
    needs_email_otp_step,
    needs_totp_step,
    EmailOtpInvalidError,
    AccountUnusableError,
    detect_account_unusable_text,
)
from core.account_export import follow_oauth_callback, fetch_session
from core.email_provider import wait_for_otp
from core.totp_login import generate_totp_code, load_totp_secret, normalize_totp_secret

logger = logging.getLogger(__name__)
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()

# 查活网络预检失败（403/429/代理/超时等）多为出口 IP 被 CF 标记或代理池抖动，
# 视为可换新 IP 重试；账号本身问题（废号/邮箱错误等）不重试。
_RETRYABLE_NETWORK_HINTS = (
    "403", "429", "502", "503", "504",
    "proxy", "socks", "timeout", "timed out",
    "connection", "closed", "reset",
)


def _is_retryable_network_error(exc: BaseException) -> bool:
    if isinstance(exc, AccountUnusableError):
        return False
    text = str(exc or "").lower()
    return any(h in text for h in _RETRYABLE_NETWORK_HINTS)


def _pick_live_check_proxy(exclude: set[str]) -> str | None:
    """为查活选一个未失败/未冷却的代理；池空返回 None（交给 BrowserSession 默认逻辑）。"""
    try:
        from config.proxy import pick_proxy
        chosen = str(pick_proxy(exclude=exclude) or "").strip()
        return chosen if chosen else None
    except Exception:
        return None


def _network_preflight_with_retry(email: str, proxy: str | None, max_attempts: int = 6) -> tuple[BrowserSession, str]:
    """Providers → CSRF → Signin 网络预检；失败换新出口重试（每轮新会话）。

    proxy 语义：
      - None  → 每轮从 PROXY_POOL 重新 pick（可换 sticky IP）
      - ""    → 强制直连（禁止再回落到代理池）
      - "..." → 第 1 轮用指定代理；若 403/网络失败，后续轮次从池里换别的 sticky
                （sticky 固定出口，复用同一 URL 重试没有意义）
    """
    session: BrowserSession | None = None
    last_exc: BaseException | None = None
    failed_proxies: set[str] = set()
    force_direct = proxy == ""
    preferred = None if proxy is None else str(proxy).strip()
    # preferred 为空串已由 force_direct 处理；非空则首轮优先

    for attempt in range(1, max_attempts + 1):
        if session is not None:
            try:
                session.session.close()
            except Exception:
                pass

        if force_direct:
            use_proxy: str | None = ""
        elif attempt == 1 and preferred and preferred not in failed_proxies:
            use_proxy = preferred
        else:
            # 必须重新抽池：sticky 同一 URL = 同一 IP，复用等于空转
            picked = _pick_live_check_proxy(failed_proxies)
            if picked:
                use_proxy = picked
            elif preferred and preferred not in failed_proxies:
                use_proxy = preferred
            else:
                # 池内未失败/未冷却耗尽：放宽冷却再抽；仍没有则报错，绝不偷偷直连
                picked2 = _pick_live_check_proxy(set())
                if not picked2:
                    try:
                        from config.proxy import PROXY_POOL, pick_proxy
                        # 忽略冷却强制再抽（本轮 failed 仍排除）
                        pool = [p for p in (PROXY_POOL or []) if p and p not in failed_proxies]
                        picked2 = pool[0] if pool else str(pick_proxy() or "")
                    except Exception:
                        picked2 = None
                if not picked2:
                    raise RuntimeError(
                        f"PROXY_POOL 无可用代理可换（已失败 {len(failed_proxies)} 条），"
                        f"拒绝回退直连。last={last_exc}"
                    )
                use_proxy = picked2

        # 注意：不能写 `proxy if proxy else None`，否则 "" 直连会变 None 又抽池
        # force_direct 才允许 ""；其它路径禁止静默直连
        if not force_direct and use_proxy == "":
            raise RuntimeError("查活拒绝使用直连：未配置可用代理")
        session = BrowserSession(proxy=use_proxy)
        exit_ip = ""
        try:
            geo = getattr(session, "exit_geo", None) or {}
            exit_ip = str(geo.get("ip") or geo.get("query") or "")
        except Exception:
            exit_ip = ""
        used = session.proxy or ""
        logger.info(
            "[查活] 会话创建完成：proxy=%s exit_ip=%s device_id=%s（网络预检第 %s/%s 次，failed_proxies=%s）",
            used if used else ("直连" if force_direct or use_proxy == "" else "配置随机/直连"),
            exit_ip or "-",
            session.device_id,
            attempt,
            max_attempts,
            len(failed_proxies),
        )
        try:
            get_providers(session)
            csrf = get_csrf_token(session)
            authorize_url = signin_openai(session, csrf, email)
            return session, authorize_url
        except Exception as exc:
            last_exc = exc
            if used:
                failed_proxies.add(used)
                try:
                    from config.proxy import mark_proxy_cooldown
                    mark_proxy_cooldown(used, reason="live_check_403" if "403" in str(exc) else "live_check_net")
                except Exception:
                    pass
            if attempt >= max_attempts or not _is_retryable_network_error(exc):
                raise
            logger.warning(
                "[查活] 网络预检失败（%s/%s），换新 sticky/出口重试 used=%s exit_ip=%s：%s",
                attempt,
                max_attempts,
                (used.split("@")[-1] if used else "直连"),
                exit_ip or "-",
                str(exc)[:200],
            )
            # 403/429 时略加长间隔，降低同批并发打穿同一代理段
            time.sleep(1.2 + min(2.5, 0.4 * attempt))
    raise RuntimeError(f"网络预检多次失败：{last_exc}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"live-check-{safe}.log"


def is_checking(email: str) -> bool:
    key = str(email or "").strip().lower()
    with _RUNNING_LOCK:
        return key in _RUNNING


def _validate_with_retry(session: BrowserSession, email: str, otp_after_ts: float, max_otp_attempts: int = 3) -> dict:
    current_otp = None
    last_exc: Exception | None = None
    for attempt in range(1, max_otp_attempts + 1):
        try:
            if current_otp is None:
                logger.info("[查活] 等待登录 OTP：%s（第 %s/%s 次）", email, attempt, max_otp_attempts)
                current_otp = wait_for_otp(email, after_ts=otp_after_ts)
            result = validate_email_otp(session, current_otp, sentinel_header=None, so_header=None)
            return result
        except EmailOtpInvalidError as exc:
            last_exc = exc
            if attempt >= max_otp_attempts:
                break
            logger.warning("[查活] OTP 无效/过期，重新发送后再取：%s", str(exc)[:180])
            send_email_otp(session)
            # 以“重新发送请求完成后”为新基准，避免刚刚失败的上一封旧码再次被 after 容忍窗口命中。
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
        except Exception as exc:
            # 提交 OTP 后的网络抖动（连接断开/超时/代理波动）：同一会话重发验证码再验证一次。
            if attempt >= max_otp_attempts or not _is_retryable_network_error(exc):
                raise
            last_exc = exc
            logger.warning("[查活] OTP 验证网络抖动，重新发送后再取（%s/%s）：%s", attempt, max_otp_attempts, str(exc)[:180])
            try:
                send_email_otp(session)
            except Exception:
                raise
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
    raise last_exc if last_exc else RuntimeError("OTP 验证失败")


def _account_has_mail_inbox(email: str) -> bool:
    """邮箱是否在可收信池中（不能用 resolve_email_source 默认回落）。"""
    email = str(email or "").strip()
    if not email:
        return False
    try:
        from core import db
        if db.get_generic_api_email_by_email(email) or db.get_outlook_by_email(email):
            return True
    except Exception:
        pass
    try:
        from core.gptmail_client import get_account_context as get_gptmail_context
        if get_gptmail_context(email):
            return True
    except Exception:
        pass
    try:
        from core.cf_temp_mail_client import get_account_context as get_cf_context
        if get_cf_context(email):
            return True
    except Exception:
        pass
    try:
        from core.mailnest_client import get_account_context as get_mailnest_context
        if get_mailnest_context(email):
            return True
    except Exception:
        pass
    try:
        from core.cloudmail_client import get_account_context as get_cloudmail_context
        if get_cloudmail_context(email):
            return True
    except Exception:
        pass
    return False


def _load_account_login_material(email: str) -> dict:
    """从账号库读取查活可用的登录素材。"""
    try:
        from core import db
        acc = db.get_account_by_email(email) or {}
    except Exception:
        acc = {}
    password = str(acc.get("password") or "").strip()
    totp_secret = load_totp_secret(email, acc.get("totp_secret"))
    email_source = str(acc.get("email_source") or "").strip().lower()
    # original_email_line 也可能含 password----totp（导入格式）
    if (not password or not totp_secret) and acc.get("original_email_line"):
        try:
            from core.account_import import parse_import_account_line
            rec = parse_import_account_line(str(acc.get("original_email_line") or ""))
            if isinstance(rec, dict):
                password = password or str(rec.get("password") or "").strip()
                if not totp_secret:
                    totp_secret = normalize_totp_secret(rec.get("totp_secret") or "") or None
                email_source = email_source or str(rec.get("source") or rec.get("kind") or "").strip().lower()
        except Exception:
            pass
    has_mail = _account_has_mail_inbox(email)
    # 注意：outlook/generic 导入的 password 是邮箱密码，不是 OpenAI 登录密码，不能拿去 password/verify。
    # 仅 password_totp（账密+2FA）或「无收信能力但账号上明确存了 OpenAI 密码+2FA」才走密码查活。
    is_openai_password_login = email_source == "password_totp" or (
        bool(password) and bool(totp_secret) and email_source not in ("outlook", "generic_api") and not has_mail
    )
    prefer_password = bool(password) and is_openai_password_login
    return {
        "password": password,
        "totp_secret": totp_secret,
        "email_source": email_source,
        "has_mail": has_mail,
        "prefer_password": prefer_password,
    }


def _complete_oauth_from_continue(session: BrowserSession, continue_url: str, page_type: str = "", *, referer: str) -> dict:
    if not continue_url:
        raise RuntimeError(f"登录成功但没有 OAuth continue_url: page_type={page_type or '-'}")
    if "about-you" in str(continue_url) or page_type in {"about_you", "about-you"}:
        raise RuntimeError(
            f"该邮箱登录后进入资料页，疑似不是完整已注册账号: page_type={page_type}, continue_url={continue_url}"
        )
    follow_oauth_callback(session, str(continue_url), referer=referer)
    session_info = fetch_session(session)
    access_token = str(session_info.get("accessToken") or "")
    if not access_token:
        raise RuntimeError("重新登录后未拿到 accessToken")
    return session_info


def _login_via_password_totp(
    session: BrowserSession,
    email: str,
    *,
    password: str,
    totp_secret: str | None,
    final_url: str,
    has_mail: bool,
) -> dict:
    """密码 + 可选 TOTP 登录；若密码后落到邮箱 OTP 且可收信则回退 OTP。"""
    url_l = str(final_url or "").lower()
    continue_url = ""
    page_type = ""

    on_password_page = "password" in url_l
    on_totp_page = needs_totp_step("", url_l)
    on_email_otp_page = needs_email_otp_step("", url_l)

    # 仍停在邮箱入口/通用 log-in：先 authorize/continue 提交邮箱
    if not (on_password_page or on_totp_page or on_email_otp_page):
        email_data = submit_login_email(session, email)
        continue_url, page_type = extract_auth_continue(email_data)
        hint = f"{page_type} {continue_url}".lower()
        on_password_page = "password" in hint or page_type in {"login_password", "password"}
        on_totp_page = needs_totp_step(page_type, continue_url)
        on_email_otp_page = needs_email_otp_step(page_type, continue_url)
        # 没给明确 page 时，默认下一步走密码（已注册+有密码）
        if not (on_password_page or on_totp_page or on_email_otp_page):
            on_password_page = True

    # 密码校验
    if on_password_page and not on_totp_page and not on_email_otp_page:
        logger.info("[查活] 使用密码登录路径 email=%s", email)
        pwd_data = verify_login_password(session, password)
        continue_url, page_type = extract_auth_continue(pwd_data)
        if not continue_url:
            if needs_totp_step(page_type, ""):
                continue_url = "https://auth.openai.com/multi-factor/totp"
            elif needs_email_otp_step(page_type, ""):
                continue_url = "https://auth.openai.com/email-verification"
        on_totp_page = needs_totp_step(page_type, continue_url)
        on_email_otp_page = needs_email_otp_step(page_type, continue_url)

    # TOTP / 2FA（与补跑一致：用本地 totp_secret）
    if on_totp_page:
        secret = normalize_totp_secret(totp_secret) if totp_secret else ""
        if not secret:
            raise RuntimeError(
                f"登录需要 TOTP/2FA，但账号 {email} 无 totp_secret（password_totp 导入应带 2FA 密钥）"
            )
        logger.info("[查活] 检测到 2FA/TOTP，使用本地 totp_secret 过验证")
        last_exc: Exception | None = None
        used_codes: set[str] = set()
        for attempt in range(1, 4):
            code = generate_totp_code(secret, wait_near_boundary=True)
            if code in used_codes:
                time.sleep(1.2)
                code = generate_totp_code(secret, wait_near_boundary=True)
            used_codes.add(code)
            try:
                cu = str(continue_url or "")
                referer = cu if (
                    "totp" in cu.lower()
                    or "factor" in cu.lower()
                    or "mfa" in cu.lower()
                ) else "https://auth.openai.com/mfa-challenge"
                totp_data = verify_login_totp(
                    session,
                    code,
                    referer=referer,
                    challenge_url=cu if "mfa-challenge" in cu.lower() else referer,
                )
                continue_url, page_type = extract_auth_continue(totp_data)
                last_exc = None
                break
            except EmailOtpInvalidError as exc:
                last_exc = exc
                logger.warning("[查活] TOTP 无效（%s/3）：%s", attempt, str(exc)[:160])
                time.sleep(1.2)
            except RuntimeError as exc:
                # schema/参数错误不应傻重试 3 次不同码
                if "参数均不匹配" in str(exc) or "missing" in str(exc).lower():
                    raise
                last_exc = exc
                logger.warning("[查活] TOTP 失败（%s/3）：%s", attempt, str(exc)[:160])
                time.sleep(1.0)
        if last_exc is not None:
            raise last_exc
        on_email_otp_page = needs_email_otp_step(page_type, continue_url)

    # 密码后若落到邮箱 OTP（少数策略）
    if on_email_otp_page or needs_email_otp_step(page_type, continue_url):
        if not has_mail:
            raise RuntimeError(
                f"密码校验后需要邮箱 OTP，但账号 {email} 无收信能力"
                f"（password_totp 应走 TOTP；若实际策略是邮箱 OTP，请改用可收信邮箱导入）"
            )
        logger.info("[查活] 密码后进入邮箱 OTP，改走收信验证")
        otp_after_ts = time.time()
        try:
            send_email_otp(session)
        except Exception:
            pass
        validate_result = _validate_with_retry(session, email, otp_after_ts)
        continue_url, page_type = extract_auth_continue(validate_result)

    if not continue_url:
        raise RuntimeError(f"密码/2FA 登录后无 continue_url page_type={page_type or '-'}")

    # continue 若只是中间页 URL（password/totp/email-verification），说明还没拿到 OAuth 跳转
    low_cu = str(continue_url).lower()
    if any(k in low_cu for k in ("/log-in", "/password", "email-verification", "factor-totp", "multi-factor")) \
            and "authorize/continue" not in low_cu and "callback" not in low_cu:
        # 中间态但无下一步信号：再探一次 TOTP（有 secret 时）
        if totp_secret and not needs_email_otp_step(page_type, continue_url):
            secret = normalize_totp_secret(totp_secret)
            if secret and not needs_totp_step(page_type, continue_url):
                logger.info("[查活] continue 仍为中间页，尝试补交 TOTP")
                code = generate_totp_code(secret, wait_near_boundary=True)
                totp_data = verify_login_totp(
                    session,
                    code,
                    challenge_url=str(continue_url or ""),
                    referer=str(continue_url or "") or "https://auth.openai.com/mfa-challenge",
                )
                continue_url, page_type = extract_auth_continue(totp_data)
        if any(k in str(continue_url).lower() for k in (
            "/log-in", "/password", "email-verification", "factor-totp", "multi-factor", "mfa-challenge",
        )) and "authorize/continue" not in str(continue_url).lower() and "callback" not in str(continue_url).lower():
            raise RuntimeError(
                f"密码/2FA 后仍停在中间页，未拿到 OAuth continue_url: page={page_type or '-'} url={continue_url}"
            )

    referer = "https://auth.openai.com/log-in/password"
    if needs_totp_step(page_type, continue_url):
        referer = "https://auth.openai.com/multi-factor/totp"
    elif needs_email_otp_step(page_type, continue_url):
        referer = "https://auth.openai.com/email-verification"
    return _complete_oauth_from_continue(session, continue_url, page_type, referer=referer)


def _login_via_email_otp(session: BrowserSession, email: str, final_url: str) -> dict:
    otp_after_ts = time.time()
    # 若落在密码页且没有密码素材，明确报错（避免干等 OTP）
    if "password" in str(final_url or "").lower() and "email-verification" not in str(final_url or "").lower():
        raise RuntimeError(
            f"登录落到密码页但账号无 password/totp 素材，无法邮箱 OTP 查活: {final_url}"
        )
    validate_result = _validate_with_retry(session, email, otp_after_ts)
    continue_url, page_type = extract_auth_continue(validate_result)
    # 邮箱 OTP 后偶发再要 TOTP
    if needs_totp_step(page_type, continue_url):
        secret = load_totp_secret(email, None)
        if not secret:
            raise RuntimeError(f"邮箱 OTP 后还需 TOTP，但账号 {email} 无 totp_secret")
        logger.info("[查活] 邮箱 OTP 后进入 TOTP")
        code = generate_totp_code(secret, wait_near_boundary=True)
        totp_data = verify_login_totp(session, code)
        continue_url, page_type = extract_auth_continue(totp_data)
    return _complete_oauth_from_continue(
        session,
        continue_url,
        page_type,
        referer="https://auth.openai.com/email-verification",
    )


def check_account_liveness(email: str, proxy: str | None = None, *, clear_log: bool = True) -> dict:
    """
    重新登录账号并刷新最新 accessToken。

    返回：
      {
        ok: bool,
        status: live/deactivated/failed,
        access_token: str?,
        session: dict?,
        checked_at: ISO,
        error: str?
      }
    """
    email = str(email or "").strip()
    if not email:
        raise ValueError("email 不能为空")

    checked_at = _now()
    key = email.lower()
    path = log_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    if clear_log:
        path.write_text("", encoding="utf-8")

    fh: logging.FileHandler | None = None
    root_logger = logging.getLogger()
    thread_name = threading.current_thread().name
    with _RUNNING_LOCK:
        _RUNNING.add(key)
    try:
        fh = logging.FileHandler(str(path), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)

        material = _load_account_login_material(email)
        login_mode = "password_totp" if material.get("prefer_password") else "email_otp"
        logger.info("[查活] 日志文件：%s", path)
        logger.info("[查活] 开始重新登录：%s mode=%s source=%s has_mail=%s has_password=%s has_totp=%s",
                    email, login_mode, material.get("email_source") or "-",
                    material.get("has_mail"), bool(material.get("password")), bool(material.get("totp_secret")))
        if login_mode == "password_totp":
            logger.info("[查活] 流程：Providers → CSRF → Signin → Authorize → 密码 → TOTP(如需) → OAuth callback → Session/AT")
        else:
            logger.info("[查活] 流程：Providers → CSRF → Signin → Authorize → 邮箱 OTP → OAuth callback → Session/AT")

        session, authorize_url = _network_preflight_with_retry(email, proxy)
        final_url = follow_authorize(session, authorize_url)
        dead_code = detect_account_unusable_text(final_url)
        if dead_code:
            return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": dead_code}
        logger.info("[查活] authorize 落点：%s", str(final_url or "-")[:200])

        if material.get("prefer_password") and material.get("password"):
            session_info = _login_via_password_totp(
                session,
                email,
                password=str(material.get("password") or ""),
                totp_secret=material.get("totp_secret"),
                final_url=final_url,
                has_mail=bool(material.get("has_mail")),
            )
        else:
            session_info = _login_via_email_otp(session, email, final_url)

        access_token = str(session_info.get("accessToken") or "")
        user = session_info.get("user") or {}
        account = session_info.get("account") or {}
        logger.info("[查活] 正常：%s user_id=%s plan=%s", email, user.get("id"), account.get("planType"))
        return {
            "ok": True,
            "status": "live",
            "checked_at": checked_at,
            "access_token": access_token,
            "session": session_info,
            "device_id": session.device_id,
            "proxy_used": session.proxy or None,
            "login_mode": login_mode,
        }
    except AccountUnusableError as exc:
        code = getattr(exc, "error_code", "") or detect_account_unusable_text(str(exc)) or "account_deactivated"
        logger.warning("[查活] 已废号：%s %s", email, code)
        return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
    except Exception as exc:
        code = detect_account_unusable_text(str(exc))
        if code:
            logger.warning("[查活] 已废号：%s %s", email, code)
            return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
        logger.warning("[查活] 失败：%s %s: %s", email, type(exc).__name__, str(exc)[:260])
        return {"ok": False, "status": "failed", "checked_at": checked_at, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    finally:
        try:
            logger.info("[查活] 结束：%s", email)
            if fh is not None:
                root_logger.removeHandler(fh)
                fh.close()
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(key)
