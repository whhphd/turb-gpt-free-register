# -*- coding: utf-8 -*-
"""mail.com / kittymail 等品牌邮箱：本地池 + 网页协议收 ChatGPT OTP。"""
from __future__ import annotations

import email as email_lib
import imaplib
import logging
import random
import re
import ssl
import threading
import time
from dataclasses import dataclass, field
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from config import email as _email_cfg
from core.mailcom_protocol import MailComClient, MailComError
from core.otp_utils import extract_otp, looks_like_openai_email

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONTEXT_CACHE: dict[str, "MailComAccount"] = {}
_CLIENT_CACHE: dict[str, MailComClient] = {}
# 同一主邮箱+别名共用一条接码代理（不写回邮箱池）。key=login_email
_GROUP_PROXY: dict[str, str] = {}
_GROUP_PROXY_LOCK = threading.Lock()
_IMAP_HOST = "imap.mail.com"
_IMAP_PORT = 993
_INBOX_LOCKS: dict[str, threading.Lock] = {}
_INBOX_LOCKS_GUARD = threading.Lock()

_VERIFY_SUBJECT_HINTS = (
    "verification code",
    "temporary chatgpt",
    "chatgpt login code",
    "your code",
    "验证码",
    "登录代码",
    "認証コード",
)
_SKIP_SUBJECT_HINTS = (
    "new sign-in",
    "new sign in",
    "sign-in to your",
    "signed in",
    "security alert",
    "welcome on board",
)


class MailComMailError(RuntimeError):
    """mail.com 邮箱池 / 取码错误。"""


@dataclass
class MailComAccount:
    email: str
    password: str
    proxy_url: str = ""
    session: dict[str, Any] = field(default_factory=dict)
    login_email: str = ""


def _accounts_file() -> Path:
    name = str(getattr(_email_cfg, "MAILCOM_ACCOUNTS_FILE", "") or "用于注册的mail邮箱.txt").strip()
    p = Path(name)
    return p if p.is_absolute() else _PROJECT_ROOT / p


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _login_address(account: MailComAccount) -> str:
    return _cache_key(account.login_email or account.email)


