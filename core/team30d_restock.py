# -*- coding: utf-8 -*-
"""30d.team 半自动兑换入池 + 401 自动找回。

不按号池消耗自动决定兑换数量；只消费用户贴进来的卡密。
401 检测到后自动找回并更新原号，不需要确认。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import sub2api as _cfg
from core.sub2api_pool_push import (
    build_pool_account_from_codex_json,
    normalize_upload_json_to_codex_entries,
    push_prepared_accounts_to_pool,
)
from core.team30d_client import (
    Team30dClient,
    Team30dError,
    extract_order_fields,
    health_need_reclaim,
    parse_card_codes,
    preview_action,
)

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "team30d_restock"
CONFIG_PATH = DATA_DIR / "config.json"
CARDS_PATH = DATA_DIR / "cards.json"
RUNS_PATH = DATA_DIR / "runs.jsonl"

_LOCK = threading.RLock()
_STOP = threading.Event()
_WAKE = threading.Event()
_WORKER: threading.Thread | None = None
_REDEEMING = False

DEFAULT_CONFIG: dict[str, Any] = {
    "api_base": "https://30d.team",
    "project": "30d_team",
    "format": "sub2api",
    "target_id": "",
    "push_group_id": int(getattr(_cfg, "SUB2API_POOL_GROUP_ID", 8) or 8),
    "concurrency": int(getattr(_cfg, "SUB2API_POOL_CONCURRENCY", 50) or 50),
    "priority": int(getattr(_cfg, "SUB2API_POOL_PRIORITY", 1) or 1),
    "load_factor": int(getattr(_cfg, "SUB2API_POOL_LOAD_FACTOR", 10) or 10),
    "rate_multiplier": float(getattr(_cfg, "SUB2API_POOL_RATE_MULTIPLIER", 1.0) or 1.0),
    "reclaim_enabled": True,
    "reclaim_interval_sec": 60,
    "timeout": 30.0,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _log(message: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{_now()} {message}"
    logger.info("[30d] %s", message)
    with RUNS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def get_config() -> dict[str, Any]:
    with _LOCK:
        raw = _read_json(CONFIG_PATH, {})
        cfg = dict(DEFAULT_CONFIG)
        if isinstance(raw, dict):
            cfg.update({k: raw[k] for k in DEFAULT_CONFIG if k in raw})
        cfg["push_group_id"] = int(cfg.get("push_group_id") or getattr(_cfg, "SUB2API_POOL_GROUP_ID", 8) or 8)
        return cfg


def save_config(updates: dict[str, Any] | None) -> dict[str, Any]:
    with _LOCK:
        cfg = get_config()
        for key, value in (updates or {}).items():
            if key in DEFAULT_CONFIG:
                cfg[key] = value
        _write_json(CONFIG_PATH, cfg)
        return cfg


def list_cards() -> list[dict]:
    with _LOCK:
        rows = _read_json(CARDS_PATH, [])
        return list(rows) if isinstance(rows, list) else []


def _save_cards(rows: list[dict]) -> None:
    _write_json(CARDS_PATH, rows)


def _upsert_card(record: dict) -> None:
    code = str(record.get("card_code") or "").strip()
    if not code:
        return
    with _LOCK:
        rows = list_cards()
        found = False
        for row in rows:
            if str(row.get("card_code") or "").strip().lower() == code.lower():
                row.update(record)
                found = True
                break
        if not found:
            rows.append(record)
        _save_cards(rows)


def get_log_tail(lines: int = 80) -> list[str]:
    try:
        text = RUNS_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = [ln for ln in text.splitlines() if ln.strip()]
    return rows[-max(1, min(500, int(lines or 80))):]


def _client(cfg: dict | None = None) -> Team30dClient:
    cfg = cfg or get_config()
    return Team30dClient(
        base_url=str(cfg.get("api_base") or "https://30d.team"),
        timeout=float(cfg.get("timeout") or 30),
    )


def _payload_accounts(payload: Any, *, card_code: str, cfg: dict) -> list[tuple[str, dict]]:
    entries = normalize_upload_json_to_codex_entries(payload, filename=f"{card_code}.json")
    prepared: list[tuple[str, dict]] = []
    for idx, entry in enumerate(entries):
        label = str(entry.get("email") or f"{card_code}#{idx + 1}")
        account = build_pool_account_from_codex_json(
            entry,
            filename=label,
            group_id=int(cfg.get("push_group_id") or 8),
            concurrency=int(cfg.get("concurrency") or 50),
            priority=int(cfg.get("priority") or 1),
            load_factor=int(cfg.get("load_factor") or 10),
            rate_multiplier=float(cfg.get("rate_multiplier") or 1),
            extra_patch={
                "import_source": "30d_team",
                "card_code": card_code,
                "provider": "30d.team",
            },
        )
        prepared.append((label, account))
    return prepared


def _apply_push_ids(card: dict, push: dict) -> None:
    emails = list(card.get("emails") or [])
    pool_ids = list(card.get("pool_ids") or [])
    for item in push.get("results") or []:
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        email = str(item.get("email") or "").strip()
        if email and email not in emails:
            emails.append(email)
        pid = item.get("account_id") or item.get("id")
        if pid not in (None, "") and int(pid) not in {int(x) for x in pool_ids if str(x).isdigit()}:
            try:
                pool_ids.append(int(pid))
            except (TypeError, ValueError):
                pass
    card["emails"] = emails
    card["pool_ids"] = pool_ids


def redeem_codes(text: str, *, target_id: str | None = None) -> dict:
    """把用户输入的卡密全部兑换并入池。不按库存计算数量。"""
    global _REDEEMING
    codes = parse_card_codes(text)
    if not codes:
        return {"ok": False, "error": "没有有效兑换码", "success": 0, "failed": 0, "results": []}
    with _LOCK:
        if _REDEEMING:
            return {"ok": False, "error": "已有兑换任务在跑", "success": 0, "failed": 0, "results": []}
        _REDEEMING = True
    cfg = get_config()
    tid = str(target_id if target_id is not None else cfg.get("target_id") or "").strip()
    client = _client(cfg)
    results: list[dict] = []
    success = 0
    failed = 0
    try:
        _log(f"开始兑换 {len(codes)} 张卡密")
        for code in codes:
            row: dict[str, Any] = {
                "card_code": code,
                "status": "redeeming",
                "updated_at": _now(),
            }
            try:
                preview = client.preview(
                    code,
                    format=str(cfg.get("format") or "sub2api"),
                    project=str(cfg.get("project") or "30d_team"),
                    target_id=tid or None,
                )
                action = preview_action(preview)
                if not action:
                    raise Team30dError("预览后无法兑换（无剩余额度且不能刷新已绑定账号）")
                raw = client.redeem(
                    code,
                    format=str(cfg.get("format") or "sub2api"),
                    project=str(cfg.get("project") or "30d_team"),
                    target_id=tid or None,
                    action=action,
                )
                waited = client.wait_order(raw)
                order_no, token, status = extract_order_fields(waited)
                if not token or not order_no:
                    no0, tok0, _ = extract_order_fields(raw)
                    order_no = order_no or no0
                    token = token or tok0
                if not order_no or not token:
                    raise Team30dError(f"兑换响应缺少 order_no/download_token status={status}")
                payload = client.download(order_no, token)
                prepared = _payload_accounts(payload, card_code=code, cfg=cfg)
                if not prepared:
                    raise Team30dError("下载结果里没有可用 access_token")
                push = push_prepared_accounts_to_pool(prepared)
                row.update({
                    "status": "pushed" if push.get("ok") or push.get("success") else "push_partial",
                    "order_no": order_no,
                    "download_token": token,
                    "order_status": status,
                    "push_success": int(push.get("success") or 0),
                    "push_failed": int(push.get("failed") or 0),
                    "updated_at": _now(),
                })
                _apply_push_ids(row, push)
                if push.get("failed"):
                    failed += 1
                    row["error"] = f"入池部分失败 success={push.get('success')} failed={push.get('failed')}"
                else:
                    success += 1
                    row.pop("error", None)
                _log(f"卡密 {code} 兑换完成 入池成功={row.get('push_success')} 失败={row.get('push_failed')}")
            except Exception as exc:
                failed += 1
                row["status"] = "failed"
                row["error"] = f"{type(exc).__name__}: {exc}"[:400]
                row["updated_at"] = _now()
                _log(f"卡密 {code} 失败: {row['error']}")
            _upsert_card(row)
            results.append({k: v for k, v in row.items() if k != "download_token"})
        _log(f"兑换结束 success={success} failed={failed}")
        return {"ok": failed == 0, "success": success, "failed": failed, "total": len(codes), "results": results}
    finally:
        with _LOCK:
            _REDEEMING = False


def _pool_401_card_codes(cards: list[dict]) -> list[str]:
    """号池里 30d 号若报 401，对应卡密也要找回。检测失败不算。"""
    try:
        from core.sub2api_pool_monitor import fetch_pool_accounts, _pool_email
    except Exception as exc:
        _log(f"拉取号池失败，跳过号池 401 扫描: {type(exc).__name__}: {exc}")
        return []
    cfg = get_config()
    try:
        accounts = fetch_pool_accounts(group_id=int(cfg.get("push_group_id") or 8))
    except Exception as exc:
        _log(f"号池列表失败，不触发找回: {type(exc).__name__}: {exc}")
        return []
    email_to_code: dict[str, str] = {}
    for card in cards:
        code = str(card.get("card_code") or "").strip()
        for email in card.get("emails") or []:
            email_to_code[str(email or "").strip().lower()] = code
    out: list[str] = []
    seen: set[str] = set()
    for acc in accounts:
        extra = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}
        code = str(extra.get("card_code") or "").strip()
        source = str(extra.get("import_source") or "").strip().lower()
        if not code:
            email = _pool_email(acc)
            code = email_to_code.get(email, "")
        if not code and source != "30d_team":
            continue
        blob = " ".join([
            str(acc.get("error_message") or ""),
            str(acc.get("error") or ""),
            str(acc.get("status") or ""),
            str(acc.get("last_error") or ""),
        ]).lower()
        if "401" not in blob and "unauthorized" not in blob:
            continue
        key = (code or str(acc.get("id") or "")).lower()
        if key and key not in seen:
            seen.add(key)
            if code:
                out.append(code)
    return out


def _update_pool_from_payload(payload: Any, card: dict, cfg: dict) -> dict:
    from core.sub2api_pool_monitor import _update_pool_credentials, fetch_pool_accounts, _pool_email

    prepared = _payload_accounts(payload, card_code=str(card.get("card_code") or ""), cfg=cfg)
    if not prepared:
        raise Team30dError("找回下载结果没有可用 access_token")
    id_by_email: dict[str, int] = {}
    for pid in card.get("pool_ids") or []:
        try:
            int(pid)
        except (TypeError, ValueError):
            continue
    try:
        accounts = fetch_pool_accounts(group_id=int(cfg.get("push_group_id") or 8))
        for acc in accounts:
            extra = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}
            email = _pool_email(acc)
            if extra.get("card_code") == card.get("card_code") or email:
                try:
                    id_by_email[email] = int(acc.get("id"))
                except (TypeError, ValueError):
                    pass
    except Exception as exc:
        _log(f"找回后匹配号池失败，改为重新推送: {type(exc).__name__}: {exc}")

    updated = 0
    created = 0
    errors: list[str] = []
    leftover: list[tuple[str, dict]] = []
    known_ids = [int(x) for x in (card.get("pool_ids") or []) if str(x).isdigit()]
    for label, account in prepared:
        email = str((account.get("name") or "")).strip().lower()
        pid = id_by_email.get(email)
        if pid is None and len(known_ids) == 1 and len(prepared) == 1:
            pid = known_ids[0]
        if pid is not None:
            try:
                _update_pool_credentials(pid, account)
                updated += 1
            except Exception as exc:
                errors.append(f"{email}: {type(exc).__name__}: {exc}")
        else:
            leftover.append((label, account))
    if leftover:
        push = push_prepared_accounts_to_pool(leftover)
        created += int(push.get("success") or 0)
        if push.get("failed"):
            errors.append(f"补推失败 {push.get('failed')}")
        _apply_push_ids(card, push)
    return {"updated": updated, "created": created, "errors": errors}


def run_reclaim_once() -> dict:
    """健康检查 + 号池 401 → 自动找回并更新原号。检测失败不找回。"""
    cfg = get_config()
    cards = [c for c in list_cards() if str(c.get("card_code") or "").strip()]
    codes = [str(c.get("card_code")).strip() for c in cards]
    if not codes:
        return {"ok": True, "checked": 0, "reclaimed": 0, "message": "没有已兑换卡密"}
    client = _client(cfg)
    need: list[str] = []
    try:
        health = client.health_check(codes)
        need = health_need_reclaim(health)
    except Exception as exc:
        _log(f"health-check 失败，不发起找回: {type(exc).__name__}: {exc}")
        health_failed = True
    else:
        health_failed = False
    try:
        pool_need = _pool_401_card_codes(cards)
    except Exception as exc:
        _log(f"号池 401 扫描异常，忽略: {type(exc).__name__}: {exc}")
        pool_need = []
    for code in pool_need:
        if code not in need:
            need.append(code)
    if health_failed and not pool_need:
        return {"ok": False, "checked": len(codes), "reclaimed": 0, "error": "health-check 失败，未找回"}
    if not need:
        return {"ok": True, "checked": len(codes), "reclaimed": 0, "message": "无需找回"}
    _log(f"检测到需找回 {len(need)} 张卡密，自动找回")
    try:
        client.batch_reclaim(need, mode="401")
        progress = client.poll_reclaim(need)
    except Exception as exc:
        _log(f"找回请求失败: {type(exc).__name__}: {exc}")
        return {"ok": False, "checked": len(codes), "reclaimed": 0, "error": str(exc)[:300]}

    reclaimed = 0
    errors: list[str] = []
    by_code = {str(c.get("card_code") or "").strip().lower(): c for c in cards}
    for code in need:
        card = by_code.get(code.lower()) or {"card_code": code}
        order_no = str(card.get("order_no") or "").strip()
        token = str(card.get("download_token") or "").strip()
        if not order_no or not token:
            errors.append(f"{code}: 缺少 download_token，无法下载找回结果")
            continue
        try:
            payload = client.download(order_no, token)
            stats = _update_pool_from_payload(payload, card, cfg)
            card["status"] = "reclaimed"
            card["last_reclaim_at"] = _now()
            card["updated_at"] = _now()
            _upsert_card(card)
            reclaimed += 1
            _log(f"卡密 {code} 找回完成 updated={stats.get('updated')} created={stats.get('created')}")
            if stats.get("errors"):
                errors.extend(stats["errors"])
        except Exception as exc:
            errors.append(f"{code}: {type(exc).__name__}: {exc}")
            _log(f"卡密 {code} 找回后更新失败: {exc}")
    return {
        "ok": not errors,
        "checked": len(codes),
        "need": len(need),
        "reclaimed": reclaimed,
        "progress": progress,
        "errors": errors[:20],
    }


def get_status() -> dict:
    cards = list_cards()
    return {
        "config": get_config(),
        "redeeming": _REDEEMING,
        "worker_alive": bool(_WORKER and _WORKER.is_alive()),
        "cards": len(cards),
        "card_items": [{k: v for k, v in c.items() if k != "download_token"} for c in cards[-50:]],
    }


def _worker_loop() -> None:
    while not _STOP.is_set():
        cfg = get_config()
        interval = max(15, int(cfg.get("reclaim_interval_sec") or 60))
        if cfg.get("reclaim_enabled"):
            try:
                run_reclaim_once()
            except Exception as exc:
                _log(f"找回轮次异常: {type(exc).__name__}: {exc}")
        _WAKE.clear()
        _WAKE.wait(timeout=interval)


def ensure_reclaim_worker_started() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _STOP.clear()
        _WORKER = threading.Thread(target=_worker_loop, name="team30d-reclaim", daemon=True)
        _WORKER.start()
        _log("401 找回 worker 已启动")
