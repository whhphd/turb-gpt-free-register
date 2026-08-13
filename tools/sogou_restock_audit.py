#!/usr/bin/env python3
"""Read-only health audit for the SogouEdu restock worker."""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import sogouedu_restock as restock

logger = logging.getLogger("sogou_restock_audit")

RECENT_WINDOW_SEC = 15 * 60
PARTIAL_FINALIZE_SEC = 5 * 60
PARTIAL_ALERT_SEC = 6 * 60
FINALIZE_PUSH_ALERT_SEC = 2 * 60
RECOVERY_ALERT_SEC = 5 * 60


def _status(level: str, message: str) -> None:
    logger.warning("[%s] %s", level, message)


def _event_time(row: dict[str, Any]) -> float | None:
    for key in ("finished_at", "updated_at", "started_at", "created_at"):
        parsed = restock._parse_time(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _recent_runs(now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    rows = restock.get_restock_log_tail(500)
    return [
        row for row in rows
        if isinstance(row, dict)
        and (event_time := _event_time(row)) is not None
        and 0 <= now - event_time <= RECENT_WINDOW_SEC
    ]


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}分{seconds:02d}秒"


def _service_active() -> bool | None:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", "turb-gpt-webui.service"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0 and proc.stdout.strip() == "active"


def _journal_errors() -> int:
    try:
        proc = subprocess.run(
            [
                "journalctl", "-u", "turb-gpt-webui.service",
                "--since", "10 minutes ago", "--no-pager", "-o", "cat",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    needles = ("Sogou", "sogou", "补池", "push_waiting", "claim", "recover")
    bad = ("ERROR", "Traceback", "失败", "异常", "CRITICAL")
    return sum(
        1 for line in proc.stdout.splitlines()
        if any(x in line for x in needles) and any(x in line for x in bad)
    )


def _unprocessed_claimable(row: dict[str, Any]) -> bool:
    if row.get("processed_at"):
        return False
    status = str(row.get("status") or row.get("delivery_status") or "").lower()
    return status in {"claimable", "ready", "available"} or bool(row.get("claim_url"))


def collect_audit_messages(now: float | None = None) -> list[tuple[str, str]]:
    now = time.time() if now is None else now
    cfg = restock.load_restock_config()
    state = restock._load_state()
    current = state.get("current_order")
    runs = _recent_runs(now)
    messages: list[tuple[str, str]] = []

    service_active = _service_active()
    if service_active is True:
        messages.append(("OK", "WebUI 服务 active"))
    elif service_active is False:
        messages.append(("CRIT", "WebUI 服务不是 active"))
    else:
        messages.append(("WARN", "无法读取 WebUI 服务状态"))

    if not cfg.get("enabled"):
        messages.append(("WARN", "自动补池开关未开启"))
    else:
        messages.append(("OK", "自动补池已开启"))

    last_run = state.get("last_run") if isinstance(state.get("last_run"), dict) else None
    last_run_time = _event_time(last_run) if last_run else None
    stale_after = max(180, int(cfg.get("monitor_interval_sec") or 60) * 4)
    if cfg.get("enabled") and (last_run_time is None or now - last_run_time > stale_after):
        age = "无运行记录" if last_run_time is None else f"已停止 {_duration(now - last_run_time)}"
        messages.append(("CRIT", f"补池 worker 运行异常：{age}"))
    elif last_run_time is not None:
        messages.append(("OK", f"补池 worker 最近运行于 {_duration(now - last_run_time)} 前"))

    if isinstance(current, dict):
        order_id = current.get("order_id") or "unknown"
        order_status = current.get("status") or "unknown"
        reserved = int(current.get("reserved") or 0)
        last_error = str(current.get("last_error") or "").strip()
        partial_since = restock._parse_time(current.get("partial_ready_since"))
        if last_error or order_status in {"error", "failed", "cancelled", "canceled"}:
            messages.append(("CRIT", f"当前订单 {order_id} status={order_status} error={last_error[:180]}"))
        elif partial_since is not None and reserved > 0:
            elapsed = max(0, now - partial_since)
            remaining = max(0, PARTIAL_FINALIZE_SEC - elapsed)
            level = "CRIT" if elapsed > PARTIAL_ALERT_SEC else "WARN"
            text = (
                f"当前订单 {order_id} 部分备货 {reserved} 个，已等待 {_duration(elapsed)}，"
                f"距自动结算 {_duration(remaining)}"
            )
            if level == "CRIT":
                text += "，超过 6 分钟仍未结算"
            messages.append((level, text))
        else:
            messages.append(("WARN", f"当前订单 {order_id} status={order_status} reserved={reserved}"))
    else:
        messages.append(("OK", "当前没有待处理订单"))

    pushed_by_order: dict[str, float] = {}
    for row in runs:
        if row.get("action") != "pushed" or not row.get("order_id"):
            continue
        event_time = _event_time(row)
        if event_time is not None:
            pushed_by_order[str(row["order_id"])] = max(
                event_time,
                pushed_by_order.get(str(row["order_id"]), 0),
            )
    for row in runs:
        if row.get("action") != "partial_finalized" or not row.get("order_id"):
            continue
        order_id = str(row["order_id"])
        finalized_at = _event_time(row)
        if finalized_at is None:
            continue
        pushed_at = pushed_by_order.get(order_id)
        if pushed_at is None or pushed_at < finalized_at:
            elapsed = now - finalized_at
            level = "CRIT" if elapsed > FINALIZE_PUSH_ALERT_SEC else "WARN"
            messages.append((level, f"订单 {order_id} 部分结算后等待推池 {_duration(elapsed)}"))

    counts = Counter(str(row.get("action") or "unknown") for row in runs)
    for action in ("push_waiting", "push_retry", "order_failed"):
        if counts.get(action):
            messages.append(("WARN", f"最近 15 分钟运行记录 {action}={counts[action]}"))

    pushed = [row for row in runs if row.get("action") == "pushed"]
    if pushed:
        success = sum(int((row.get("result") or {}).get("success") or 0) for row in pushed)
        failed = sum(int((row.get("result") or {}).get("failed") or 0) for row in pushed)
        messages.append(("OK" if failed == 0 else "WARN", f"最近 15 分钟推池成功={success} 失败={failed}"))
    finalized = sum(1 for row in runs if row.get("action") == "partial_finalized")
    if finalized:
        messages.append(("OK", f"最近 15 分钟部分结算={finalized}"))
    repaired = sum(int((row.get("recovery") or {}).get("repaired") or 0) for row in runs)
    recreated = sum(int((row.get("recovery") or {}).get("recreated") or 0) for row in runs)
    if repaired or recreated:
        messages.append(("OK", f"最近 15 分钟补发原位修复={repaired} 新建={recreated}"))

    recovery_rows = restock._read_json(restock.RECOVERIES_PATH, [])
    if not isinstance(recovery_rows, list):
        recovery_rows = []
    pending = [row for row in recovery_rows if isinstance(row, dict) and _unprocessed_claimable(row)]
    overdue = [
        row for row in pending
        if (event_time := _event_time(row)) is not None and now - event_time > RECOVERY_ALERT_SEC
    ]
    if overdue:
        messages.append(("CRIT", f"补发待认领={len(pending)}，其中超过 5 分钟={len(overdue)}"))
    elif pending:
        messages.append(("WARN", f"补发待认领={len(pending)}，均未超过 5 分钟"))
    else:
        messages.append(("OK", "本地恢复记录无待认领"))
    recent_failed = [
        row for row in recovery_rows
        if isinstance(row, dict)
        and not row.get("processed_at")
        and row.get("last_error")
        and (event_time := _event_time(row)) is not None
        and 0 <= now - event_time <= RECENT_WINDOW_SEC
    ]
    if recent_failed:
        messages.append(("WARN", f"最近 15 分钟补发未成功且有错误={len(recent_failed)}"))

    journal_errors = _journal_errors()
    if journal_errors > 0:
        messages.append(("WARN", f"最近 10 分钟业务日志异常行={journal_errors}"))
    elif journal_errors == 0:
        messages.append(("OK", "最近 10 分钟未发现 Sogou/补池异常日志"))
    else:
        messages.append(("WARN", "无法读取 journalctl，跳过日志检查"))
    return messages


def run_audit() -> int:
    for level, message in collect_audit_messages():
        _status(level, message)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    raise SystemExit(run_audit())
