#!/usr/bin/env python3
"""Read-only health audit for the SogouEdu restock worker."""
from __future__ import annotations

import json
import logging
import sys
import subprocess
from collections import Counter
from pathlib import Path

# systemd 直接执行 tools/ 下的脚本时，Python 默认不会把项目根目录加入
# sys.path；显式加入，确保能读取与 WebUI 相同的业务状态模块。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import sogouedu_restock as restock

logger = logging.getLogger("sogou_restock_audit")


def _status(level: str, message: str) -> None:
    logger.warning("[%s] %s", level, message)


def _recent_runs() -> list[dict]:
    rows = restock.get_restock_log_tail(200)
    return [row for row in rows if isinstance(row, dict)]


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
    return sum(1 for line in proc.stdout.splitlines() if any(x in line for x in needles) and any(x in line for x in bad))


def run_audit() -> int:
    cfg = restock.load_restock_config()
    state = restock._load_state()
    current = state.get("current_order")
    runs = _recent_runs()
    counts = Counter(str(row.get("action") or row.get("status") or "unknown") for row in runs)

    if not cfg.get("enabled"):
        _status("WARN", "自动补池开关未开启")
    else:
        _status("OK", "自动补池已开启")

    if isinstance(current, dict):
        order_id = current.get("order_id") or "unknown"
        order_status = current.get("status") or "unknown"
        last_error = str(current.get("last_error") or "").strip()
        if last_error or order_status in {"error", "failed", "cancelled", "canceled"}:
            _status("CRIT", f"当前订单 {order_id} status={order_status} error={last_error[:180]}")
        else:
            _status("WARN", f"当前订单 {order_id} status={order_status}")
    else:
        _status("OK", "当前没有待处理订单")

    for action in ("push_waiting", "push_retry", "order_failed"):
        if counts.get(action):
            _status("WARN", f"最近运行记录 {action}={counts[action]}")

    recovery_rows = restock._read_json(restock.RECOVERIES_PATH, [])
    if not isinstance(recovery_rows, list):
        recovery_rows = []
    recovery_counts = Counter(str(row.get("status") or row.get("delivery_status") or "unknown").lower() for row in recovery_rows if isinstance(row, dict))
    claimable = sum(1 for row in recovery_rows if isinstance(row, dict) and (str(row.get("status") or row.get("delivery_status") or "").lower() in {"claimable", "ready", "available"} or row.get("claim_url")))
    failed = sum(1 for row in recovery_rows if isinstance(row, dict) and row.get("last_error") and not row.get("processed_at"))
    if claimable:
        _status("WARN", f"本地恢复记录待认领={claimable}")
    else:
        _status("OK", f"本地恢复记录无待认领，状态={dict(recovery_counts)}")
    if failed:
        _status("WARN", f"本地恢复记录未成功且有错误={failed}")

    journal_errors = _journal_errors()
    if journal_errors > 0:
        _status("WARN", f"最近 10 分钟业务日志异常行={journal_errors}")
    elif journal_errors == 0:
        _status("OK", "最近 10 分钟未发现 Sogou/补池异常日志")
    else:
        _status("WARN", "无法读取 journalctl，跳过日志检查")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    raise SystemExit(run_audit())
