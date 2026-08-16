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
from core.bugteam_client import BugTeamClient, BugTeamError
from core.sogouedu_client import SogouEduClient, SogouEduError
from core.sub2api_pool_push import (
    build_pool_account_from_codex_json,
    normalize_upload_json_to_codex_entries,
    push_prepared_accounts_to_pool,
)
from core.quota_forecast import collect_quota_snapshot, update_forecast

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
_PARTIAL_FINALIZE_WAIT_SEC = 300
_PARTIAL_FINALIZE_RETRY_SEC = 30

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "monitor_group_id": int(getattr(_cfg, "SUB2API_POOL_GROUP_ID", 8) or 8),
    "push_group_id": int(getattr(_cfg, "SUB2API_POOL_GROUP_ID", 8) or 8),
    "product": "oauth_7d",
    "min_healthy": 5,
    "target_healthy": 10,
    "max_purchase_per_order": 5,
    "provider_priority": ["sogou", "bugteam"],
    "bugteam_product": str(getattr(_cfg, "BUGTEAM_PRODUCT", "team_1h") or "team_1h"),
    "partial_retry_limit": 2,
    "monitor_interval_sec": 60,
    "order_poll_interval_sec": 3,
    "recovery_poll_interval_sec": 30,
    "concurrency": int(getattr(_cfg, "SUB2API_POOL_CONCURRENCY", 50) or 50),
    "priority": int(getattr(_cfg, "SUB2API_POOL_PRIORITY", 1) or 1),
    "load_factor": int(getattr(_cfg, "SUB2API_POOL_LOAD_FACTOR", 10) or 10),
    "rate_multiplier": float(getattr(_cfg, "SUB2API_POOL_RATE_MULTIPLIER", 1.0) or 1.0),
    "auto_pause_on_expired": bool(getattr(_cfg, "SUB2API_POOL_AUTO_PAUSE_ON_EXPIRED", True)),
    "model_whitelist": [],
    # Exactly one replenishment trigger is active at a time.  Keep the old
    # forecast_enabled field below as a compatibility mirror for old state.
    "trigger_mode": "inventory",
    # Quota forecasting is collected when disabled so operators can compare
    # it with the provider dashboard before enabling forecast-triggered orders.
    "forecast_enabled": False,
    "forecast_interrupt_minutes": 20,
    "forecast_target_minutes": 25,
    "forecast_rate_window_minutes": 10,
    "forecast_min_samples": 3,
    "forecast_safety_factor": 1.2,
    "forecast_fallback_quantity": 5,
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
    defaults["bugteam_product"] = str(getattr(_cfg, "BUGTEAM_PRODUCT", defaults["bugteam_product"]) or defaults["bugteam_product"])
    return defaults


def normalize_restock_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = _config_defaults()
    if isinstance(raw, dict):
        cfg.update({key: raw[key] for key in _CONFIG_KEYS if key in raw})
    raw_mode = raw.get("trigger_mode") if isinstance(raw, dict) else None
    if raw_mode in (None, ""):
        # Existing installations used forecast_enabled before trigger_mode
        # existed. Infer that behavior once; new configs default to inventory.
        raw_mode = "forecast" if isinstance(raw, dict) and bool(raw.get("forecast_enabled")) else "inventory"
    cfg["trigger_mode"] = str(raw_mode).strip().lower()
    if cfg["trigger_mode"] not in {"inventory", "forecast"}:
        cfg["trigger_mode"] = "inventory"
    cfg["enabled"] = bool(cfg.get("enabled", False))
    for key in ("monitor_group_id", "push_group_id", "min_healthy", "target_healthy", "max_purchase_per_order", "partial_retry_limit", "concurrency", "priority", "load_factor"):
        try:
            cfg[key] = int(cfg.get(key) or 0)
        except (TypeError, ValueError):
            cfg[key] = int(_config_defaults().get(key, 1))
    cfg["monitor_group_id"] = max(1, cfg["monitor_group_id"])
    cfg["push_group_id"] = max(1, cfg["push_group_id"])
    cfg["min_healthy"] = max(0, cfg["min_healthy"])
    cfg["target_healthy"] = max(cfg["min_healthy"], cfg["target_healthy"])
    cfg["max_purchase_per_order"] = max(1, cfg["max_purchase_per_order"])
    cfg["partial_retry_limit"] = max(0, min(5, cfg["partial_retry_limit"]))
    try:
        cfg["forecast_fallback_quantity"] = max(1, min(1000, int(cfg.get("forecast_fallback_quantity") or 5)))
    except (TypeError, ValueError):
        cfg["forecast_fallback_quantity"] = 5
    for key in ("monitor_interval_sec", "order_poll_interval_sec", "recovery_poll_interval_sec"):
        try:
            cfg[key] = max(1, int(cfg.get(key) or 1))
        except (TypeError, ValueError):
            cfg[key] = 1
    # The mode is authoritative. The mirror keeps old callers readable without
    # allowing the two trigger paths to be active at the same time.
    cfg["forecast_enabled"] = cfg["trigger_mode"] == "forecast"
    try:
        cfg["forecast_interrupt_minutes"] = max(0, min(24 * 60, int(cfg.get("forecast_interrupt_minutes") or 0)))
    except (TypeError, ValueError):
        cfg["forecast_interrupt_minutes"] = 20
    try:
        cfg["forecast_target_minutes"] = max(cfg["forecast_interrupt_minutes"], min(24 * 60, int(cfg.get("forecast_target_minutes") or 25)))
    except (TypeError, ValueError):
        cfg["forecast_target_minutes"] = max(cfg["forecast_interrupt_minutes"], 25)
    try:
        cfg["forecast_rate_window_minutes"] = max(1, min(24 * 60, int(cfg.get("forecast_rate_window_minutes") or 10)))
    except (TypeError, ValueError):
        cfg["forecast_rate_window_minutes"] = 10
    try:
        cfg["forecast_min_samples"] = max(3, min(1000, int(cfg.get("forecast_min_samples") or 3)))
    except (TypeError, ValueError):
        cfg["forecast_min_samples"] = 3
    try:
        cfg["forecast_safety_factor"] = max(1.0, min(5.0, float(cfg.get("forecast_safety_factor") or 1.0)))
    except (TypeError, ValueError):
        cfg["forecast_safety_factor"] = 1.2
    try:
        cfg["rate_multiplier"] = max(0.0, float(cfg.get("rate_multiplier") or 0.0))
    except (TypeError, ValueError):
        cfg["rate_multiplier"] = 1.0
    cfg["auto_pause_on_expired"] = bool(cfg.get("auto_pause_on_expired", True))
    cfg["product"] = str(cfg.get("product") or "oauth_7d").strip()
    if cfg["product"] not in {"oauth_7d", "oauth_30d"}:
        cfg["product"] = "oauth_7d"
    cfg["bugteam_product"] = str(cfg.get("bugteam_product") or getattr(_cfg, "BUGTEAM_PRODUCT", "team_1h") or "team_1h").strip()
    priority = cfg.get("provider_priority")
    if not isinstance(priority, list):
        priority = ["sogou", "bugteam"]
    priority = [str(item).strip().lower() for item in priority if str(item).strip().lower() in {"sogou", "bugteam"}]
    cfg["provider_priority"] = list(dict.fromkeys(priority)) or ["sogou", "bugteam"]
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
    state.setdefault("quota_forecast", {})
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


