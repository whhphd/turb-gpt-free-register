# -*- coding: utf-8 -*-
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import sogouedu_restock as restock


def _jwt(email: str = "sogou@example.com") -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1893456000, "email": email}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


class FakeClient:
    def __init__(self):
        self.created = []
        self.inventory_calls = []
        self.balance_calls = 0
        self.take_calls = 0

    def balance(self):
        self.balance_calls += 1
        return {"data": {"balance": 100}}

    def inventory(self, product, quantity):
        self.inventory_calls.append((product, quantity))
        return {"data": {"available": quantity}}

    def create_order(self, product, quantity, *, idempotency_key):
        self.created.append((product, quantity, idempotency_key))
        return {"data": {"order_id": "order-1", "status": "pending"}}

    def order_status(self, order_id, *, status_url=None):
        return {"data": {"order_id": order_id, "status": "ready"}}

    def take_order(self, order_id, *, take_url=None):
        self.take_calls += 1
        return {"data": {"accounts": [{"email": "sogou@example.com", "access_token": _jwt()}]}}

    def list_recoveries(self, *, before_id=None, limit=100):
        return {"data": {"items": []}}

    def claim_recovery(self, recovery_id, *, claim_url=None, ticket=None):
        return {"data": {"accounts": [{"email": "orphan@example.com", "access_token": _jwt("orphan@example.com")}]}}


class SogouRestockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = {
            "RESTOCK_DIR": root,
            "CONFIG_PATH": root / "config.json",
            "STATE_PATH": root / "state.json",
            "ORDERS_PATH": root / "orders.json",
            "RECOVERIES_PATH": root / "recoveries.json",
            "RUNS_PATH": root / "runs.jsonl",
        }
        self.path_patch = patch.multiple(restock, **self.paths)
        self.path_patch.start()
        restock._RUNNING = False

    def tearDown(self):
        restock._RUNNING = False
        self.path_patch.stop()
        self.temp.cleanup()

    def test_config_normalizes_product_and_model_whitelist(self):
        cfg = restock.save_restock_config({
            "product": "bad",
            "min_healthy": 9,
            "target_healthy": 2,
            "model_whitelist": ["gpt-5.5", "", "gpt-5.5"],
        })
        self.assertEqual(cfg["product"], "oauth_7d")
        self.assertEqual(cfg["target_healthy"], 9)
        self.assertEqual(cfg["model_whitelist"], ["gpt-5.5"])

    def test_healthy_counts_all_sources_but_excludes_bad_rows(self):
        rows = [
            {"status": "active", "schedulable": True, "extra": {"import_source": "manual"}},
            {"status": "active", "schedulable": True, "extra": {"import_source": "sogouedu_auto_restock"}},
            {"status": "error", "schedulable": True},
            {"status": "active", "schedulable": False},
            {"status": "inactive", "schedulable": True},
        ]
        self.assertEqual(restock.count_healthy_accounts(rows), 2)

    def test_cycle_does_not_order_when_inventory_is_enough(self):
        restock.save_restock_config({"enabled": True, "min_healthy": 2, "target_healthy": 4})
        fake = FakeClient()
        rows = [{"status": "active", "schedulable": True} for _ in range(4)]
        with patch.object(restock._pool_monitor, "fetch_pool_accounts", return_value=rows):
            result = restock.run_restock_cycle(client=fake)
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "inventory_ok")
        self.assertEqual(fake.created, [])

    def test_cycle_orders_gap_capped_with_persisted_key(self):
        restock.save_restock_config({
            "enabled": True,
            "min_healthy": 5,
            "target_healthy": 10,
            "max_purchase_per_order": 3,
        })
        fake = FakeClient()
        rows = [{"status": "active", "schedulable": True}]
        with patch.object(restock._pool_monitor, "fetch_pool_accounts", return_value=rows):
            result = restock.run_restock_cycle(client=fake)
        self.assertTrue(result["ok"])
        self.assertEqual(fake.inventory_calls, [("oauth_7d", 3)])
        self.assertEqual(fake.created[0][1], 3)
        self.assertTrue(fake.created[0][2].startswith("sogou-restock-"))

    def test_cycle_uses_sub2api_current_active_filter_for_health(self):
        restock.save_restock_config({
            "enabled": True,
            "min_healthy": 5,
            "target_healthy": 10,
            "max_purchase_per_order": 3,
        })
        fake = FakeClient()
        all_rows = [
            {"id": 1, "status": "active", "schedulable": True},
            {"id": 2, "status": "active", "schedulable": True},
        ]

        def fetch_accounts(**kwargs):
            return [] if kwargs.get("status") == "active" else all_rows

        with patch.object(restock._pool_monitor, "fetch_pool_accounts", side_effect=fetch_accounts) as fetched:
            result = restock.run_restock_cycle(client=fake)

        self.assertTrue(result["ok"])
        self.assertEqual(result["healthy"], 0)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["quantity"], 3)
        self.assertEqual(fake.inventory_calls, [("oauth_7d", 3)])
        self.assertEqual([call.kwargs.get("status") for call in fetched.call_args_list], [None, "active"])
        self.assertEqual(restock._load_state()["inventory"], {
            "healthy": 0,
            "total": 2,
            "checked_at": result["started_at"],
        })

    def test_prepared_payload_applies_model_whitelist_and_pool_settings(self):
        cfg = restock.normalize_restock_config({
            "push_group_id": 12,
            "concurrency": 7,
            "model_whitelist": ["gpt-5.5", "gpt-5.4"],
            "auto_pause_on_expired": False,
        })
        prepared = restock._build_prepared(
            {"accounts": [{"email": "model@example.com", "access_token": _jwt("model@example.com")}]},
            cfg,
            order_id="order-model",
        )
        account = prepared[0][1]
        self.assertEqual(account["group_ids"], [12])
        self.assertEqual(account["concurrency"], 7)
        self.assertEqual(account["credentials"]["model_mapping"], {"gpt-5.5": "gpt-5.5", "gpt-5.4": "gpt-5.4"})
        self.assertFalse(account["auto_pause_on_expired"])

    def test_unfinished_order_is_taken_and_pushed_without_rebuy(self):
        restock.save_restock_config({"enabled": True, "min_healthy": 0, "target_healthy": 0})
        state = restock._load_state()
        state["current_order"] = {"order_id": "order-1", "quantity": 1, "status": "pending"}
        restock._save_state(state)
        fake = FakeClient()
        with patch.object(restock._pool_monitor, "fetch_pool_accounts", return_value=[]), patch.object(
            restock, "push_prepared_accounts_to_pool", return_value={"success": 1, "failed": 0}
        ) as pushed:
            result = restock.run_restock_cycle(client=fake)
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "pushed")
        self.assertEqual(fake.created, [])
        pushed.assert_called_once()
        self.assertIsNone(restock._load_state()["current_order"])

    def test_nested_pickup_payload_is_unwrapped_and_pushed(self):
        restock.save_restock_config({"enabled": True, "min_healthy": 0, "target_healthy": 0})
        state = restock._load_state()
        state["current_order"] = {"order_id": "67600", "quantity": 1, "status": "pending"}
        restock._save_state(state)
        fake = FakeClient()
        nested_response = {
            "data": {
                "order": {"id": 67600, "status": "completed"},
                "payload": {
                    "type": "sub2api",
                    "accounts": [{"email": "nested@example.com", "access_token": _jwt("nested@example.com")}],
                },
                "status": "completed",
            }
        }
        with patch.object(fake, "take_order", return_value=nested_response), patch.object(
            restock._pool_monitor, "fetch_pool_accounts", return_value=[]
        ), patch.object(
            restock, "push_prepared_accounts_to_pool", return_value={"success": 1, "failed": 0}
        ) as pushed:
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["action"], "pushed")
        self.assertEqual(pushed.call_args.args[0][0][1]["credentials"]["email"], "nested@example.com")
        self.assertIsNone(restock._load_state()["current_order"])

    def test_empty_pickup_payload_is_cleared_for_next_retry(self):
        restock.save_restock_config({
            "enabled": True,
            "min_healthy": 0,
            "target_healthy": 0,
            "order_poll_interval_sec": 1,
        })
        state = restock._load_state()
        state["current_order"] = {"order_id": "empty-order", "quantity": 1, "status": "pending"}
        restock._save_state(state)
        fake = FakeClient()
        empty_response = {"data": {"status": "completed", "payload": {"accounts": []}}}
        with patch.object(fake, "take_order", return_value=empty_response) as take, patch.object(
            restock._pool_monitor, "fetch_pool_accounts", return_value=[]
        ), patch.object(restock, "push_prepared_accounts_to_pool") as pushed:
            first = restock.run_restock_cycle(client=fake)
            second = restock.run_restock_cycle(client=fake)

        self.assertEqual(first["action"], "push_waiting")
        self.assertEqual(second["action"], "push_waiting")
        self.assertEqual(take.call_count, 2)
        pushed.assert_not_called()
        current = restock._load_state()["current_order"]
        self.assertEqual(current["status"], "ready")
        self.assertNotIn("payload", current)

    def test_orphan_recovery_recreates_with_source_marker(self):
        restock.save_restock_config({"enabled": True, "min_healthy": 0, "target_healthy": 0})
        fake = FakeClient()
        with patch.object(restock._pool_monitor, "fetch_pool_accounts", return_value=[]), patch.object(
            fake, "list_recoveries", return_value={"data": {"items": [{"id": "r-1", "pool_id": "missing-1", "status": "claimable"}]}}
        ), patch.object(
            restock, "push_prepared_accounts_to_pool", return_value={"success": 1, "failed": 0}
        ) as pushed:
            result = restock.run_restock_cycle(client=fake)
        self.assertTrue(result["ok"])
        self.assertEqual(result["recovery"]["recreated"], 1)
        payload = pushed.call_args.args[0][0][1]
        self.assertEqual(payload["extra"]["import_source"], "sogouedu_auto_restock")
        self.assertTrue(payload["extra"]["recreated"])
        self.assertEqual(payload["extra"]["replacement_of_pool_id"], "missing-1")

    def test_nested_recovery_claim_repairs_existing_account(self):
        restock.save_restock_config({"enabled": True, "min_healthy": 0, "target_healthy": 0})
        fake = FakeClient()
        recovery = {"id": "r-nested", "pool_id": "42", "delivery_status": "claimable", "claim_url": "/signed/claim"}
        claim_response = {
            "data": {
                "payload": {
                    "accounts": [{"email": "fixed@example.com", "access_token": _jwt("fixed@example.com")}]
                }
            }
        }
        existing = [{
            "id": 42,
            "status": "error",
            "extra": {"import_source": "sogouedu_auto_restock"},
            "credentials": {"email": "fixed@example.com"},
        }]
        with patch.object(fake, "list_recoveries", return_value={"data": {
            "items": [recovery], "next_before_id": "page-2"
        }}) as listed, patch.object(
            fake, "claim_recovery", return_value=claim_response
        ), patch.object(
            restock._pool_monitor, "fetch_pool_accounts", side_effect=[existing, []]
        ), patch.object(restock._pool_monitor, "_update_pool_credentials") as updated:
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["recovery"]["repaired"], 1)
        updated.assert_called_once()
        self.assertEqual(updated.call_args.args[0], 42)
        self.assertIsNone(listed.call_args.kwargs["before_id"])
        self.assertEqual(restock._load_state()["recovery_cursor"], "page-2")

    def test_delivered_recovery_is_skipped_without_claim(self):
        restock.save_restock_config({"enabled": True, "min_healthy": 0, "target_healthy": 0})
        fake = FakeClient()
        with patch.object(fake, "list_recoveries", return_value={"data": {"items": [
            {"id": "r-delivered", "delivery_status": "delivered"}
        ]}}), patch.object(fake, "claim_recovery") as claim, patch.object(
            restock._pool_monitor, "fetch_pool_accounts", side_effect=[[], []]
        ):
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["recovery"]["repaired"], 0)
        self.assertEqual(result["recovery"]["recreated"], 0)
        claim.assert_not_called()

    def test_empty_nested_recovery_claim_is_not_marked_processed(self):
        restock.save_restock_config({"enabled": True, "min_healthy": 0, "target_healthy": 0})
        fake = FakeClient()
        recovery = {"id": "r-empty", "delivery_status": "claimable"}
        with patch.object(fake, "list_recoveries", return_value={"data": {"items": [recovery]}}), patch.object(
            fake, "claim_recovery", return_value={"data": {"payload": {"accounts": []}}}
        ), patch.object(restock._pool_monitor, "fetch_pool_accounts", side_effect=[[], []]):
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["recovery"]["repaired"], 0)
        saved = restock._read_json(restock.RECOVERIES_PATH, [])
        row = next(x for x in saved if x.get("id") == "r-empty")
        self.assertIsNone(row.get("processed_at"))
        self.assertIn("有效 OAuth", row.get("last_error", ""))


if __name__ == "__main__":
    unittest.main()
