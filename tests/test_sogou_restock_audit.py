# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from tools import sogou_restock_audit as audit


class SogouRestockAuditTests(unittest.TestCase):
    def collect(self, *, now=1_000, config=None, state=None, runs=None, recoveries=None):
        config = config or {"enabled": True, "monitor_interval_sec": 10}
        state = state or {
            "current_order": None,
            "last_run": {"finished_at": now - 10, "action": "inventory_ok"},
        }
        with patch.object(audit.restock, "load_restock_config", return_value=config), patch.object(
            audit.restock, "_load_state", return_value=state
        ), patch.object(
            audit.restock, "get_restock_log_tail", return_value=runs or []
        ), patch.object(
            audit.restock, "_read_json", return_value=recoveries or []
        ), patch.object(audit, "_service_active", return_value=True), patch.object(
            audit, "_journal_errors", return_value=0
        ):
            return audit.collect_audit_messages(now)

    def test_recent_runs_filters_to_fifteen_minutes(self):
        rows = [
            {"action": "order_failed", "finished_at": 99},
            {"action": "pushed", "finished_at": 101},
        ]
        with patch.object(audit.restock, "get_restock_log_tail", return_value=rows):
            self.assertEqual(audit._recent_runs(1_000), [rows[1]])

    def test_partial_order_reports_countdown_before_six_minutes(self):
        messages = self.collect(state={
            "current_order": {
                "order_id": "p-1", "status": "ready_partial", "reserved": 5,
                "partial_ready_since": 760,
            },
            "last_run": {"finished_at": 990},
        })
        self.assertIn(
            ("WARN", "当前订单 p-1 部分备货 5 个，已等待 4分00秒，距自动结算 1分00秒"),
            messages,
        )

    def test_partial_order_over_six_minutes_is_critical(self):
        messages = self.collect(state={
            "current_order": {
                "order_id": "p-2", "status": "waiting_inventory", "reserved": 3,
                "partial_ready_since": 630,
            },
            "last_run": {"finished_at": 990},
        })
        self.assertTrue(any(level == "CRIT" and "超过 6 分钟仍未结算" in text for level, text in messages))

    def test_finalize_without_push_after_two_minutes_is_critical(self):
        messages = self.collect(runs=[{
            "action": "partial_finalized", "order_id": "p-3", "finished_at": 800,
        }])
        self.assertIn(("CRIT", "订单 p-3 部分结算后等待推池 3分20秒"), messages)

    def test_finalize_followed_by_push_reports_totals_without_timeout(self):
        messages = self.collect(runs=[
            {"action": "partial_finalized", "order_id": "p-4", "finished_at": 800},
            {"action": "pushed", "order_id": "p-4", "finished_at": 850,
             "result": {"success": 5, "failed": 0}},
            {"action": "inventory_ok", "finished_at": 900,
             "recovery": {"repaired": 4, "recreated": 1}},
        ])
        self.assertFalse(any("等待推池" in text for _, text in messages))
        self.assertIn(("OK", "最近 15 分钟推池成功=5 失败=0"), messages)
        self.assertIn(("OK", "最近 15 分钟部分结算=1"), messages)
        self.assertIn(("OK", "最近 15 分钟补发原位修复=4 新建=1"), messages)

    def test_processed_recovery_and_old_error_do_not_warn(self):
        messages = self.collect(recoveries=[
            {"delivery_status": "claimable", "processed_at": 900, "last_error": "old", "updated_at": 900},
            {"delivery_status": "delivered", "last_error": "old", "updated_at": 1},
        ])
        self.assertIn(("OK", "本地恢复记录无待认领"), messages)
        self.assertFalse(any("补发未成功" in text for _, text in messages))

    def test_unprocessed_recovery_over_five_minutes_is_critical(self):
        messages = self.collect(recoveries=[
            {"delivery_status": "claimable", "updated_at": 650},
            {"delivery_status": "claimable", "updated_at": 900},
        ])
        self.assertIn(("CRIT", "补发待认领=2，其中超过 5 分钟=1"), messages)

    def test_inactive_webui_and_stale_worker_are_critical(self):
        state = {"current_order": None, "last_run": {"finished_at": 700}}
        with patch.object(audit.restock, "load_restock_config", return_value={
            "enabled": True, "monitor_interval_sec": 10,
        }), patch.object(audit.restock, "_load_state", return_value=state), patch.object(
            audit.restock, "get_restock_log_tail", return_value=[]
        ), patch.object(audit.restock, "_read_json", return_value=[]), patch.object(
            audit, "_service_active", return_value=False
        ), patch.object(audit, "_journal_errors", return_value=0):
            messages = audit.collect_audit_messages(1_000)
        self.assertIn(("CRIT", "WebUI 服务不是 active"), messages)
        self.assertTrue(any(level == "CRIT" and "补池 worker 运行异常" in text for level, text in messages))


if __name__ == "__main__":
    unittest.main()
