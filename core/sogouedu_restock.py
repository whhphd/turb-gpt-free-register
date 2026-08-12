# -*- coding: utf-8 -*-
"""SogouEdu 自动补池编排服务。

该模块只负责 sogouedu 购号、取货、恢复和推池流程；既有本地注册及号池巡视逻辑
不在这里调用或修改。业务状态使用项目 ``data/sogou_restock`` 下的原子 JSON 文件
保存，进程重启后可以继续处理未完成订单和恢复记录。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import sub2api as _cfg
from core import sub2api_pool_monitor as _pool_monitor
from core.sogouedu_client import SogouEduClient, SogouEduError
from core.sub2api_pool_push import (
    build_pool_account_from_codex_json,
    normalize_upload_json_to_codex_entries,
    push_prepared_accounts_to_pool,
)

logger = logging.getLogger(__name__)

RESTOCK_DIR = Path(__file__).resolve().parents[1] / "data" / "sogou_restock"
CONFIG_PATH = RESTOCK_DIR / "config.json"
STATE_PATH = RESTOCK_DIR / "state.json"
ORDERS_PATH = RESTOCK_DIR / "orders.json"
RECOVERIES_PATH = RESTOCK_DIR / "recoveries.json"
RUNS_PATH = RESTOCK_DIR / "runs.jsonl"

_LOCK = threading.RLock()
_RUNNING = False
_STOP = threading.Event()
_WAKE = threading.Event()
_WORKER: threading.Thread | None = None

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "monitor_group_id": int(getattr(_cfg, "SUB2API_POOL_GROUP_ID", 8) or 8),
    "push_group_id": int(getattr(_cfg, "SUB2API_POOL_GROUP_ID", 8) or 8),
    "product": "oauth_7d",
    "min_healthy": 5,
    "target_healthy": 10,
    "max_purchase_per_order": 5,
    "monitor_interval_sec": 60,
    "order_poll_interval_sec": 3,
    "recovery_poll_interval_sec": 30,
    "concurrency": int(getattr(_cfg, "SUB2API_POOL_CONCURRENCY", 50) or 50),
    "priority": int(getattr(_cfg, "SUB2API_POOL_PRIORITY", 1) or 1),
    "load_factor": int(getattr(_cfg, "SUB2API_POOL_LOAD_FACTOR", 10) or 10),
    "rate_multiplier": float(getattr(_cfg, "SUB2API_POOL_RATE_MULTIPLIER", 1.0) or 1.0),
    "auto_pause_on_expired": bool(getattr(_cfg, "SUB2API_POOL_AUTO_PAUSE_ON_EXPIRED", True)),
    "model_whitelist": [],
}

_CONFIG_KEYS = frozenset(DEFAULT_CONFIG)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    tmp.replace(path)


def _config_defaults() -> dict[str, Any]:
    # Read pool defaults at call time so an existing deployment can change its
    # normal pool settings without rewriting the restock file.
    defaults = dict(DEFAULT_CONFIG)
    defaults["monitor_group_id"] = int(getattr(_cfg, "SUB2API_POOL_GROUP_ID", defaults["monitor_group_id"]) or defaults["monitor_group_id"])
    defaults["push_group_id"] = defaults["monitor_group_id"]
    defaults["concurrency"] = int(getattr(_cfg, "SUB2API_POOL_CONCURRENCY", defaults["concurrency"]) or defaults["concurrency"])
    defaults["priority"] = int(getattr(_cfg, "SUB2API_POOL_PRIORITY", defaults["priority"]) or defaults["priority"])
    defaults["load_factor"] = int(getattr(_cfg, "SUB2API_POOL_LOAD_FACTOR", defaults["load_factor"]) or defaults["load_factor"])
    defaults["rate_multiplier"] = float(getattr(_cfg, "SUB2API_POOL_RATE_MULTIPLIER", defaults["rate_multiplier"]) or defaults["rate_multiplier"])
    defaults["auto_pause_on_expired"] = bool(getattr(_cfg, "SUB2API_POOL_AUTO_PAUSE_ON_EXPIRED", defaults["auto_pause_on_expired"]))
    return defaults


def normalize_restock_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = _config_defaults()
    if isinstance(raw, dict):
        cfg.update({key: raw[key] for key in _CONFIG_KEYS if key in raw})
    cfg["enabled"] = bool(cfg.get("enabled", False))
    for key in ("monitor_group_id", "push_group_id", "min_healthy", "target_healthy", "max_purchase_per_order", "concurrency", "priority", "load_factor"):
        try:
            cfg[key] = int(cfg.get(key) or 0)
        except (TypeError, ValueError):
            cfg[key] = int(_config_defaults().get(key, 1))
    cfg["monitor_group_id"] = max(1, cfg["monitor_group_id"])
    cfg["push_group_id"] = max(1, cfg["push_group_id"])
    cfg["min_healthy"] = max(0, cfg["min_healthy"])
    cfg["target_healthy"] = max(cfg["min_healthy"], cfg["target_healthy"])
    cfg["max_purchase_per_order"] = max(1, cfg["max_purchase_per_order"])
    for key in ("monitor_interval_sec", "order_poll_interval_sec", "recovery_poll_interval_sec"):
        try:
            cfg[key] = max(1, int(cfg.get(key) or 1))
        except (TypeError, ValueError):
            cfg[key] = 1
    try:
        cfg["rate_multiplier"] = max(0.0, float(cfg.get("rate_multiplier") or 0.0))
    except (TypeError, ValueError):
        cfg["rate_multiplier"] = 1.0
    cfg["auto_pause_on_expired"] = bool(cfg.get("auto_pause_on_expired", True))
    cfg["product"] = str(cfg.get("product") or "oauth_7d").strip()
    if cfg["product"] not in {"oauth_7d", "oauth_30d"}:
        cfg["product"] = "oauth_7d"
    models = cfg.get("model_whitelist")
    if not isinstance(models, list):
        models = []
    cfg["model_whitelist"] = list(dict.fromkeys(str(item).strip() for item in models if str(item).strip()))
    return cfg


def load_restock_config() -> dict[str, Any]:
    return normalize_restock_config(_read_json(CONFIG_PATH, {}))


def save_restock_config(patch: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    values = dict(patch or {})
    values.update(kwargs)
    current = load_restock_config()
    current.update({key: value for key, value in values.items() if key in _CONFIG_KEYS})
    normalized = normalize_restock_config(current)
    _write_json(CONFIG_PATH, normalized)
    return normalized


def _load_state() -> dict[str, Any]:
    state = _read_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("current_order", None)
    state.setdefault("last_run", None)
    state.setdefault("inventory", None)
    state.setdefault("last_recovery_scan_at", None)
    state.setdefault("recovery_cursor", None)
    return state


def _save_state(state: dict[str, Any]) -> None:
    _write_json(STATE_PATH, state)


def _append_run(event: dict[str, Any]) -> None:
    RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    safe = dict(event)
    safe.pop("payload", None)
    safe.pop("credentials", None)
    with RUNS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False) + "\n")


def get_restock_log_tail(lines: int = 80) -> list[dict[str, Any]]:
    try:
        rows = RUNS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    output: list[dict[str, Any]] = []
    for row in rows[-max(1, min(500, int(lines or 80))):]:
        try:
            value = json.loads(row)
            if isinstance(value, dict):
                output.append(value)
        except json.JSONDecodeError:
            continue
    return output


def _extract_data(body: Any) -> Any:
    if isinstance(body, dict) and "data" in body:
        return body.get("data")
    return body


def _extract_items(body: Any, *keys: str) -> list[dict]:
    data = _extract_data(body)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _value(body: Any, *keys: str) -> Any:
    data = _extract_data(body)
    if isinstance(data, dict):
        for key in keys:
            if data.get(key) not in (None, ""):
                return data.get(key)
    if isinstance(body, dict):
        for key in keys:
            if body.get(key) not in (None, ""):
                return body.get(key)
    return None


def _parse_time(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return number
    except (TypeError, ValueError):
        pass
    try:
        raw = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def is_healthy_pool_account(account: dict[str, Any], *, now: float | None = None) -> bool:
    if not isinstance(account, dict):
        return False
    status = str(account.get("status") or "").strip().lower()
    if status not in {"active", "ok", "healthy", "enabled"}:
        return False
    if account.get("schedulable") is False:
        return False
    error = str(account.get("error_message") or account.get("errorMessage") or account.get("last_error") or "").strip().lower()
    if error:
        return False
    expiry = _parse_time(account.get("expires_at") or account.get("expired") or (account.get("credentials") or {}).get("expires_at"))
    return expiry is None or expiry > (now if now is not None else time.time())


def count_healthy_accounts(accounts: list[dict[str, Any]], *, now: float | None = None) -> int:
    return sum(1 for account in (accounts or []) if is_healthy_pool_account(account, now=now))


def calculate_purchase_quantity(healthy: int, cfg: dict[str, Any]) -> int:
    gap = max(0, int(cfg["target_healthy"]) - int(healthy))
    return min(gap, max(1, int(cfg["max_purchase_per_order"])))


def _safe_order(order: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(order, dict):
        return None
    return {key: value for key, value in order.items() if key not in {"payload", "pending_payload", "credentials"}}


def get_restock_status() -> dict[str, Any]:
    cfg = load_restock_config()
    state = _load_state()
    return {
        "config": cfg,
        "running": bool(_RUNNING),
        "current_order": _safe_order(state.get("current_order")),
        "last_run": state.get("last_run"),
        "inventory": state.get("inventory"),
        "last_recovery_scan_at": state.get("last_recovery_scan_at"),
        "recovery_cursor": state.get("recovery_cursor"),
        "credentials_configured": bool(getattr(_cfg, "SOGOUEDU_USERNAME", "") and getattr(_cfg, "SOGOUEDU_PASSWORD", "")),
    }


def _order_id(body: Any) -> str:
    value = _value(body, "order_id", "orderId", "id")
    if isinstance(value, dict):
        value = value.get("id") or value.get("order_id")
    if not value:
        data = _extract_data(body)
        if isinstance(data, dict):
            nested = data.get("order") or data.get("pickup_order")
            if isinstance(nested, dict):
                value = nested.get("id") or nested.get("order_id") or nested.get("orderId")
    return str(value or "").strip()


def _order_status(body: Any) -> str:
    return str(_value(body, "status", "state", "order_status", "orderStatus") or "").strip().lower()


def _payload_accounts(body: Any) -> list[dict]:
    for value in (
        body,
        _extract_data(body),
        _value(body, "payload", "result", "accounts", "items"),
    ):
        if isinstance(value, dict):
            for key in ("accounts", "items", "data"):
                if isinstance(value.get(key), list):
                    return [item for item in value[key] if isinstance(item, dict)]
        elif isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _pool_extra(account: dict[str, Any]) -> dict[str, Any]:
    return account.get("extra") if isinstance(account.get("extra"), dict) else {}


def _match_sogou_account(accounts: list[dict], recovery: dict[str, Any]) -> dict[str, Any] | None:
    recovery_pool_id = str(recovery.get("pool_id") or recovery.get("account_id") or "").strip()
    email = str(recovery.get("email") or recovery.get("username") or "").strip().lower()
    for account in accounts:
        extra = _pool_extra(account)
        if extra.get("import_source") != "sogouedu_auto_restock":
            continue
        if recovery_pool_id and str(account.get("id") or account.get("account_id") or "") == recovery_pool_id:
            return account
        creds = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
        account_email = str(account.get("email") or creds.get("email") or account.get("name") or "").strip().lower()
        if email and email == account_email:
            return account
    return None


def _build_prepared(payload: Any, cfg: dict[str, Any], *, order_id: str = "", recreated: bool = False, replacement_of_pool_id: Any = None) -> list[tuple[str, dict]]:
    entries = normalize_upload_json_to_codex_entries(payload, filename=f"sogou-{order_id or 'recovery'}.json")
    prepared: list[tuple[str, dict]] = []
    for index, entry in enumerate(entries):
        label = str(entry.get("email") or f"sogou-{order_id or 'recovery'}#{index}")
        extra = {
            "import_source": "sogouedu_auto_restock",
            "sogou_order_id": order_id,
            "sogou_product": cfg["product"],
        }
        if recreated:
            extra["recreated"] = True
            if replacement_of_pool_id not in (None, ""):
                extra["replacement_of_pool_id"] = replacement_of_pool_id
        account = build_pool_account_from_codex_json(
            entry,
            filename=f"sogou-{order_id or 'recovery'}.json",
            group_id=cfg["push_group_id"],
            concurrency=cfg["concurrency"],
            priority=cfg["priority"],
            load_factor=cfg["load_factor"],
            rate_multiplier=cfg["rate_multiplier"],
            model_whitelist=cfg["model_whitelist"],
            extra_patch=extra,
            auto_pause_on_expired=cfg["auto_pause_on_expired"],
        )
        prepared.append((label, account))
    return prepared


def _process_current_order(client: SogouEduClient, cfg: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    order = state.get("current_order")
    if not isinstance(order, dict):
        return {"handled": False}
    snapshot = order.get("config_snapshot") if isinstance(order.get("config_snapshot"), dict) else {}
    order_cfg = normalize_restock_config({**cfg, **snapshot})
    order_id = str(order.get("order_id") or "").strip()
    if not order_id:
        key = str(order.get("idempotency_key") or "").strip()
        if not key:
            key = f"sogou-restock-{uuid.uuid4().hex}"
            order["idempotency_key"] = key
            state["current_order"] = order
            _save_state(state)
        response = client.create_order(order.get("product") or order_cfg["product"], int(order["quantity"]), idempotency_key=key)
        order_id = _order_id(response)
        if not order_id:
            order["last_error"] = "订单响应缺少 order_id"
            _save_state(state)
            raise RuntimeError(order["last_error"])
        order["order_id"] = order_id
        order["status"] = _order_status(response) or "pending"
        order["updated_at"] = _now()
        _write_json(ORDERS_PATH, _read_json(ORDERS_PATH, []) + [_safe_order(order)])
        _save_state(state)
        return {"handled": True, "action": "ordered", "order_id": order_id}

    if not order.get("payload"):
        last_polled = _parse_time(order.get("last_polled_at"))
        if last_polled and time.time() - last_polled < cfg["order_poll_interval_sec"]:
            return {"handled": True, "action": "waiting", "order_id": order_id, "status": order.get("status") or "pending"}
        response = client.order_status(order_id, status_url=order.get("status_url"))
        order["status"] = _order_status(response) or order.get("status") or "pending"
        order["last_polled_at"] = _now()
        status = order["status"]
        if status in {"failed", "cancelled", "canceled", "refunded", "error"}:
            order["last_error"] = str(_value(response, "message", "error") or status)
            state["current_order"] = None
            _save_state(state)
            return {"handled": True, "action": "order_failed", "order_id": order_id}
        if status not in {"ready", "completed", "success", "available", "fulfilled", "done"}:
            order["updated_at"] = _now()
            _save_state(state)
            return {"handled": True, "action": "waiting", "order_id": order_id, "status": status}
        response = client.take_order(order_id, take_url=order.get("take_url"))
        payload = response.get("data") if isinstance(response, dict) and isinstance(response.get("data"), (dict, list)) else response
        order["payload"] = payload
        order["status"] = "taken"
        order["updated_at"] = _now()
        _save_state(state)

    prepared = _build_prepared(order.get("payload"), order_cfg, order_id=order_id)
    if not prepared:
        order["last_error"] = "取货响应未包含有效 OAuth 账号"
        _save_state(state)
        return {"handled": True, "action": "push_waiting", "order_id": order_id}
    result = push_prepared_accounts_to_pool(prepared)
    order["last_push"] = {"success": result.get("success", 0), "failed": result.get("failed", 0)}
    order["updated_at"] = _now()
    if result.get("failed", 0):
        _save_state(state)
        return {"handled": True, "action": "push_retry", "order_id": order_id, "result": order["last_push"]}
    state["current_order"] = None
    _save_state(state)
    return {"handled": True, "action": "pushed", "order_id": order_id, "result": order["last_push"]}


def _process_recoveries(client: SogouEduClient, cfg: dict[str, Any], state: dict[str, Any], accounts: list[dict]) -> dict[str, Any]:
    now = time.time()
    last = _parse_time(state.get("last_recovery_scan_at"))
    if last and now - last < cfg["recovery_poll_interval_sec"]:
        return {"scanned": False, "repaired": 0, "recreated": 0}
    body = client.list_recoveries(before_id=state.get("recovery_cursor"), limit=100)
    items = _extract_items(body, "items", "recoveries", "records")
    repaired = recreated = 0
    newest = state.get("recovery_cursor")
    for recovery in items:
        rid = recovery.get("id") or recovery.get("recovery_id")
        if rid in (None, ""):
            continue
        newest = rid
        status = str(recovery.get("status") or recovery.get("delivery_status") or "").lower()
        if status in {"recovered", "claimed", "completed", "success", "repaired"}:
            continue
        try:
            claim = client.claim_recovery(rid, claim_url=recovery.get("claim_url"), ticket=recovery.get("ticket"))
            payload = claim.get("data") if isinstance(claim, dict) and isinstance(claim.get("data"), (dict, list)) else claim
            matched = _match_sogou_account(accounts, recovery)
            if matched:
                entries = normalize_upload_json_to_codex_entries(payload, filename=f"sogou-recovery-{rid}.json")
                if entries:
                    account_payload = build_pool_account_from_codex_json(entries[0], filename=f"sogou-recovery-{rid}.json")
                    pool_id = matched.get("id") or matched.get("account_id")
                    _pool_monitor._update_pool_credentials(int(pool_id), account_payload)
                    repaired += 1
            else:
                replacement = recovery.get("pool_id") or recovery.get("account_id")
                prepared = _build_prepared(payload, cfg, order_id=f"recovery-{rid}", recreated=True, replacement_of_pool_id=replacement)
                result = push_prepared_accounts_to_pool(prepared)
                if result.get("failed", 0) == 0 and prepared:
                    recreated += result.get("success", 0)
            recovery["processed_at"] = _now()
            recovery["result"] = "repaired" if matched else "recreated"
        except Exception as exc:
            recovery["last_error"] = f"{type(exc).__name__}: {exc}"
        _write_json(RECOVERIES_PATH, items)
    state["last_recovery_scan_at"] = _now()
    state["recovery_cursor"] = newest
    _save_state(state)
    return {"scanned": True, "repaired": repaired, "recreated": recreated}


def run_restock_cycle(*, force: bool = False, client: SogouEduClient | None = None) -> dict[str, Any]:
    """执行一次补池循环；同一进程内并发调用会被合并为一次。"""
    global _RUNNING
    with _LOCK:
        if _RUNNING:
            return {"ok": False, "skipped": True, "reason": "already_running"}
        _RUNNING = True
    started = _now()
    cfg = load_restock_config()
    state = _load_state()
    result: dict[str, Any] = {"ok": False, "started_at": started}
    try:
        if not force and not cfg["enabled"]:
            result.update({"ok": True, "skipped": True, "reason": "disabled"})
            return result
        api = client or SogouEduClient()
        order_result = _process_current_order(api, cfg, state)
        if order_result.get("handled"):
            result.update(order_result)
            result["ok"] = True
            return result
        accounts = _pool_monitor.fetch_pool_accounts(group_id=cfg["monitor_group_id"], platform="openai", account_type="oauth")
        active_accounts = _pool_monitor.fetch_pool_accounts(
            group_id=cfg["monitor_group_id"],
            platform="openai",
            account_type="oauth",
            status="active",
        )
        healthy = len(active_accounts)
        result["healthy"] = healthy
        result["total"] = len(accounts)
        state["inventory"] = {
            "healthy": healthy,
            "total": len(accounts),
            "checked_at": _now(),
        }
        _save_state(state)
        result["recovery"] = _process_recoveries(api, cfg, state, accounts)
        quantity = calculate_purchase_quantity(healthy, cfg)
        result["quantity"] = quantity
        if quantity <= 0:
            result.update({"ok": True, "action": "inventory_ok"})
            return result
        api.balance()
        api.inventory(cfg["product"], quantity)
        key = f"sogou-restock-{uuid.uuid4().hex}"
        state["current_order"] = {
            "idempotency_key": key,
            "quantity": quantity,
            "product": cfg["product"],
            "created_at": _now(),
            "status": "creating",
            "config_snapshot": {key: cfg[key] for key in ("push_group_id", "concurrency", "priority", "load_factor", "rate_multiplier", "model_whitelist", "auto_pause_on_expired", "product")},
        }
        _save_state(state)
        order_result = _process_current_order(api, cfg, state)
        result.update(order_result)
        result["ok"] = True
        return result
    except SogouEduError as exc:
        result.update({"error": str(exc), "status_code": exc.status_code})
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        logger.exception("[sogou补池] cycle failed")
        return result
    finally:
        result["finished_at"] = _now()
        state = _load_state()
        state["last_run"] = {key: value for key, value in result.items() if key not in {"payload", "credentials"}}
        _save_state(state)
        _append_run(state["last_run"])
        with _LOCK:
            _RUNNING = False


def trigger_restock_run_now() -> dict[str, Any]:
    return run_restock_cycle(force=True)


def set_restock_enabled(enabled: bool, *, fire_immediately: bool = True, **config_fields: Any) -> dict[str, Any]:
    config_fields["enabled"] = bool(enabled)
    cfg = save_restock_config(config_fields)
    if cfg["enabled"] and fire_immediately:
        _WAKE.set()
    return cfg


def _worker_loop() -> None:
    while not _STOP.is_set():
        cfg = load_restock_config()
        if cfg["enabled"]:
            run_restock_cycle()
            state = _load_state()
            wait_sec = cfg["order_poll_interval_sec"] if state.get("current_order") else cfg["monitor_interval_sec"]
            _WAKE.wait(wait_sec)
        else:
            _WAKE.wait(5)
        _WAKE.clear()


def ensure_restock_monitor_started() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER and _WORKER.is_alive():
            return
        _STOP.clear()
        _WORKER = threading.Thread(target=_worker_loop, name="sogou-restock", daemon=True)
        _WORKER.start()


def stop_restock_monitor() -> None:
    _STOP.set()
    _WAKE.set()


def list_restock_orders(limit: int = 20) -> list[dict[str, Any]]:
    rows = _read_json(ORDERS_PATH, [])
    if not isinstance(rows, list):
        return []
    return [_safe_order(item) for item in rows[-max(1, min(200, int(limit or 20))):] if isinstance(item, dict)]