def next_replenishing_state(healthy: int, cfg: dict[str, Any], current: bool) -> bool:
    if int(healthy) < int(cfg["min_healthy"]):
        return True
    if int(healthy) >= int(cfg["target_healthy"]):
        return False
    return bool(current)


def calculate_purchase_quantity(
    healthy: int,
    cfg: dict[str, Any],
    *,
    replenishing: bool,
    forecast_trigger: bool = False,
    forecast_fallback: bool = False,
    quota_forecast: dict[str, Any] | None = None,
) -> int:
    if not replenishing:
        return 0
    if cfg.get("trigger_mode") == "forecast":
        # Forecast mode is independent of inventory thresholds. Once the
        # remaining-quota ETA crosses the lead time, replenish only the
        # calculated shortfall to the target runway.
        if not forecast_trigger and not forecast_fallback:
            return 0
        if forecast_fallback:
            # A sudden availability drop or an empty pool has no reliable ETA.
            # Use the explicit emergency quantity, still respecting the order cap.
            return min(
                max(1, int(cfg.get("forecast_fallback_quantity") or 5)),
                max(1, int(cfg["max_purchase_per_order"])),
            )
        forecast = quota_forecast if isinstance(quota_forecast, dict) else {}
        target_minutes = max(
            float(cfg.get("forecast_interrupt_minutes") or 0),
            float(cfg.get("forecast_target_minutes") or 25),
        )
        required = 0
        for window in (forecast.get("windows") or {}).values():
            if not isinstance(window, dict):
                continue
            planned_rate = float(window.get("planned_rate_units_per_min") or 0.0)
            remaining = max(0.0, float(window.get("remaining_units") or 0.0))
            capacity = max(1e-12, float(window.get("capacity_units_per_account") or 1.0))
            shortfall = max(0.0, target_minutes * planned_rate - remaining)
            required = max(required, int((shortfall / capacity) + 0.999999))
        if required <= 0:
            required = 1
        return min(required, max(1, int(cfg["max_purchase_per_order"])))
    gap = max(0, int(cfg["target_healthy"]) - int(healthy))
    if forecast_trigger and gap <= 0:
        return max(1, int(cfg["max_purchase_per_order"]))
    return min(gap, max(1, int(cfg["max_purchase_per_order"])))


def _provider_names(cfg: dict[str, Any]) -> list[str]:
    values = cfg.get("provider_priority") if isinstance(cfg, dict) else None
    if not isinstance(values, list):
        values = ["sogou", "bugteam"]
    names = [str(value).strip().lower() for value in values if str(value).strip().lower() in {"sogou", "bugteam"}]
    return list(dict.fromkeys(names)) or ["sogou", "bugteam"]


def _provider_configured(provider: str, *, injected_client: Any = None) -> bool:
    provider = str(provider or "").strip().lower()
    if provider == "sogou":
        return injected_client is not None or bool(
            getattr(_cfg, "SOGOUEDU_USERNAME", "") and getattr(_cfg, "SOGOUEDU_PASSWORD", "")
        )
    if provider == "bugteam":
        return bool(getattr(_cfg, "BUGTEAM_API_TOKEN", ""))
    return False


def _new_provider_client(provider: str, *, injected_client: Any = None) -> Any:
    provider = str(provider or "").strip().lower()
    if provider == "sogou":
        return injected_client or SogouEduClient()
    if provider == "bugteam":
        return BugTeamClient()
    raise ValueError(f"未知补池供应商: {provider}")


def _provider_product(provider: str, cfg: dict[str, Any], order: dict[str, Any] | None = None) -> str:
    if isinstance(order, dict) and order.get("product"):
        return str(order["product"]).strip()
    return str(cfg.get("product") if provider == "sogou" else cfg.get("bugteam_product") or "team_1h").strip()


def _provider_source(provider: str) -> str:
    return "bugteam_auto_restock" if str(provider).strip().lower() == "bugteam" else "sogouedu_auto_restock"


def _provider_order_field(provider: str) -> str:
    return "bugteam_order_id" if str(provider).strip().lower() == "bugteam" else "sogou_order_id"


def _provider_order_key(provider: str) -> str:
    return "bugteam-restock" if str(provider).strip().lower() == "bugteam" else "sogou-restock"


