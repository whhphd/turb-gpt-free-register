# -*- coding: utf-8 -*-
"""mail.com / kittymail 等品牌邮箱：本地池 + 网页协议收 ChatGPT OTP。"""
from __future__ import annotations

import logging
import random
import re
import secrets
import time
from dataclasses import dataclass, field
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
    for email in emails:
        try:
            out = expand_aliases(email, count=want)
            created += len(out.get("created") or [])
            imported_existing += len(out.get("imported_existing") or [])
            if out.get("errors"):
                failed.append({"email": email, "error": "; ".join(out["errors"][:3])})
        except Exception as exc:
            logger.warning("[MailCom] 导入后自动创建别名失败 %s: %s: %s", email, type(exc).__name__, exc)
            failed.append({"email": email, "error": f"{type(exc).__name__}: {exc}"})
    if created or imported_existing:
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
            proxy_url = proxy_url or parent.get("proxy_url") or ""
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


def resolve_mailcom_proxy(account: MailComAccount, *, force_pool: bool = False) -> str:
    """账号自带代理优先；否则按开关从 PROXY_POOL 抽一条并绑定。

    force_pool=True 时（创建别名）必须走代理池，不受 MAILCOM_USE_PROXY_POOL 影响。
    """
    if account.proxy_url:
        return _proxy_for_requests(account.proxy_url)
    if not force_pool and not bool(getattr(_email_cfg, "MAILCOM_USE_PROXY_POOL", False)):
        return ""
    from config.proxy import pick_proxy

    chosen = str(pick_proxy() or "").strip()
    if not chosen:
        raise MailComMailError("mail.com 需要代理池出口，但代理池为空")
    chosen = _proxy_for_requests(normalize_mailcom_proxy(chosen))
    account.proxy_url = chosen
    try:
        from core.db import update_mailcom_proxy
        update_mailcom_proxy(_login_address(account), chosen)
        if account.email and _cache_key(account.email) != _login_address(account):
            update_mailcom_proxy(account.email, chosen)
    except Exception as exc:
        logger.debug("[MailCom] 绑定代理未写入池：%s: %s", type(exc).__name__, exc)
    logger.info("[MailCom] 为 %s 绑定代理池出口：%s", account.email, _mask_proxy(chosen))
    return chosen


def _client_for(account: MailComAccount) -> MailComClient:
    login = _login_address(account)
    proxy = resolve_mailcom_proxy(account)
    cached = _CLIENT_CACHE.get(login)
    if cached and cached.password == account.password and (cached.proxy_url or "") == (proxy or ""):
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


def _message_ts(date_ms: int) -> float:
    if not date_ms:
        return 0.0
    return date_ms / 1000.0 if date_ms > 10_000_000_000 else float(date_ms)


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
    client = _client_for(account)
    last_error = ""
    best: tuple[int, float, str] | None = None
    settle_until: float | None = None

    logger.info(
        "[MailCom] 开始收信取码: %s，最长 %ss settle=%ss",
        email, max_wait or _email_cfg.OTP_MAX_WAIT, settle,
    )
    while time.time() < deadline:
        try:
            messages = client.query_messages(account.email, amount=20)
            _persist_session(email, client)
            candidates: list[tuple[int, float, str]] = []
            for msg in messages:
                msg_ts = _message_ts(msg.date_ms)
                if after_ts and msg_ts and msg_ts + 2 < after_ts:
                    continue
                try:
                    body = client.get_body(msg.mail_id)
                except MailComError as exc:
                    last_error = f"{exc.kind}: {exc}"
                    continue
                if not _is_chatgpt_otp_mail(msg.subject, msg.sender, body):
                    continue
                code = extract_otp({
                    "subject": msg.subject,
                    "from": msg.sender,
                    "text": body,
                    "content": body,
                })
                if not code:
                    continue
                candidates.append((_otp_score(msg.subject), msg_ts, code))
            _persist_session(email, client)
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
        except MailComError as exc:
            last_error = f"{exc.kind}: {exc}"
            logger.warning("[MailCom] 取码失败 %s: %s", email, last_error)
            if exc.kind in {"bad_credentials", "blocked"}:
                raise MailComMailError(f"mail.com 登录失败 ({exc.kind}): {exc}") from exc
            _CLIENT_CACHE.pop(_login_address(account), None)
        time.sleep(max(1, int(interval)))

    if best:
        logger.info("[MailCom] 超时前回落已锁定 OTP=%s", best[2])
        return best[2]
    raise MailComMailError(f"等待 mail.com 验证码超时: {email}; last={last_error}")


def _alias_domain_choices(primary_email: str) -> list[str]:
    primary_domain = str(primary_email or "").rsplit("@", 1)[-1].strip().lower()
    out: list[str] = []
    if primary_domain:
        out.append(primary_domain)
    for item in list(getattr(_email_cfg, "MAILCOM_DOMAINS", []) or []) + list(
        getattr(_email_cfg, "MAILCOM_EXTRA_DOMAINS", []) or []
    ):
        domain = str(item or "").strip().lower()
        if domain and domain not in out:
            out.append(domain)
    return out or ["mail.com"]


def _new_alias_address(login_email: str, used: set[str]) -> str:
    local = re.sub(r"[^a-z0-9]", "", str(login_email).split("@", 1)[0].lower())[:10] or "mail"
    domains = _alias_domain_choices(login_email)
    for _ in range(30):
        address = f"{local}{secrets.token_hex(3)}@{random.choice(domains)}"
        if address not in used:
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
    # 创建/同步别名必须走代理池，避免 Settings 接口直连被风控。
    proxy = resolve_mailcom_proxy(primary, force_pool=True)
    primary.proxy_url = proxy
    logger.info("[MailCom] 创建别名使用代理：login=%s proxy=%s", login, _mask_proxy(proxy))
    _CLIENT_CACHE.pop(login, None)
    client = _client_for(primary)
    try:
        remote = [str(x or "").strip().lower() for x in client.list_aliases() if str(x or "").strip()]
    except MailComError as exc:
        raise MailComMailError(f"读取 mail.com 别名失败 ({exc.kind}): {exc}") from exc
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
                "proxy_url": primary.proxy_url,
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
                "proxy_url": primary.proxy_url,
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