def normalize_mailcom_proxy(value: str | None) -> str:
    """接受 http(s)/socks5(h) URL，或 host:port:user:pass。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        parts = text.split(":", 3)
        if len(parts) != 4 or not all(parts):
            raise MailComMailError("mail.com 代理格式无效，应为 http://user:pass@host:port 或 host:port:user:pass")
        host, port, username, password = parts
        return f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise MailComMailError("mail.com 代理只支持 http / https / socks5 / socks5h")
    return text


def looks_like_mailcom_proxy(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    low = text.lower()
    if low.startswith(("http://", "https://", "socks5://", "socks5h://")):
        return True
    parts = text.split(":")
    return len(parts) == 4 and all(parts) and parts[1].isdigit()


def is_mailcom_address(email: str) -> bool:
    addr = str(email or "").strip().lower()
    if "@" not in addr:
        return False
    domain = addr.rsplit("@", 1)[-1]
    extra = []
    try:
        extra = [str(x).strip().lower() for x in (getattr(_email_cfg, "MAILCOM_EXTRA_DOMAINS", []) or []) if str(x).strip()]
    except Exception:
        extra = []
    configured = [str(x).strip().lower() for x in (getattr(_email_cfg, "MAILCOM_DOMAINS", []) or []) if str(x).strip()]
    allow = {d for d in (*configured, *extra) if d}
    return domain in allow or domain.endswith(".mail.com")


def _is_chatgpt_otp_mail(subject: str, sender: str, body: str) -> bool:
    item = {"subject": subject or "", "from": sender or "", "text": body or "", "content": body or ""}
    if not looks_like_openai_email(item):
        return False
    subj = (subject or "").lower()
    if any(h in subj for h in _SKIP_SUBJECT_HINTS) and not any(h in subj for h in _VERIFY_SUBJECT_HINTS):
        return False
    return True


def _otp_score(subject: str) -> int:
    subj = (subject or "").lower()
    if any(h in subj for h in _VERIFY_SUBJECT_HINTS):
        return 2
    return 1


def _parse_accounts_file(path: Path) -> list[MailComAccount]:
    if not path.exists():
        return []
    out: list[MailComAccount] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in (line.split("----") if "----" in line else line.split("===="))]
        if len(parts) < 2 or "@" not in parts[0] or not parts[1]:
            continue
        proxy = ""
        if len(parts) >= 3:
            try:
                proxy = normalize_mailcom_proxy(parts[2])
            except MailComMailError:
                continue
        out.append(MailComAccount(email=parts[0], password=parts[1], proxy_url=proxy))
    return out


def import_from_file(path: str | Path | None = None) -> tuple[int, int]:
    from core.db import import_mailcom_emails_ex

    p = Path(path) if path else _accounts_file()
    records = [
        {"email": a.email, "password": a.password, "proxy_url": a.proxy_url}
        for a in _parse_accounts_file(p)
    ]
    inserted, skipped, primaries = import_mailcom_emails_ex(records)
    auto_expand_imported_primaries(primaries)
    return inserted, skipped


def import_from_text(text: str) -> tuple[int, int]:
    from core.db import import_mailcom_emails_ex
    from core.account_import import parse_import_text

    records, _errors = parse_import_text(text, preferred_source="mailcom")
    inserted, skipped, primaries = import_mailcom_emails_ex(records)
    auto_expand_imported_primaries(primaries)
    return inserted, skipped


def auto_expand_imported_primaries(emails: list[str] | None) -> dict:
    """导入主邮箱后按 MAILCOM_AUTO_ALIAS_COUNT 自动建别名。"""
    want = int(getattr(_email_cfg, "MAILCOM_AUTO_ALIAS_COUNT", 0) or 0)
    emails = [str(x or "").strip() for x in (emails or []) if str(x or "").strip()]
    if want <= 0 or not emails:
        return {"created": 0, "imported_existing": 0, "failed": []}
    created = 0
    imported_existing = 0
    failed: list[dict] = []
    for idx, email in enumerate(emails, start=1):
        logger.info("[MailCom] 导入后自动别名 %s/%s：%s", idx, len(emails), email)
        try:
            out = expand_aliases(email, count=want)
            created += len(out.get("created") or [])
            imported_existing += len(out.get("imported_existing") or [])
            if out.get("errors"):
                failed.append({"email": email, "error": "; ".join(out["errors"][:3])})
        except Exception as exc:
            logger.warning("[MailCom] 导入后自动创建别名失败 %s: %s: %s", email, type(exc).__name__, exc)
            failed.append({"email": email, "error": f"{type(exc).__name__}: {exc}"})
    if created or imported_existing or failed:
        logger.info(
            "[MailCom] 导入后自动别名：主邮箱=%s 新建=%s 同步已有=%s 失败=%s",
            len(emails), created, imported_existing, len(failed),
        )
    return {"created": created, "imported_existing": imported_existing, "failed": failed}


def pick_account() -> MailComAccount:
    from core.db import claim_next_mailcom_email, mailcom_email_pool_summary

    inserted, skipped = import_from_file()
    if inserted:
        logger.info("[MailCom] 已自动从 %s 导入 %s 个邮箱（跳过 %s 个）", _accounts_file().name, inserted, skipped)

    row = claim_next_mailcom_email()
    if row is None:
        summary = mailcom_email_pool_summary()
        raise MailComMailError(
            f"mail.com 邮箱池没有可用账号: {summary}. 请导入 邮箱----密码，或写入 {_accounts_file().name}"
        )
    account = _account_from_row(row)
    if not account.password:
        raise MailComMailError(f"mail.com 邮箱缺少登录密码: {account.email}")
    _CONTEXT_CACHE[_cache_key(account.email)] = account
    logger.info(
        "[MailCom] 选中邮箱: %s（登录=%s DB id=%s）",
        account.email, _login_address(account), row.get("id"),
    )
    return account


def get_account_context(email: str) -> MailComAccount | None:
    key = _cache_key(email)
    if key in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[key]
    from core.db import get_mailcom_email_by_email

    row = get_mailcom_email_by_email(email)
    if row is None:
        return None
    account = _account_from_row(row)
    _CONTEXT_CACHE[key] = account
    return account


def _account_from_row(row: dict) -> MailComAccount:
    email = str(row.get("email") or "").strip()
    login_email = str(row.get("login_email") or "").strip() or email
    password = row.get("password") or ""
    proxy_url = row.get("proxy_url") or ""
    session = dict(row.get("session") or {})
    if login_email.lower() != email.lower():
        from core.db import get_mailcom_email_by_email
        parent = get_mailcom_email_by_email(login_email)
        if parent:
            password = password or parent.get("password") or ""
            session = dict(parent.get("session") or session or {})
    return MailComAccount(
        email=email,
        password=password,
        proxy_url=proxy_url,
        session=session,
        login_email=login_email,
    )


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    from core.db import release_mailcom_email

    release_mailcom_email(email, status=status, note=note)
    key = _cache_key(email)
    _CONTEXT_CACHE.pop(key, None)
    _CLIENT_CACHE.pop(key, None)


def _persist_session(email: str, client: MailComClient) -> None:
    from core.db import update_mailcom_session

    ctx = get_account_context(email)
    login = _login_address(ctx) if ctx else _cache_key(email)
    state = client.export_state()
    update_mailcom_session(login, state)
    if ctx:
        ctx.session = state


def _mask_proxy(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return "直连"
    if "@" in text:
        scheme = text.split("://", 1)[0] if "://" in text else ""
        host = text.rsplit("@", 1)[-1]
        return f"{scheme}://***@{host}" if scheme else f"***@{host}"
    return text.split("://")[0] + "://***" if "://" in text else "***"


def _proxy_for_requests(url: str) -> str:
    """requests/PySocks：socks5 改 socks5h，DNS 走代理端。"""
    text = str(url or "").strip()
    if text.lower().startswith("socks5://"):
        return "socks5h://" + text[len("socks5://"):]
    return text


def _inbox_lock(email: str) -> threading.Lock:
    """同一主邮箱的取信/建别名串行，避免并发打失效 sid。"""
    key = _cache_key(email)
    with _INBOX_LOCKS_GUARD:
        lock = _INBOX_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _INBOX_LOCKS[key] = lock
        return lock


def _clear_persisted_session(account: MailComAccount) -> None:
    login = _login_address(account)
    account.session = {}
    try:
        from core.db import update_mailcom_session
        update_mailcom_session(login, {})
    except Exception:
        pass


def forget_group_proxy(email: str | None = None) -> None:
    """丢掉内存里的组代理。email 为空则清空全部。"""
    with _GROUP_PROXY_LOCK:
        if email is None:
            _GROUP_PROXY.clear()
            return
        key = _cache_key(email)
        _GROUP_PROXY.pop(key, None)
        ctx = _CONTEXT_CACHE.get(key)
        if ctx:
            _GROUP_PROXY.pop(_login_address(ctx), None)


_RETRYABLE_WEB_PROXY_KINDS = frozenset({
    "login_redirect",
    "blocked",
    "network",
    "login_failed",
    "rate_limited",
    "session_rejected",
    "session_expired",
    "oauth_failed",
})
_MAILCOM_WEB_PROXY_ATTEMPTS = 10


def _proxy_exclude_variants(url: str) -> set[str]:
    """socks5 / socks5h 视为同一条，排除时两边都去掉。"""
    text = str(url or "").strip()
    if not text:
        return set()
    out = {text}
    low = text.lower()
    if low.startswith("socks5h://"):
        out.add("socks5://" + text[len("socks5h://"):])
    elif low.startswith("socks5://"):
        out.add("socks5h://" + text[len("socks5://"):])
    return out


def _invalidate_web_session(account: MailComAccount, failed_proxy: str = "") -> None:
    """丢掉失效 sid / 组代理，并把失败出口放进短冷却。"""
    login = _login_address(account)
    forget_group_proxy(login)
    forget_group_proxy(account.email)
    _CLIENT_CACHE.pop(_cache_key(login), None)
    _clear_persisted_session(account)
    if not failed_proxy:
        return
    try:
        from config.proxy import mark_proxy_cooldown
        for item in _proxy_exclude_variants(failed_proxy):
            mark_proxy_cooldown(item, reason="mailcom_login")
    except Exception:
        pass


def resolve_mailcom_proxy(
    account: MailComAccount,
    *,
    force_pool: bool = False,
    exclude: set[str] | list[str] | None = None,
) -> str:
    """取信/建别名从 PROXY_POOL 抽代理，不写回邮箱池。

    同一 login_email（主邮箱+其别名）复用同一条出口，避免一组收件箱连打多个 IP。
    force_pool=True 时（创建别名）必须走代理池。
    """
    if not force_pool and not bool(getattr(_email_cfg, "MAILCOM_USE_PROXY_POOL", False)):
        return ""
    excluded = {str(x).strip() for x in (exclude or []) if str(x).strip()}
    group = _login_address(account)
    with _GROUP_PROXY_LOCK:
        cached = str(_GROUP_PROXY.get(group) or "").strip()
        if cached and cached not in excluded:
            logger.info("[MailCom] %s 组内复用代理 login=%s %s", account.email, group, _mask_proxy(cached))
            return cached
        if cached and cached in excluded:
            _GROUP_PROXY.pop(group, None)

    from config.proxy import pick_proxy

    chosen = str(pick_proxy(exclude=excluded) or "").strip()
    if not chosen:
        raise MailComMailError("mail.com 需要代理池出口，但代理池为空")
    chosen = _proxy_for_requests(normalize_mailcom_proxy(chosen))
    if chosen in excluded:
        raise MailComMailError("mail.com 代理池里没有可用的新出口可换")
    with _GROUP_PROXY_LOCK:
        existing = str(_GROUP_PROXY.get(group) or "").strip()
        if existing and existing not in excluded:
            logger.info("[MailCom] %s 组内复用代理 login=%s %s", account.email, group, _mask_proxy(existing))
            return existing
        _GROUP_PROXY[group] = chosen
    logger.info("[MailCom] %s 组新抽代理 login=%s %s", account.email, group, _mask_proxy(chosen))
    return chosen


def _client_for(
    account: MailComAccount,
    *,
    force_pool: bool = False,
    exclude: set[str] | list[str] | None = None,
) -> MailComClient:
    login = _login_address(account)
    proxy = resolve_mailcom_proxy(account, force_pool=force_pool, exclude=exclude)
    cached = _CLIENT_CACHE.get(login)
    excluded = {str(x).strip() for x in (exclude or []) if str(x).strip()}
    if (
        cached
        and cached.password == account.password
        and (cached.proxy_url or "") == (proxy or "")
        and (proxy or "") not in excluded
    ):
        return cached
    client = MailComClient(
        login,
        account.password,
        state=account.session or {},
        timeout=30.0,
        proxy_url=proxy,
    )
    _CLIENT_CACHE[login] = client
    logger.info(
        "[MailCom] 接码客户端 address=%s login=%s proxy=%s",
        account.email, login, _mask_proxy(proxy),
    )
    return client


def _list_remote_aliases(account: MailComAccount) -> tuple[MailComClient, list[str]]:
    """登录并读取别名；login_redirect/封出口等可换代理错误会换 sticky 重试。"""
    failed: set[str] = set()
    last_exc: BaseException | None = None
    attempts = _MAILCOM_WEB_PROXY_ATTEMPTS
    login = _login_address(account)
    for attempt in range(1, attempts + 1):
        client = _client_for(account, force_pool=True, exclude=failed)
        try:
            with _inbox_lock(login):
                remote = [
                    str(x or "").strip().lower()
                    for x in client.list_aliases()
                    if str(x or "").strip()
                ]
            return client, remote
        except MailComError as exc:
            last_exc = exc
            used = str(getattr(client, "proxy_url", "") or "")
            failed |= _proxy_exclude_variants(used)
            retryable = exc.kind in _RETRYABLE_WEB_PROXY_KINDS
            if not retryable or attempt >= attempts:
                if exc.kind in {"oauth_failed", "session_expired"}:
                    _clear_persisted_session(account)
                raise MailComMailError(f"读取 mail.com 别名失败 ({exc.kind}): {exc}") from exc
            logger.warning(
                "[MailCom] 登录/读别名失败 kind=%s proxy=%s，换代理重试 %s/%s：%s",
                exc.kind,
                _mask_proxy(used),
                attempt + 1,
                attempts,
                exc,
            )
            _invalidate_web_session(account, used)
            time.sleep(min(2 * attempt, 6))
    raise MailComMailError(f"读取 mail.com 别名失败: {last_exc}")


def _message_ts(date_ms: int) -> float:
    if not date_ms:
        return 0.0
    return date_ms / 1000.0 if date_ms > 10_000_000_000 else float(date_ms)


def _decode_mime_header(value: str | None) -> str:
    text = str(value or "")
    if not text:
        return ""
    parts: list[str] = []
    try:
        for chunk, enc in decode_header(text):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(enc or "utf-8", "replace"))
            else:
                parts.append(str(chunk))
    except Exception:
        return text
    return " ".join(parts).strip()


def _email_plain_text(msg: email_lib.message.Message) -> str:
    if msg.is_multipart():
        chunks: list[str] = []
        for part in msg.walk():
            if part.get_content_type() != "text/plain":
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            chunks.append(payload.decode(charset, "replace"))
        return "\n".join(chunks)
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, "replace")


def _addresses_from_headers(msg: email_lib.message.Message, names: tuple[str, ...]) -> list[str]:
    """从邮件头解析收件地址，精确匹配用，不用子串。"""
    values: list[str] = []
    for name in names:
        raw_items = msg.get_all(name) or []
        if not raw_items:
            raw = msg.get(name)
            if raw:
                raw_items = [raw]
        for item in raw_items:
            values.append(_decode_mime_header(str(item or "")))
    out: list[str] = []
    seen: set[str] = set()
    for _, addr in getaddresses(values):
        text = str(addr or "").strip().lower()
        if text and "@" in text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _otp_mail_is_for_recipient(msg: email_lib.message.Message, recipient: str) -> bool:
    """只认写给当前注册地址的信。

    主邮箱和别名共用同一 IMAP 收件箱。Delivered-To 经常是主邮箱，
    不能用来匹配，否则主邮箱注册会吃到别名的验证码。
    """
    want = _cache_key(recipient)
    if not want or "@" not in want:
        return False
    addrs = _addresses_from_headers(
        msg,
        ("To", "Cc", "X-Original-To", "Envelope-To", "X-Forwarded-To"),
    )
    return want in addrs


def _imap_otp_candidates(
    account: MailComAccount,
    recipient: str,
    after_ts: float | None,
) -> list[tuple[int, float, str]]:
    """直连 IMAP 收信。网页登录经住宅代理会被踢到 support.mail.com。"""
    login = _login_address(account)
    want = _cache_key(recipient)
    ctx = ssl.create_default_context()
    mailbox = imaplib.IMAP4_SSL(_IMAP_HOST, _IMAP_PORT, ssl_context=ctx)
    try:
        mailbox.login(login, account.password)
        typ, _ = mailbox.select("INBOX")
        if typ != "OK":
            raise MailComMailError(f"mail.com IMAP 无法打开收件箱: {typ}")
        typ, data = mailbox.search(None, "ALL")
        if typ != "OK":
            raise MailComMailError("mail.com IMAP SEARCH 失败")
        ids = (data[0] or b"").split()[-40:]
        out: list[tuple[int, float, str]] = []
        for mid in reversed(ids):
            typ, msgdata = mailbox.fetch(mid, "(RFC822)")
            if typ != "OK" or not msgdata or not msgdata[0]:
                continue
            raw = msgdata[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            msg = email_lib.message_from_bytes(bytes(raw))
            subject = _decode_mime_header(msg.get("Subject"))
            sender = _decode_mime_header(msg.get("From"))
            body = _email_plain_text(msg)
            msg_ts = 0.0
            try:
                dt = parsedate_to_datetime(msg.get("Date"))
                if dt is not None:
                    msg_ts = dt.timestamp()
            except Exception:
                msg_ts = 0.0
            if after_ts and msg_ts and msg_ts + 2 < after_ts:
                continue
            if not _otp_mail_is_for_recipient(msg, want):
                if _is_chatgpt_otp_mail(subject, sender, body):
                    others = _addresses_from_headers(msg, ("To", "Cc", "X-Original-To"))
                    logger.debug(
                        "[MailCom] 跳过其它收件人的验证码邮件 want=%s to=%s login=%s",
                        want, ",".join(others[:4]) or "-", login,
                    )
                continue
            if not _is_chatgpt_otp_mail(subject, sender, body):
                continue
            code = extract_otp({
                "subject": subject,
                "from": sender,
                "text": body,
                "content": body,
            })
            if code:
                out.append((_otp_score(subject), msg_ts, code))
        return out
    except imaplib.IMAP4.error as exc:
        raise MailComMailError(f"mail.com IMAP 登录失败: {exc}") from exc
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """轮询 mail.com 收件箱，只返回 ChatGPT/OpenAI 的 6 位验证码。"""
    account = get_account_context(email)
    if account is None:
        raise MailComMailError(f"mail.com 邮箱不存在或未导入: {email}")
    if not account.password:
        raise MailComMailError(f"mail.com 邮箱缺少登录密码: {email}")

    deadline = time.time() + (max_wait or _email_cfg.OTP_MAX_WAIT)
    interval = poll_interval or _email_cfg.OTP_POLL_INTERVAL
    settle = settle_seconds if settle_seconds is not None else getattr(_email_cfg, "OTP_SETTLE_SECONDS", 5)
    last_error = ""
    best: tuple[int, float, str] | None = None
    settle_until: float | None = None

    logger.info(
        "[MailCom] IMAP 直连取码: %s login=%s 最长 %ss settle=%ss",
        email, _login_address(account), max_wait or _email_cfg.OTP_MAX_WAIT, settle,
    )
    while time.time() < deadline:
        try:
            with _inbox_lock(_login_address(account)):
                candidates = _imap_otp_candidates(account, email, after_ts)
            if candidates:
                candidates.sort(key=lambda x: (x[0], x[1]))
                score, msg_ts, code = candidates[-1]
                now = time.time()
                if best is None:
                    best = (score, msg_ts, code)
                    settle_until = now + float(settle or 0)
                    logger.info("[MailCom] 首次锁定 OTP=%s score=%s ts=%s，等 %ss", code, score, msg_ts, settle)
                elif (score, msg_ts, code) != best:
                    logger.info("[MailCom] 发现更新 OTP=%s，替换 %s", code, best[2])
                    best = (score, msg_ts, code)
                    settle_until = now + float(settle or 0)
                if settle_until is None or now >= settle_until:
                    logger.info("[MailCom] 返回 OTP=%s", best[2])
                    return best[2]
            else:
                last_error = "尚未出现 after_ts 之后的 ChatGPT 6 位验证码邮件"
        except MailComMailError as exc:
            last_error = str(exc)
            logger.warning("[MailCom] IMAP 取码失败 %s: %s", email, last_error)
            if "登录失败" in last_error or "AUTHENTICATIONFAILED" in last_error.upper():
                raise
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("[MailCom] IMAP 取码异常 %s: %s", email, last_error)
        time.sleep(max(1, int(interval)))

    if best:
        logger.info("[MailCom] 超时前回落已锁定 OTP=%s", best[2])
        return best[2]
    raise MailComMailError(f"等待 mail.com 验证码超时: {email}; last={last_error}")


def _configured_alias_domains() -> list[str]:
    out: list[str] = []
    for item in getattr(_email_cfg, "MAILCOM_ALIAS_DOMAINS", []) or []:
        domain = str(item or "").strip().lower().lstrip("@")
        if domain and domain not in out:
            out.append(domain)
    return out


def _alias_domain_choices(primary_email: str) -> list[str]:
    configured = _configured_alias_domains()
    if configured:
        return configured
    primary_domain = str(primary_email or "").rsplit("@", 1)[-1].strip().lower()
    out: list[str] = []
    if primary_domain:
        out.append(primary_domain)
    for item in list(getattr(_email_cfg, "MAILCOM_DOMAINS", []) or []) + list(
        getattr(_email_cfg, "MAILCOM_EXTRA_DOMAINS", []) or []
    ):
        domain = str(item or "").strip().lower().lstrip("@")
        if domain and domain not in out:
            out.append(domain)
    return out or ["mail.com"]


def _primary_local_compact(login_email: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(login_email or "").split("@", 1)[0].lower())


def _alias_digit_token() -> str:
    """2~4 位数字，降低 mail.com 全局撞名。"""
    width = random.choice((2, 2, 3, 3, 4))
    lo = 10 ** (width - 1)
    hi = 10 ** width - 1
    return f"{random.randint(lo, hi)}"


def _alias_local_candidates(login_email: str) -> list[str]:
    """独立姓名 + 数字 local-part，不沿用主邮箱前缀。"""
    from core.name_samples import FIRST_NAMES, LAST_NAMES, MIDDLE_NAMES

    login_local = _primary_local_compact(login_email)
    firsts = list(FIRST_NAMES)
    lasts = list(LAST_NAMES)
    middles = list(MIDDLE_NAMES)
    random.shuffle(firsts)
    random.shuffle(lasts)
    random.shuffle(middles)
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        text = re.sub(r"[^a-z0-9.]", "", str(value or "").lower())
        text = re.sub(r"\.{2,}", ".", text).strip(".")
        if not text or text in seen or not (3 <= len(text) <= 32):
            return
        if not re.search(r"\d{2,4}", text):
            return
        compact = text.replace(".", "")
        if login_local and (compact == login_local or compact.startswith(login_local)):
            return
        seen.add(text)
        out.append(text)

    pairs = min(len(firsts), len(lasts), 48)
    for i in range(pairs):
        first = firsts[i].lower()
        last = lasts[i].lower()
        middle = middles[i % len(middles)].lower()
        num = _alias_digit_token()
        add(f"{first}.{last}{num}")
        add(f"{first}{last}{num}")
        add(f"{first}.{last}.{num}")
        add(f"{first}.{middle}.{last}{num}")
        add(f"{last}.{first}{num}")
        add(f"{first[0]}{last}{num}")
    return out


def _new_alias_address(login_email: str, used: set[str]) -> str:
    domains = _alias_domain_choices(login_email)
    primary_domain = str(login_email or "").rsplit("@", 1)[-1].strip().lower()
    preferred = domains if _configured_alias_domains() else (
        [d for d in domains if d != primary_domain] or domains
    )
    if not preferred:
        preferred = ["mail.com"]
    used_l = {str(x or "").strip().lower() for x in used}
    for local in _alias_local_candidates(login_email):
        domain = random.choice(preferred)
        address = f"{local}@{domain}"
        if address not in used_l:
            return address
    raise MailComMailError("无法生成未占用的别名地址")


def expand_aliases(email: str, count: int = 3) -> dict:
    """为 mail.com 主邮箱同步已有别名，并再创建 count 个新别名入库。

    登录始终用主邮箱；别名作为独立注册地址领取，OTP 按收件人过滤。
    mail.com 单账号通常最多 1 个主地址 + 9 个别名。
    """
    from core.db import import_mailcom_emails, list_mailcom_related

    account = get_account_context(email)
    if account is None:
        raise MailComMailError(f"mail.com 邮箱不存在或未导入: {email}")
    login = _login_address(account)
    primary = get_account_context(login) or account
    if not primary.password:
        raise MailComMailError(f"mail.com 主邮箱缺少登录密码: {login}")

    want = max(0, min(int(count or 0), 9))
    # 创建别名走组内同一条代理，不写回邮箱池；登录被拦则换 sticky 重试。
    client, remote = _list_remote_aliases(primary)
    _persist_session(login, client)

    related = list_mailcom_related(login)
    known = {_cache_key(r.get("email")) for r in related}
    imported_existing: list[str] = []
    for address in remote:
        if address and address not in known:
            imported_existing.append(address)
            known.add(address)
    if imported_existing:
        import_mailcom_emails([
            {
                "email": address,
                "password": primary.password,
                "login_email": login,
                "note": f"已有别名，主邮箱 {login}",
            }
            for address in imported_existing
        ])

    related = list_mailcom_related(login)
    room = max(0, 10 - len(related))
    to_create = min(want, room)
    created: list[str] = []
    errors: list[str] = []
    used = set(known) | set(remote)
    for _ in range(to_create):
        address = ""
        last_err = ""
        for _attempt in range(5):
            address = _new_alias_address(login, used)
            try:
                with _inbox_lock(login):
                    client.add_alias(address)
                created.append(address)
                used.add(address)
                last_err = ""
                break
            except MailComError as exc:
                last_err = f"{exc.kind}: {exc}"
                used.add(address)
                if exc.kind == "alias_limit":
                    break
        if last_err:
            errors.append(f"{address}: {last_err}")
            if "alias_limit" in last_err:
                break
    _persist_session(login, client)
    if created:
        import_mailcom_emails([
            {
                "email": address,
                "password": primary.password,
                "login_email": login,
                "note": f"别名，主邮箱 {login}",
            }
            for address in created
        ])
        logger.info("[MailCom] %s 新建别名 %s 个: %s", login, len(created), created)
    return {
        "login_email": login,
        "imported_existing": imported_existing,
        "created": created,
        "errors": errors,
        "related_total": len(list_mailcom_related(login)),
    }