def _inventory_available(body: Any) -> int | None:
    value = _value(body, "available", "available_quantity", "stock", "quantity")
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _provider_error(exc: Exception) -> tuple[str, int | None]:
    return str(exc), getattr(exc, "status_code", None)


def _safe_order(order: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(order, dict):
        return None
    return {key: value for key, value in order.items() if key not in {"payload", "pending_payload", "credentials"}}


def _upsert_order_history(
    order: dict[str, Any] | None,
    *,
    status: str | None = None,
    remote_status: str | None = None,
    **fields: Any,
) -> None:
    """Persist a sanitized order snapshot without changing order processing state."""
    if not isinstance(order, dict):
        return
    snapshot = dict(order)
    if status not in (None, ""):
        snapshot["status"] = str(status)
    if remote_status not in (None, ""):
        order["remote_status"] = str(remote_status)
        snapshot["remote_status"] = str(remote_status)
    snapshot.update(fields)
    snapshot["updated_at"] = _now()
    safe = _safe_order(snapshot)
    if not safe:
        return
    order_id = str(safe.get("order_id") or "").strip()
    if not order_id:
        return
    provider = str(safe.get("provider") or "").strip().lower()
    rows = _read_json(ORDERS_PATH, [])
    if not isinstance(rows, list):
        rows = []
    match_index = None
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        if not isinstance(row, dict) or str(row.get("order_id") or "").strip() != order_id:
            continue
        row_provider = str(row.get("provider") or "").strip().lower()
        if row_provider and provider and row_provider != provider:
            continue
        match_index = index
        break
    if match_index is None:
        rows.append(safe)
    else:
        merged = dict(rows[match_index])
        merged.update(safe)
        rows[match_index] = merged
    _write_json(ORDERS_PATH, rows)


def get_restock_status() -> dict[str, Any]:
    cfg = load_restock_config()
    state = _load_state()
    return {
        "config": cfg,
        "running": bool(_RUNNING),
        "current_order": _safe_order(state.get("current_order")),
        "last_run": state.get("last_run"),
        "replenishing": bool(state.get("replenishing")),
        "inventory": state.get("inventory"),
        "quota_forecast": (state.get("quota_forecast") or {}).get("forecast"),
        "last_recovery_scan_at": state.get("last_recovery_scan_at"),
        "recovery_cursor": state.get("recovery_cursor"),
        "credentials_configured": bool(getattr(_cfg, "SOGOUEDU_USERNAME", "") and getattr(_cfg, "SOGOUEDU_PASSWORD", "")),
        "providers": {
            "sogou": bool(getattr(_cfg, "SOGOUEDU_USERNAME", "") and getattr(_cfg, "SOGOUEDU_PASSWORD", "")),
            "bugteam": bool(getattr(_cfg, "BUGTEAM_API_TOKEN", "")),
        },
    }


def test_provider_connections() -> dict[str, Any]:
    """测试已配置供应商连接，只返回脱敏的连通性和商品摘要。"""
    cfg = load_restock_config()
    output: dict[str, Any] = {}
    for provider in _provider_names(cfg):
        if not _provider_configured(provider):
            output[provider] = {"configured": False, "ok": False, "error": "未配置供应商凭据"}
            continue
        try:
            client = _new_provider_client(provider)
            if provider == "sogou":
                client.login()
                output[provider] = {"configured": True, "ok": True, "message": "SogouEdu 登录成功"}
            else:
                dashboard = client.dashboard()
                products = dashboard.get("products") if isinstance(dashboard, dict) else []
                safe_products = [
                    {"code": item.get("code"), "name": item.get("name")}
                    for item in products
                    if isinstance(item, dict) and item.get("code")
                ]
                output[provider] = {
                    "configured": True,
                    "ok": True,
                    "message": "BugTeam 连接成功",
                    "products": safe_products,
                }
        except (SogouEduError, BugTeamError) as exc:
            output[provider] = {
                "configured": True,
                "ok": False,
                "error": str(exc),
                "status_code": getattr(exc, "status_code", None),
            }
        except Exception as exc:
            output[provider] = {"configured": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return output


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
    return str(_order_value(body, "status", "state", "order_status", "orderStatus") or "").strip().lower()


def _order_value(body: Any, *keys: str) -> Any:
    data = _extract_data(body)
    candidates = [data]
    if body is not data:
        candidates.append(body)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in keys:
            if candidate.get(key) not in (None, ""):
                return candidate.get(key)
        nested = candidate.get("order") or candidate.get("pickup_order")
        if isinstance(nested, dict):
            for key in keys:
                if nested.get(key) not in (None, ""):
                    return nested.get(key)
    return None


def _delivery_payload(body: Any) -> Any:
    """解开取货接口包装层，返回真正包含 accounts 的交付载荷。"""
    value = body
    seen: set[int] = set()
    for _ in range(6):
        if isinstance(value, list):
            return value
        if not isinstance(value, dict):
            return value
        if isinstance(value.get("accounts"), list):
            return value
        marker = id(value)
        if marker in seen:
            break
        seen.add(marker)
        nested = next(
            (
                value.get(key)
                for key in ("data", "payload", "result")
                if isinstance(value.get(key), (dict, list))
            ),
            None,
        )
        if nested is None:
            break
        value = nested
    return value


def _recovery_page_cursor(body: Any) -> Any:
    """读取供应商分页游标，仅用于展示/审计，不作为新记录扫描起点。"""
    data = _extract_data(body)
    if isinstance(data, dict):
        return data.get("next_before_id") or data.get("nextBeforeId")
    return None


def _recovery_key(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("recovery_id") or "").strip()


def _merge_recovery_items(current: list[dict], stored: list[dict]) -> list[dict]:
    """合并最新 API 页和本地待处理记录，API 的字段优先。"""
    merged: dict[str, dict] = {}
    for row in stored + current:
        key = _recovery_key(row)
        if not key:
            continue
        old = merged.get(key) or {}
        merged[key] = {**old, **row}
    return list(merged.values())


def _pool_extra(account: dict[str, Any]) -> dict[str, Any]:
    return account.get("extra") if isinstance(account.get("extra"), dict) else {}


def _match_provider_account(accounts: list[dict], recovery: dict[str, Any], provider: str = "sogou") -> dict[str, Any] | None:
    recovery_pool_id = str(recovery.get("pool_id") or recovery.get("account_id") or "").strip()
    email = str(recovery.get("email") or recovery.get("username") or "").strip().lower()
    for account in accounts:
        extra = _pool_extra(account)
        if extra.get("import_source") != _provider_source(provider):
            continue
        if recovery_pool_id and str(account.get("id") or account.get("account_id") or "") == recovery_pool_id:
            return account
        creds = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
        account_email = str(account.get("email") or creds.get("email") or account.get("name") or "").strip().lower()
        if email and email == account_email:
            return account
    return None


def _match_sogou_account(accounts: list[dict], recovery: dict[str, Any]) -> dict[str, Any] | None:
    """兼容旧测试和外部调用的 Sogou 修复账号匹配入口。"""
    return _match_provider_account(accounts, recovery, "sogou")


def _order_items(body: Any) -> list[dict[str, Any]]:
    data = _extract_data(body)
    candidates = [data]
    if body is not data:
        candidates.append(body)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        nested = candidate.get("order") or candidate.get("pickup_order")
        for value in (nested, candidate):
            if isinstance(value, dict) and isinstance(value.get("items"), list):
                return [item for item in value["items"] if isinstance(item, dict)]
    return []


def _partial_order_is_exhausted(body: Any) -> bool:
    """判断部分结算后是否所有已预留账号都已失败且无可交付项。"""
    items = _order_items(body)
    if not items:
        return False
    failure_values = {"failed", "error", "deactivated", "refunded", "cancelled", "canceled"}
    for item in items:
        health = str(item.get("health_status") or "").strip().lower()
        reauthorization = str(item.get("reauthorization_status") or "").strip().lower()
        replacement = str(item.get("replacement_status") or "").strip().lower()
        if replacement in failure_values:
            continue
        if not replacement and health in failure_values and reauthorization in failure_values:
            continue
        if replacement not in failure_values:
            return False
    return True


def _recovery_order_email(recovery: dict[str, Any], order_body: Any) -> str:
    recovery_id = str(recovery.get("id") or recovery.get("recovery_id") or "").strip()
    inventory_id = str(
        recovery.get("inventory_id")
        or recovery.get("inventory_account_id")
        or ""
    ).strip()
    for item in _order_items(order_body):
        item_recovery_id = str(item.get("recovery_id") or "").strip()
        item_inventory_id = str(
            item.get("inventory_account_id") or item.get("inventory_id") or ""
        ).strip()
        if (
            recovery_id and item_recovery_id == recovery_id
        ) or (
            inventory_id and item_inventory_id == inventory_id
        ):
            return str(item.get("email") or item.get("username") or "").strip().lower()
    return ""


def _payload_email(payload: Any, *, recovery_id: Any = "") -> str:
    entries = normalize_upload_json_to_codex_entries(
        payload,
        filename=f"sogou-recovery-{recovery_id or 'unknown'}.json",
    )
    if not entries:
        return ""
    return str(entries[0].get("email") or "").strip().lower()


def _build_prepared(
    payload: Any,
    cfg: dict[str, Any],
    *,
    order_id: str = "",
    provider: str = "sogou",
    recreated: bool = False,
    replacement_of_pool_id: Any = None,
) -> list[tuple[str, dict]]:
    provider = str(provider or "sogou").strip().lower()
    prefix = "bugteam" if provider == "bugteam" else "sogou"
    provider_label = "BugTeam" if provider == "bugteam" else "Sogou"
    entries = normalize_upload_json_to_codex_entries(payload, filename=f"{prefix}-{order_id or 'recovery'}.json")
    prepared: list[tuple[str, dict]] = []
    for index, entry in enumerate(entries):
        label = str(entry.get("email") or f"{prefix}-{order_id or 'recovery'}#{index}")
        extra = {
            "import_source": _provider_source(provider),
            _provider_order_field(provider): order_id,
            f"{provider}_product": _provider_product(provider, cfg),
        }
        if recreated:
            extra["recreated"] = True
            if replacement_of_pool_id not in (None, ""):
                extra["replacement_of_pool_id"] = replacement_of_pool_id
        account = build_pool_account_from_codex_json(
            entry,
            filename=f"{prefix}-{order_id or 'recovery'}.json",
            group_id=cfg["push_group_id"],
            concurrency=cfg["concurrency"],
            priority=cfg["priority"],
            load_factor=cfg["load_factor"],
            rate_multiplier=cfg["rate_multiplier"],
            model_whitelist=cfg["model_whitelist"],
            extra_patch=extra,
            auto_pause_on_expired=cfg["auto_pause_on_expired"],
        )
        fallback_name = f"{prefix}-{order_id or 'recovery'}#{index}"
        account["name"] = f"{provider_label} | {account.get('name') or entry.get('email') or fallback_name}"
        prepared.append((label, account))
    return prepared


def _schedule_followup_order(
    order: dict[str, Any],
    cfg: dict[str, Any],
    state: dict[str, Any],
    *,
    remaining: int,
    reason: str,
) -> dict[str, Any] | None:
    """为少交付/终态失败订单安排同供应商重试或下一个供应商。

    ``provider`` 缺失表示升级前的旧状态，保留原有清理行为，避免改变正在
    处理的历史 Sogou 订单。新建订单始终写入 provider 字段。
    """
    if not order.get("provider"):
        return None
    remaining = max(0, int(remaining or 0))
    if remaining <= 0:
        return None
    previous_order_id = str(order.get("order_id") or "")
    provider = str(order.get("provider") or "sogou").strip().lower()
    providers = _provider_names(cfg)
    try:
        provider_index = providers.index(provider)
    except ValueError:
        provider_index = max(0, int(order.get("provider_index") or 0))
    retry_count = max(0, int(order.get("provider_retry_count") or 0))
    retry_limit = max(0, int(cfg.get("partial_retry_limit") or 0))
    if retry_count < retry_limit:
        next_provider = provider
        next_retry_count = retry_count + 1
        next_index = provider_index
        action = "provider_retry_scheduled"
    else:
        next_index = provider_index + 1
        if next_index >= len(providers):
            _upsert_order_history(
                order,
                status="order_failed",
                remote_status=order.get("remote_status") or order.get("status"),
                transition_reason=reason,
                remaining_quantity=remaining,
                finished_at=_now(),
            )
            state["current_order"] = None
            _save_state(state)
            return {
                "handled": True,
                "action": "order_failed",
                "order_id": str(order.get("order_id") or ""),
                "status": "provider_exhausted",
                "remaining": remaining,
                "reason": reason,
            }
        next_provider = providers[next_index]
        next_retry_count = 0
        action = "provider_fallback_scheduled"

    _upsert_order_history(
        order,
        status=action,
        remote_status=order.get("remote_status") or order.get("status"),
        transition_reason=reason,
        remaining_quantity=remaining,
    )
    order["provider"] = next_provider
    order["provider_index"] = next_index
    order["provider_retry_count"] = next_retry_count
    order["product"] = _provider_product(next_provider, cfg)
    order["quantity"] = remaining
    order["remaining_quantity"] = remaining
    order["last_transition_reason"] = reason
    order["updated_at"] = _now()
    for key in ("order_id", "payload", "last_polled_at", "partial_ready_since", "partial_finalize_last_attempt_at", "partial_finalized_at"):
        order.pop(key, None)
    order["status"] = "creating"
    order["idempotency_key"] = f"{_provider_order_key(next_provider)}-{uuid.uuid4().hex}"
    state["current_order"] = order
    _save_state(state)
    return {
        "handled": True,
        "action": action,
        "provider": next_provider,
        "order_id": previous_order_id,
        "remaining": remaining,
        "provider_retry_count": next_retry_count,
        "reason": reason,
    }


def _process_current_order(client: Any, cfg: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    order = state.get("current_order")
    if not isinstance(order, dict):
        return {"handled": False}
    snapshot = order.get("config_snapshot") if isinstance(order.get("config_snapshot"), dict) else {}
    order_cfg = normalize_restock_config({**cfg, **snapshot})
    provider = str(order.get("provider") or "sogou").strip().lower()
    order_id = str(order.get("order_id") or "").strip()
    if not order_id:
        key = str(order.get("idempotency_key") or "").strip()
        if not key:
            key = f"{_provider_order_key(provider)}-{uuid.uuid4().hex}"
            order["idempotency_key"] = key
            state["current_order"] = order
            _save_state(state)
        product = _provider_product(provider, order_cfg, order)
        response = client.create_order(product, int(order["quantity"]), idempotency_key=key)
        order_id = _order_id(response)
        if not order_id:
            order["last_error"] = "订单响应缺少 order_id"
            _save_state(state)
            raise RuntimeError(order["last_error"])
        order["order_id"] = order_id
        order["provider"] = provider
        order["product"] = product
        order["status"] = _order_status(response) or "pending"
        order["updated_at"] = _now()
        _upsert_order_history(order, remote_status=order["status"])
        _save_state(state)
        return {"handled": True, "action": "ordered", "order_id": order_id, "provider": provider}

    if not order.get("payload"):
        last_polled = _parse_time(order.get("last_polled_at"))
        if last_polled and time.time() - last_polled < cfg["order_poll_interval_sec"]:
            return {"handled": True, "action": "waiting", "order_id": order_id, "status": order.get("status") or "pending"}
        response = client.order_status(order_id, status_url=order.get("status_url"))
        order["status"] = _order_status(response) or order.get("status") or "pending"
        order["last_polled_at"] = _now()
        status = order["status"]
        try:
            reserved = max(0, int(_order_value(response, "reserved", "reserved_count", "delivered_quantity", "delivered") or 0))
        except (TypeError, ValueError):
            reserved = 0
        order["reserved"] = reserved
        _upsert_order_history(order, remote_status=status)
        if status in {"failed", "cancelled", "canceled", "refunded", "error"}:
            order["last_error"] = str(_value(response, "message", "error") or status)
            followup = _schedule_followup_order(
                order,
                order_cfg,
                state,
                remaining=max(1, int(order.get("quantity") or 0)),
                reason=f"{provider}:{status}",
            )
            if followup:
                return followup
            _upsert_order_history(
                order,
                status="order_failed",
                remote_status=status,
                finished_at=_now(),
            )
            state["current_order"] = None
            _save_state(state)
            return {"handled": True, "action": "order_failed", "order_id": order_id}
        if reserved <= 0:
            order.pop("partial_ready_since", None)
            order.pop("partial_finalize_last_attempt_at", None)
        if provider == "sogou" and status == "ready_partial" and reserved > 0:
            partial_since = _parse_time(order.get("partial_ready_since"))
            if partial_since is None:
                order["partial_ready_since"] = _now()
                partial_since = _parse_time(order["partial_ready_since"])
            elapsed = max(0.0, time.time() - (partial_since or time.time()))
            last_attempt = _parse_time(order.get("partial_finalize_last_attempt_at"))
            retry_ready = last_attempt is None or time.time() - last_attempt >= _PARTIAL_FINALIZE_RETRY_SEC
            if elapsed >= _PARTIAL_FINALIZE_WAIT_SEC and retry_ready:
                order["partial_finalize_last_attempt_at"] = _now()
                order["updated_at"] = _now()
                _save_state(state)
                finalized = client.finalize_order(order_id)
                order["status"] = _order_status(finalized) or "finalizing"
                order["partial_finalized_at"] = _now()
                order.pop("partial_ready_since", None)
                order.pop("partial_finalize_last_attempt_at", None)
                order.pop("last_polled_at", None)
                order["updated_at"] = _now()
                _upsert_order_history(
                    order,
                    status="partial_finalized",
                    remote_status=order["status"],
                    reserved=reserved,
                )
                _save_state(state)
                return {
                    "handled": True,
                    "action": "partial_finalized",
                    "order_id": order_id,
                    "reserved": reserved,
                    "status": order["status"],
                }
        if (
            order.get("partial_finalized_at")
            and status == "partial"
            and _partial_order_is_exhausted(response)
        ):
            followup = _schedule_followup_order(
                order,
                order_cfg,
                state,
                remaining=max(1, int(order.get("quantity") or 0)),
                reason=f"{provider}:partial_exhausted",
            )
            if followup:
                return followup
            _upsert_order_history(
                order,
                status="order_failed",
                remote_status=status,
                reserved=reserved,
                transition_reason="partial_exhausted",
                finished_at=_now(),
            )
            state["current_order"] = None
            _save_state(state)
            return {
                "handled": True,
                "action": "order_failed",
                "order_id": order_id,
                "status": "partial_exhausted",
                "reserved": reserved,
                "reason": "部分结算后无可交付账号",
            }
        # Sogou 部分结算完成后仍返回 partial；只要响应带有预留账号，
        # 该状态也已经可以取货。否则会在 partial 状态下无限等待。
        partial_settled_ready = bool(
            order.get("partial_finalized_at")
            and status == "partial"
            and reserved > 0
            and _order_items(response)
        )
        if status not in {"ready", "completed", "success", "available", "fulfilled", "done"} and not partial_settled_ready:
            order["updated_at"] = _now()
            _upsert_order_history(order, remote_status=status, reserved=reserved)
            _save_state(state)
            waiting = {
                "handled": True,
                "action": "waiting",
                "order_id": order_id,
                "status": status,
                "reserved": reserved,
            }
            if order.get("partial_ready_since"):
                waiting["partial_ready_since"] = order["partial_ready_since"]
            return waiting
        order.pop("partial_ready_since", None)
        order.pop("partial_finalize_last_attempt_at", None)
        response = client.take_order(order_id, take_url=order.get("take_url"))
        order["payload"] = _delivery_payload(response)
        order["status"] = "taken"
        order["updated_at"] = _now()
        _upsert_order_history(order, status="taken", remote_status=status, reserved=reserved)
        _save_state(state)

    payload = _delivery_payload(order.get("payload"))
    if payload is not order.get("payload"):
        order["payload"] = payload
    prepared = _build_prepared(payload, order_cfg, order_id=order_id, provider=provider)
    if not prepared:
        order["last_error"] = "取货响应未包含有效 OAuth 账号"
        order.pop("payload", None)
        order.pop("last_polled_at", None)
        order["status"] = "ready"
        order["updated_at"] = _now()
        _upsert_order_history(
            order,
            status="push_waiting",
            remote_status=order.get("remote_status") or order.get("status"),
        )
        _save_state(state)
        return {"handled": True, "action": "push_waiting", "order_id": order_id}
    result = push_prepared_accounts_to_pool(prepared)
    pushed_success = max(0, min(len(prepared), int(result.get("success", 0) or 0)))
    pushed_failed = max(0, int(result.get("failed", 0) or 0))
    order["last_push"] = {"success": pushed_success, "failed": pushed_failed}
    order["updated_at"] = _now()
    if pushed_failed:
        _upsert_order_history(
            order,
            status="push_retry",
            remote_status=order.get("remote_status") or order.get("status"),
            delivered_quantity=pushed_success,
        )
        _save_state(state)
        return {"handled": True, "action": "push_retry", "order_id": order_id, "result": order["last_push"]}
    expected = max(1, int(order.get("quantity") or len(prepared)))
    if order.get("provider") and pushed_success < expected:
        _upsert_order_history(
            order,
            status="partial_pushed",
            remote_status=order.get("remote_status") or order.get("status"),
            delivered_quantity=pushed_success,
        )
        followup = _schedule_followup_order(
            order,
            order_cfg,
            state,
            remaining=expected - pushed_success,
            reason=f"{provider}:partial_delivery:{pushed_success}/{expected}",
        )
        if followup:
            followup["order_id"] = order_id
            followup["delivered"] = pushed_success
            return followup
    _upsert_order_history(
        order,
        status="pushed",
        remote_status=order.get("remote_status") or order.get("status"),
        delivered_quantity=pushed_success,
        finished_at=_now(),
    )
    state["current_order"] = None
    _save_state(state)
    return {"handled": True, "action": "pushed", "order_id": order_id, "result": order["last_push"]}


def _process_recoveries(
    client: Any,
    cfg: dict[str, Any],
    state: dict[str, Any],
    accounts: list[dict],
    *,
    provider: str = "sogou",
) -> dict[str, Any]:
    provider = str(provider or "sogou").strip().lower()
    now = time.time()
    scan_key = "last_recovery_scan_at" if provider == "sogou" else f"last_recovery_scan_at_{provider}"
    cursor_key = "recovery_cursor" if provider == "sogou" else f"recovery_cursor_{provider}"
    last = _parse_time(state.get(scan_key))
    if last and now - last < cfg["recovery_poll_interval_sec"]:
        return {"scanned": False, "repaired": 0, "recreated": 0}
    # before_id 是向更旧记录翻页的游标，不能作为下一轮轮询的起点，否则新
    # 产生的补发记录会永远落在游标之后。每轮从最新页开始，并合并本地失败
    # 记录，保证 claimable 记录即使暂时不在最新页也能重试。
    body = client.list_recoveries(before_id=None, limit=100)
    latest_items = _extract_items(body, "items", "recoveries", "records")
    stored_items = _read_json(RECOVERIES_PATH, [])
    if not isinstance(stored_items, list):
        stored_items = []
    for row in latest_items:
        row["provider"] = provider
    provider_items: list[dict[str, Any]] = []
    other_provider_items: list[dict[str, Any]] = []
    for row in stored_items:
        if not isinstance(row, dict):
            continue
        row_provider = str(row.get("provider") or "sogou").strip().lower()
        if row_provider == provider:
            provider_items.append(row)
        else:
            other_provider_items.append(row)
    items = _merge_recovery_items(latest_items, provider_items)
    repaired = recreated = 0
    newest = _recovery_page_cursor(body)
    source_orders: dict[str, Any] = {}
    for recovery in items:
        rid = recovery.get("id") or recovery.get("recovery_id")
        if rid in (None, ""):
            continue
        recovery.setdefault("provider", provider)
        status = str(recovery.get("status") or recovery.get("state") or recovery.get("delivery_status") or "").strip().lower()
        if status in {"recovered", "claimed", "completed", "success", "repaired"}:
            continue
        if status not in {"claimable", "ready", "available"} and not recovery.get("claim_url"):
            # delivered/pending 等状态没有可领取票据，不应反复调用 claim。
            recovery["skip_reason"] = f"status_not_claimable:{status or 'unknown'}"
            continue
        try:
            source_order_id = str(
                recovery.get("source_order_id") or recovery.get("order_id") or ""
            ).strip()
            if not recovery.get("email") and source_order_id:
                if source_order_id not in source_orders:
                    source_orders[source_order_id] = client.order_status(source_order_id)
                resolved_email = _recovery_order_email(recovery, source_orders[source_order_id])
                if resolved_email:
                    recovery["email"] = resolved_email
                    recovery["matched_by"] = "source_order"
            matched = _match_provider_account(accounts, recovery, provider)
            claim = client.claim_recovery(
                rid,
                claim_url=recovery.get("claim_url"),
                ticket=recovery.get("ticket") or recovery.get("claim_ticket"),
            )
            payload = _delivery_payload(claim)
            if not matched:
                claimed_email = _payload_email(payload, recovery_id=rid)
                if claimed_email:
                    recovery["email"] = claimed_email
                    recovery["matched_by"] = "claim_payload"
                    matched = _match_provider_account(accounts, recovery, provider)
            if matched:
                prefix = "bugteam" if provider == "bugteam" else "sogou"
                entries = normalize_upload_json_to_codex_entries(payload, filename=f"{prefix}-recovery-{rid}.json")
                if not entries:
                    raise ValueError("补发认领响应未包含有效 OAuth 账号")
                account_payload = build_pool_account_from_codex_json(
                    entries[0],
                    filename=f"{prefix}-recovery-{rid}.json",
                    model_whitelist=cfg["model_whitelist"],
                )
                pool_id = matched.get("id") or matched.get("account_id")
                _pool_monitor._update_pool_credentials(int(pool_id), account_payload)
                repaired += 1
            else:
                replacement = recovery.get("pool_id") or recovery.get("account_id")
                prepared = _build_prepared(
                    payload,
                    cfg,
                    order_id=f"recovery-{rid}",
                    provider=provider,
                    recreated=True,
                    replacement_of_pool_id=replacement,
                )
                if not prepared:
                    raise ValueError("补发认领响应未包含有效 OAuth 账号")
                result = push_prepared_accounts_to_pool(prepared)
                if result.get("failed", 0):
                    raise RuntimeError(f"补发推池失败: {result.get('failed')} 个账号")
                recreated += result.get("success", 0)
            recovery["processed_at"] = _now()
            recovery["result"] = "repaired" if matched else "recreated"
        except Exception as exc:
            recovery["last_error"] = f"{type(exc).__name__}: {exc}"
        _write_json(RECOVERIES_PATH, other_provider_items + items)
    state[scan_key] = _now()
    state[cursor_key] = newest
    _save_state(state)
    return {"scanned": True, "repaired": repaired, "recreated": recreated}


def run_restock_cycle(*, force: bool = False, client: Any = None) -> dict[str, Any]:
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
        current_order = state.get("current_order") if isinstance(state.get("current_order"), dict) else None
        current_provider = str((current_order or {}).get("provider") or "sogou").strip().lower()
        api = _new_provider_client(current_provider, injected_client=client if current_provider == "sogou" else None)
        order_result = _process_current_order(api, cfg, state)
        if order_result.get("handled"):
            result.update(order_result)
            result["ok"] = True
            return result
        accounts = _pool_monitor.fetch_pool_accounts(group_id=cfg["monitor_group_id"], platform="openai", account_type="oauth")
        recovery_totals: dict[str, Any] = {"scanned": False, "repaired": 0, "recreated": 0}
        recovery_errors: list[dict[str, Any]] = []
        for provider in _provider_names(cfg):
            if not _provider_configured(provider, injected_client=client if provider == "sogou" else None):
                continue
            provider_client = _new_provider_client(provider, injected_client=client if provider == "sogou" else None)
            try:
                recovery_result = _process_recoveries(provider_client, cfg, state, accounts, provider=provider)
            except (SogouEduError, BugTeamError) as exc:
                recovery_errors.append({"provider": provider, "error": str(exc)})
                continue
            recovery_totals["scanned"] = bool(recovery_totals["scanned"] or recovery_result.get("scanned"))
            recovery_totals["repaired"] += int(recovery_result.get("repaired") or 0)
            recovery_totals["recreated"] += int(recovery_result.get("recreated") or 0)
        if recovery_errors:
            recovery_totals["errors"] = recovery_errors
        result["recovery"] = recovery_totals
        active_accounts = _pool_monitor.fetch_pool_accounts(
            group_id=cfg["monitor_group_id"],
            platform="openai",
            account_type="oauth",
            status="active",
        )
        # The API status filter is not sufficient for the inventory count:
        # rate-limited or otherwise unschedulable rows can still be ``active``.
        # Keep the quota forecast on the same currently healthy population.
        healthy_accounts = [account for account in active_accounts if is_healthy_pool_account(account)]
        healthy = len(healthy_accounts)
        # Quota sampling is the first replenishment-decision stage. Every
        # patrol uses the same current account snapshot for ETA and ordering.
        quota_state = state.get("quota_forecast") if isinstance(state.get("quota_forecast"), dict) else {}
        quota_snapshot = collect_quota_snapshot(healthy_accounts)
        quota_state, quota_forecast = update_forecast(
            quota_state,
            quota_snapshot,
            min_samples=cfg["forecast_min_samples"],
            safety_factor=cfg["forecast_safety_factor"],
            rate_window_minutes=cfg["forecast_rate_window_minutes"],
        )
        state["quota_forecast"] = quota_state
        forecast_trigger = bool(
            cfg["trigger_mode"] == "forecast"
            and quota_forecast.get("status") == "ready"
            and quota_forecast.get("eta_minutes") is not None
            and float(quota_forecast["eta_minutes"]) <= float(cfg["forecast_interrupt_minutes"])
        )
        # Forecast mode normally ignores inventory thresholds. A completely
        # empty schedulable OAuth pool is the exception: without an account we
        # cannot collect a quota window, so waiting for an ETA would deadlock
        # replenishment indefinitely.
        removed_accounts = int(quota_forecast.get("removed_accounts") or 0)
        new_accounts = int(quota_forecast.get("new_accounts") or 0)
        previous_account_count = max(0, healthy + removed_accounts - new_accounts)
        drop_threshold = max(2, int(previous_account_count * 0.2 + 0.999999))
        availability_drop = bool(
            removed_accounts >= drop_threshold
            and previous_account_count > 0
        )
        forecast_fallback = bool(
            cfg["trigger_mode"] == "forecast"
            and not forecast_trigger
            and (healthy <= 0 or availability_drop)
        )
        static_replenishing = (
            next_replenishing_state(healthy, cfg, bool(state.get("replenishing")))
            if cfg["trigger_mode"] == "inventory" else False
        )
        replenishing = bool(
            (forecast_trigger or forecast_fallback)
            if cfg["trigger_mode"] == "forecast" else static_replenishing
        )
        result["healthy"] = healthy
        result["total"] = len(accounts)
        result["trigger_mode"] = cfg["trigger_mode"]
        result["replenishing"] = replenishing
        result["static_replenishing"] = static_replenishing
        result["forecast_trigger"] = forecast_trigger
        result["forecast_fallback"] = forecast_fallback
        result["forecast_fallback_reason"] = (
            "availability_drop" if availability_drop else ("empty_pool" if healthy <= 0 else "")
        )
        result["forecast_sampled"] = True
        result["quota_forecast"] = quota_forecast
        state["replenishing"] = replenishing
        state["inventory"] = {
            "healthy": healthy,
            "total": len(accounts),
            "checked_at": _now(),
        }
        _save_state(state)
        quantity = calculate_purchase_quantity(
            healthy,
            cfg,
            replenishing=replenishing,
            forecast_trigger=forecast_trigger,
            forecast_fallback=forecast_fallback,
            quota_forecast=quota_forecast,
        )
        result["quantity"] = quantity
        if quantity <= 0:
            action = "inventory_ok"
            if cfg["trigger_mode"] == "forecast":
                action = "forecast_not_triggered"
            result.update({"ok": True, "action": action})
            return result
        selected_provider = ""
        provider_errors: list[dict[str, Any]] = []
        selected_api: Any = None
        selected_product = ""
        for provider in _provider_names(cfg):
            injected = client if provider == "sogou" else None
            if not _provider_configured(provider, injected_client=injected):
                provider_errors.append({"provider": provider, "error": "未配置供应商凭据"})
                continue
            provider_client = _new_provider_client(provider, injected_client=injected)
            product = _provider_product(provider, cfg)
            try:
                provider_client.balance()
                inventory = provider_client.inventory(product, quantity)
                available = _inventory_available(inventory)
                if available is not None and available < quantity:
                    raise RuntimeError(f"库存不足: available={available}, required={quantity}")
            except (SogouEduError, BugTeamError, RuntimeError) as exc:
                message, status_code = _provider_error(exc)
                provider_errors.append({"provider": provider, "error": message, "status_code": status_code})
                continue
            selected_provider = provider
            selected_api = provider_client
            selected_product = product
            break
        if not selected_provider or selected_api is None:
            result.update({"error": "所有补池供应商均无法满足库存", "provider_errors": provider_errors})
            return result
        key = f"{_provider_order_key(selected_provider)}-{uuid.uuid4().hex}"
        state["current_order"] = {
            "idempotency_key": key,
            "quantity": quantity,
            "product": selected_product,
            "provider": selected_provider,
            "provider_index": _provider_names(cfg).index(selected_provider),
            "provider_retry_count": 0,
            "created_at": _now(),
            "status": "creating",
            "config_snapshot": {key: cfg[key] for key in ("push_group_id", "concurrency", "priority", "load_factor", "rate_multiplier", "model_whitelist", "auto_pause_on_expired", "product", "bugteam_product", "partial_retry_limit", "provider_priority", "forecast_fallback_quantity")},
        }
        _save_state(state)
        order_result = _process_current_order(selected_api, cfg, state)
        result.update(order_result)
        result["provider"] = selected_provider
        result["ok"] = True
        return result
    except (SogouEduError, BugTeamError) as exc:
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
