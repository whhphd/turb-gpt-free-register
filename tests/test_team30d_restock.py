# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from core.team30d_client import extract_order_fields, health_need_reclaim, parse_card_codes
from core import team30d_restock as rs


class ParseCodesTests(unittest.TestCase):
    def test_one_per_line_skip_blank_and_comment(self):
        text = "RCL-aaa\n\n# skip\nRCL-aaa\nRCL-bbb\n"
        self.assertEqual(parse_card_codes(text), ["RCL-aaa", "RCL-bbb"])


class HealthNeedTests(unittest.TestCase):
    def test_need_reclaim_from_items(self):
        payload = {
            "items": [
                {"card_code": "RCL-1", "status": "need_reclaim"},
                {"card_code": "RCL-2", "status": "healthy"},
            ]
        }
        self.assertEqual(health_need_reclaim(payload), ["RCL-1"])

    def test_empty_on_unknown_shape(self):
        self.assertEqual(health_need_reclaim({"ok": True}), [])


class OrderFieldsTests(unittest.TestCase):
    def test_nested_order(self):
        no, token, st = extract_order_fields({
            "order": {"order_no": "ORD1", "download_token": "tok", "status": "pending"}
        })
        self.assertEqual((no, token, st), ("ORD1", "tok", "pending"))

    def test_top_level_token_survives_status_poll_without_token(self):
        no, token, st = extract_order_fields({
            "ok": True,
            "order_no": "ORD1",
            "download_token": "tok",
            "status": "pending",
        })
        self.assertEqual((no, token, st), ("ORD1", "tok", "pending"))
        no2, token2, st2 = extract_order_fields({
            "ok": True,
            "order": {"order_no": "ORD1", "status": "completed", "downloadable": True},
        })
        self.assertEqual((no2, token2, st2), ("ORD1", "", "completed"))


class RedeemAllTests(unittest.TestCase):
    def test_redeems_every_code_not_inventory_qty(self):
        client = MagicMock()
        client.preview.return_value = {"preview": {"can_redeem_remaining": True, "card_quota_remaining": 1}}
        client.redeem.return_value = {"order": {"order_no": "O1", "download_token": "t1", "status": "success"}}
        client.wait_order.side_effect = lambda o: o
        client.download.return_value = {"access_token": "tok", "email": "a@b.com"}
        prepared = [("a@b.com", {"name": "a@b.com", "credentials": {"access_token": "tok"}, "extra": {}})]
        with patch.object(rs, "_client", return_value=client), \
             patch.object(rs, "_payload_accounts", return_value=prepared), \
             patch.object(rs, "push_prepared_accounts_to_pool", return_value={"ok": True, "success": 1, "failed": 0, "results": [{"ok": True, "email": "a@b.com", "account_id": 11}]}), \
             patch.object(rs, "_upsert_card"), \
             patch.object(rs, "_log"), \
             patch.object(rs, "get_config", return_value=dict(rs.DEFAULT_CONFIG)):
            out = rs.redeem_codes("RCL-1\nRCL-2")
        self.assertEqual(client.redeem.call_count, 2)
        self.assertEqual(out["total"], 2)

    def test_reclaim_skips_when_health_check_fails(self):
        client = MagicMock()
        client.health_check.side_effect = RuntimeError("timeout")
        with patch.object(rs, "list_cards", return_value=[{"card_code": "RCL-1"}]), \
             patch.object(rs, "_client", return_value=client), \
             patch.object(rs, "_pool_401_card_codes", return_value=[]), \
             patch.object(rs, "_log"), \
             patch.object(rs, "get_config", return_value=dict(rs.DEFAULT_CONFIG)):
            out = rs.run_reclaim_once()
        client.batch_reclaim.assert_not_called()
        self.assertFalse(out["ok"])
        self.assertEqual(out["reclaimed"], 0)

    def test_reclaim_runs_when_need_detected(self):
        client = MagicMock()
        client.health_check.return_value = {"items": [{"card_code": "RCL-1", "status": "need_reclaim"}]}
        client.batch_reclaim.return_value = {"already_running": 0}
        client.poll_reclaim.return_value = {"already_running": 0}
        client.download.return_value = {"access_token": "tok", "email": "a@b.com"}
        card = {"card_code": "RCL-1", "order_no": "O1", "download_token": "t1", "emails": ["a@b.com"], "pool_ids": [9]}
        with patch.object(rs, "list_cards", return_value=[card]), \
             patch.object(rs, "_client", return_value=client), \
             patch.object(rs, "_pool_401_card_codes", return_value=[]), \
             patch.object(rs, "_update_pool_from_payload", return_value={"updated": 1, "created": 0, "errors": []}), \
             patch.object(rs, "_upsert_card"), \
             patch.object(rs, "_log"), \
             patch.object(rs, "get_config", return_value=dict(rs.DEFAULT_CONFIG)):
            out = rs.run_reclaim_once()
        client.batch_reclaim.assert_called_once()
        self.assertEqual(out["reclaimed"], 1)


if __name__ == "__main__":
    unittest.main()
