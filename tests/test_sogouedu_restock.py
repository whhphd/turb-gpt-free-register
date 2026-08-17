# -*- coding: utf-8 -*-
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import sogouedu_restock as restock
from core.sogouedu_client import SogouEduClient


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
        self.finalize_calls = []
        self.cancel_calls = []

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

    def finalize_order(self, order_id):
        self.finalize_calls.append(order_id)
        return {"order": {"id": order_id, "status": "completed"}, "status": "completed"}

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        return {"order_id": order_id, "state": "cancelled"}

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

    def test_worker_rechecks_immediately_after_delivery_or_followup(self):
        cfg = restock.normalize_restock_config({"monitor_interval_sec": 3, "order_poll_interval_sec": 3})
        self.assertEqual(
            restock._next_worker_wait_seconds(cfg, {"last_run": {"action": "pushed"}}),
            1,
        )
        self.assertEqual(
            restock._next_worker_wait_seconds(
                cfg,
                {"current_order": {"status": "creating"}, "last_run": {"action": "provider_retry_scheduled"}},
            ),
            1,
        )
        self.assertEqual(
            restock._next_worker_wait_seconds(
                cfg,
                {"current_order": {"status": "pending"}, "last_run": {"action": "waiting"}},
            ),
            3,
        )
        self.assertEqual(
            restock._next_worker_wait_seconds(cfg, {"last_run": {"action": "forecast_not_triggered"}}),
            3,
        )

    def test_trigger_mode_is_exclusive_and_legacy_forecast_is_migrated(self):
        inventory = restock.normalize_restock_config({"trigger_mode": "inventory", "forecast_enabled": True})
        self.assertEqual(inventory["trigger_mode"], "inventory")
        self.assertFalse(inventory["forecast_enabled"])
        forecast = restock.normalize_restock_config({"trigger_mode": "forecast"})
        self.assertEqual(forecast["trigger_mode"], "forecast")
        self.assertTrue(forecast["forecast_enabled"])
        legacy = restock.normalize_restock_config({"forecast_enabled": True})
        self.assertEqual(legacy["trigger_mode"], "forecast")

    def test_forecast_fallback_quantity_defaults_to_five(self):
        cfg = restock.normalize_restock_config({"trigger_mode": "forecast"})
        self.assertEqual(cfg["forecast_fallback_quantity"], 5)
        self.assertNotIn("forecast_sample_interval_sec", cfg)
        cfg = restock.normalize_restock_config({"trigger_mode": "forecast", "forecast_fallback_quantity": 0})
        self.assertEqual(cfg["forecast_fallback_quantity"], 5)

    def test_forecast_purchase_quantity_does_not_use_inventory_target(self):
        cfg = restock.normalize_restock_config({
            "trigger_mode": "forecast",
            "min_healthy": 5,
            "target_healthy": 100,
            "max_purchase_per_order": 3,
        })
        forecast = {
            "windows": {
                "10080m": {
                    "remaining_units": 0.1,
                    "planned_rate_units_per_min": 0.1,
                    "capacity_units_per_account": 1.0,
                }
            }
        }
        self.assertEqual(restock.calculate_purchase_quantity(100, cfg, replenishing=True, forecast_trigger=True, quota_forecast=forecast), 3)

    def test_forecast_purchase_quantity_targets_configured_runway(self):
        cfg = restock.normalize_restock_config({
            "trigger_mode": "forecast",
            "forecast_interrupt_minutes": 20,
            "forecast_target_minutes": 25,
            "max_purchase_per_order": 10,
        })
        forecast = {
            "windows": {
                "10080m": {
                    "remaining_units": 1.5,
                    "planned_rate_units_per_min": 0.1,
                    "capacity_units_per_account": 1.0,
                },
                "43200m": {
                    "remaining_units": 0.5,
                    "planned_rate_units_per_min": 0.1,
                    "capacity_units_per_account": 1.0,
                },
            }
        }
        # The 30d window needs two accounts to reach 2.5 units / 25 minutes;
        # the 7d window needs only one.
        self.assertEqual(restock.calculate_purchase_quantity(31, cfg, replenishing=True, forecast_trigger=True, quota_forecast=forecast), 2)

    def test_forecast_purchase_quantity_respects_single_order_cap(self):
        cfg = restock.normalize_restock_config({
            "trigger_mode": "forecast",
            "forecast_target_minutes": 25,
            "max_purchase_per_order": 3,
        })
        forecast = {"windows": {"10080m": {
            "remaining_units": 0,
            "planned_rate_units_per_min": 1,
            "capacity_units_per_account": 1,
        }}}
        self.assertEqual(restock.calculate_purchase_quantity(31, cfg, replenishing=True, forecast_trigger=True, quota_forecast=forecast), 3)

    def test_healthy_counts_all_sources_but_excludes_bad_rows(self):
        rows = [
            {"status": "active", "schedulable": True, "extra": {"import_source": "manual"}},
            {"status": "active", "schedulable": True, "extra": {"import_source": "sogouedu_auto_restock"}},
            {"status": "error", "schedulable": True},
            {"status": "active", "schedulable": False},
            {"status": "inactive", "schedulable": True},
        ]
        self.assertEqual(restock.count_healthy_accounts(rows), 2)

    def test_order_history_upsert_updates_each_provider_without_leaking_payload(self):
        restock._write_json(restock.ORDERS_PATH, [
            {"order_id": "same-id", "provider": "sogou", "status": "waiting_inventory"},
            {"order_id": "same-id", "provider": "bugteam", "status": "waiting_inventory"},
        ])
        restock._upsert_order_history(
            {
                "order_id": "same-id",
                "provider": "bugteam",
                "status": "taken",
                "payload": {"accounts": [{"access_token": "secret"}]},
            },
            status="pushed",
            remote_status="completed",
            delivered_quantity=1,
        )
        restock._upsert_order_history(
            {"order_id": "sogou-new", "provider": "sogou", "status": "completed"},
            status="pushed",
            remote_status="completed",
            delivered_quantity=2,
        )

        rows = restock._read_json(restock.ORDERS_PATH, [])
        bugteam = next(row for row in rows if row.get("provider") == "bugteam")
        sogou = next(row for row in rows if row.get("provider") == "sogou" and row.get("order_id") == "same-id")
        new_sogou = next(row for row in rows if row.get("order_id") == "sogou-new")
        self.assertEqual(bugteam["status"], "pushed")
        self.assertEqual(bugteam["remote_status"], "completed")
        self.assertEqual(bugteam["delivered_quantity"], 1)
        self.assertNotIn("payload", bugteam)
        self.assertEqual(sogou["status"], "waiting_inventory")
        self.assertEqual(new_sogou["status"], "pushed")

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
        self.assertTrue(restock._load_state()["replenishing"])

    def test_cycle_does_not_order_between_minimum_and_target_before_trigger(self):
        restock.save_restock_config({
            "enabled": True,
            "min_healthy": 5,
            "target_healthy": 10,
            "max_purchase_per_order": 10,
        })
        fake = FakeClient()
        rows = [{"status": "active", "schedulable": True} for _ in range(7)]
        with patch.object(restock._pool_monitor, "fetch_pool_accounts", return_value=rows):
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["action"], "inventory_ok")
        self.assertEqual(result["quantity"], 0)
        self.assertFalse(result["replenishing"])
        self.assertEqual(fake.created, [])

    def test_cycle_continues_replenishing_between_minimum_and_target(self):
        restock.save_restock_config({
            "enabled": True,
            "min_healthy": 5,
            "target_healthy": 10,
            "max_purchase_per_order": 10,
        })
        state = restock._load_state()
        state["replenishing"] = True
        restock._save_state(state)
        fake = FakeClient()
        rows = [{"status": "active", "schedulable": True} for _ in range(7)]
        with patch.object(restock._pool_monitor, "fetch_pool_accounts", return_value=rows):
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["action"], "ordered")
        self.assertEqual(result["quantity"], 3)
        self.assertTrue(result["replenishing"])
        self.assertEqual(fake.created[0][1], 3)

    def test_cycle_stops_replenishing_at_target(self):
        restock.save_restock_config({
            "enabled": True,
            "min_healthy": 5,
            "target_healthy": 10,
        })
        state = restock._load_state()
        state["replenishing"] = True
        restock._save_state(state)
        fake = FakeClient()
        rows = [{"status": "active", "schedulable": True} for _ in range(10)]
        with patch.object(restock._pool_monitor, "fetch_pool_accounts", return_value=rows):
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["action"], "inventory_ok")
        self.assertEqual(result["quantity"], 0)
        self.assertFalse(result["replenishing"])
        self.assertFalse(restock._load_state()["replenishing"])
        self.assertEqual(fake.created, [])

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

        with patch.object(restock._pool_monitor, "fetch_pool_accounts", side_effect=fetch_accounts) as fetched, patch.object(
            restock, "_provider_configured", side_effect=lambda provider, **kwargs: provider == "sogou"
        ):
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

    def test_forecast_mode_bootstraps_when_no_schedulable_oauth_accounts(self):
        restock.save_restock_config({
            "enabled": True,
            "trigger_mode": "forecast",
            "min_healthy": 5,
            "target_healthy": 10,
            "max_purchase_per_order": 10,
            "forecast_fallback_quantity": 5,
        })
        fake = FakeClient()
        all_rows = [{"id": 1, "status": "active", "schedulable": True}]

        def fetch_accounts(**kwargs):
            return [] if kwargs.get("status") == "active" else all_rows

        with patch.object(restock._pool_monitor, "fetch_pool_accounts", side_effect=fetch_accounts):
            result = restock.run_restock_cycle(client=fake)

        self.assertTrue(result["ok"])
        self.assertEqual(result["healthy"], 0)
        self.assertFalse(result["forecast_trigger"])
        self.assertTrue(result["forecast_fallback"])
        self.assertEqual(result["forecast_fallback_reason"], "healthy_floor")
        self.assertEqual(result["quantity"], 5)
        self.assertEqual(fake.created[0][1], 5)

    def test_forecast_mode_does_not_fall_back_on_large_sampled_availability_drop(self):
        restock.save_restock_config({
            "enabled": True,
            "trigger_mode": "forecast",
            "max_purchase_per_order": 10,
            "forecast_fallback_quantity": 5,
        })
        state = restock._load_state()
        state["quota_forecast"] = {
            "last_sampled_at": 0,
            "previous_snapshot": {"account_count": 30},
        }
        restock._save_state(state)
        fake = FakeClient()
        all_rows = [{"id": index, "status": "active", "schedulable": True} for index in range(30)]
        active_rows = all_rows[:18]
        forecast_state = {
            "last_sampled_at": 100,
            "previous_snapshot": {"account_count": 30},
            "forecast": {
                "status": "ready",
                "eta_minutes": 45,
                "windows": {},
                "removed_accounts": 12,
            },
        }

        def fetch_accounts(**kwargs):
            return active_rows if kwargs.get("status") == "active" else all_rows

        with patch.object(restock._pool_monitor, "fetch_pool_accounts", side_effect=fetch_accounts), \
             patch.object(restock, "collect_quota_snapshot", wraps=restock.collect_quota_snapshot) as sampled, \
             patch.object(restock, "update_forecast", return_value=(forecast_state, forecast_state["forecast"])):
            result = restock.run_restock_cycle(client=fake)

        self.assertTrue(result["ok"])
        self.assertEqual(result["healthy"], 18)
        self.assertFalse(result["forecast_trigger"])
        self.assertFalse(result["forecast_fallback"])
        self.assertEqual(result["forecast_fallback_reason"], "")
        self.assertEqual(result["action"], "forecast_not_triggered")
        self.assertEqual(result["quantity"], 0)
        self.assertEqual(fake.created, [])
        self.assertEqual(len(sampled.call_args.args[0]), 18)
        self.assertEqual(len(sampled.call_args.kwargs["rate_accounts"]), 30)

    def test_forecast_mode_fallback_only_fills_to_healthy_floor(self):
        restock.save_restock_config({
            "enabled": True,
            "trigger_mode": "forecast",
            "max_purchase_per_order": 10,
            "forecast_fallback_quantity": 5,
        })
        fake = FakeClient()
        rows = [{"id": index, "status": "active", "schedulable": True} for index in range(3)]
        forecast = {"status": "ready", "eta_minutes": 45, "windows": {}}
        next_state = {"last_sampled_at": 100, "forecast": forecast}

        with patch.object(
            restock._pool_monitor, "fetch_pool_accounts", return_value=rows
        ), patch.object(restock, "update_forecast", return_value=(next_state, forecast)):
            result = restock.run_restock_cycle(client=fake)

        self.assertTrue(result["forecast_fallback"])
        self.assertEqual(result["forecast_fallback_reason"], "healthy_floor")
        self.assertEqual(result["quantity"], 2)
        self.assertEqual(fake.created[0][1], 2)

    def test_forecast_shortage_keeps_peak_quantity_when_new_estimate_drops(self):
        restock.save_restock_config({
            "enabled": True,
            "trigger_mode": "forecast",
            "max_purchase_per_order": 20,
            "forecast_fallback_quantity": 5,
        })
        state = restock._load_state()
        state["pending_restock_quantity"] = 20
        restock._save_state(state)
        fake = FakeClient()
        fake.inventory = lambda product, quantity: {"data": {"available": 0}}
        rows = [{"id": index, "status": "active", "schedulable": True} for index in range(4)]
        forecast = {"status": "insufficient", "eta_minutes": None, "windows": {}}

        with patch.object(restock._pool_monitor, "fetch_pool_accounts", return_value=rows), patch.object(
            restock, "update_forecast", return_value=({"forecast": forecast}, forecast)
        ), patch.object(restock, "_provider_configured", side_effect=lambda provider, **kwargs: provider == "sogou"):
            result = restock.run_restock_cycle(client=fake)

        self.assertFalse(result["ok"])
        self.assertEqual(result["calculated_quantity"], 1)
        self.assertEqual(result["quantity"], 20)
        self.assertEqual(result["pending_restock_quantity"], 20)
        self.assertEqual(restock._load_state()["pending_restock_quantity"], 20)

    def test_forecast_shortage_orders_partial_supplier_inventory(self):
        restock.save_restock_config({
            "enabled": True,
            "trigger_mode": "forecast",
            "max_purchase_per_order": 20,
            "forecast_fallback_quantity": 5,
        })
        state = restock._load_state()
        state["pending_restock_quantity"] = 20
        restock._save_state(state)
        fake = FakeClient()
        fake.inventory = lambda product, quantity: {"data": {"available": 3}}
        forecast = {"status": "insufficient", "eta_minutes": None, "windows": {}}

        with patch.object(restock._pool_monitor, "fetch_pool_accounts", return_value=[]), patch.object(
            restock, "update_forecast", return_value=({"forecast": forecast}, forecast)
        ), patch.object(restock, "_provider_configured", side_effect=lambda provider, **kwargs: provider == "sogou"):
            result = restock.run_restock_cycle(client=fake)

        self.assertTrue(result["ok"])
        self.assertEqual(result["quantity"], 20)
        self.assertEqual(result["order_quantity"], 3)
        self.assertEqual(fake.created[0][1], 3)
        state = restock._load_state()
        self.assertEqual(state["pending_restock_quantity"], 20)
        self.assertEqual(state["current_order"]["quantity"], 3)

    def test_pending_shortage_survives_an_insufficient_forecast_above_healthy_floor(self):
        restock.save_restock_config({
            "enabled": True,
            "trigger_mode": "forecast",
            "max_purchase_per_order": 20,
            "forecast_fallback_quantity": 5,
        })
        state = restock._load_state()
        state["pending_restock_quantity"] = 20
        restock._save_state(state)
        fake = FakeClient()
        fake.inventory = lambda product, quantity: {"data": {"available": 0}}
        rows = [{"id": index, "status": "active", "schedulable": True} for index in range(5)]
        forecast = {"status": "insufficient", "eta_minutes": None, "windows": {}}

        with patch.object(restock._pool_monitor, "fetch_pool_accounts", return_value=rows), patch.object(
            restock, "update_forecast", return_value=({"forecast": forecast}, forecast)
        ), patch.object(restock, "_provider_configured", side_effect=lambda provider, **kwargs: provider == "sogou"):
            result = restock.run_restock_cycle(client=fake)

        self.assertFalse(result["forecast_fallback"])
        self.assertTrue(result["pending_forecast_trigger"])
        self.assertEqual(result["calculated_quantity"], 0)
        self.assertEqual(result["quantity"], 20)
        self.assertEqual(restock._load_state()["pending_restock_quantity"], 20)

    def test_successful_push_decrements_pending_shortage(self):
        restock.save_restock_config({"enabled": True, "trigger_mode": "forecast"})
        state = restock._load_state()
        state["pending_restock_quantity"] = 20
        state["current_order"] = {
            "provider": "sogou",
            "order_id": "delivery-3",
            "quantity": 3,
            "status": "taken",
            "payload": {"accounts": [
                {"email": f"delivered-{index}@example.com", "access_token": _jwt(f"delivered-{index}@example.com")}
                for index in range(3)
            ]},
        }
        restock._save_state(state)
        fake = FakeClient()

        with patch.object(restock, "push_prepared_accounts_to_pool", return_value={"success": 3, "failed": 0}):
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["action"], "pushed")
        state = restock._load_state()
        self.assertEqual(state["pending_restock_quantity"], 17)
        self.assertIsNone(state["current_order"])

    def test_forecast_mode_stops_drop_fallback_after_replacements_arrive(self):
        restock.save_restock_config({
            "enabled": True,
            "trigger_mode": "forecast",
            "max_purchase_per_order": 10,
            "forecast_fallback_quantity": 5,
        })
        fake = FakeClient()
        state = restock._load_state()
        state["pending_restock_quantity"] = 10
        restock._save_state(state)
        rows = [{"id": index, "status": "active", "schedulable": True} for index in range(28)]
        forecast = {
            "status": "ready",
            "eta_minutes": 45,
            "windows": {},
            "removed_accounts": 12,
            "new_accounts": 10,
        }
        next_state = {"last_sampled_at": 100, "forecast": forecast}

        with patch.object(
            restock._pool_monitor, "fetch_pool_accounts", return_value=rows
        ), patch.object(restock, "update_forecast", return_value=(next_state, forecast)):
            result = restock.run_restock_cycle(client=fake)

        self.assertTrue(result["ok"])
        self.assertFalse(result["forecast_trigger"])
        self.assertFalse(result["forecast_fallback"])
        self.assertEqual(result["action"], "forecast_not_triggered")
        self.assertEqual(result["quantity"], 0)
        self.assertEqual(fake.created, [])
        self.assertEqual(restock._load_state()["pending_restock_quantity"], 0)

    def test_forecast_mode_samples_every_cycle_instead_of_reusing_cached_eta(self):
        restock.save_restock_config({
            "enabled": True,
            "trigger_mode": "forecast",
            "forecast_interrupt_minutes": 20,
            "max_purchase_per_order": 5,
        })
        state = restock._load_state()
        state["quota_forecast"] = {
            "last_sampled_at": 90,
            "forecast": {
                "status": "ready",
                "eta_minutes": 1.779,
                "windows": {
                    "10080m": {
                        "remaining_units": 0.1,
                        "planned_rate_units_per_min": 0.1,
                        "capacity_units_per_account": 1.0,
                    },
                },
            },
        }
        restock._save_state(state)
        fake = FakeClient()
        rows = [{"id": index, "status": "active", "schedulable": True} for index in range(9)]
        forecast = {
            "status": "ready",
            "eta_minutes": 45,
            "windows": {},
            "new_accounts": 0,
            "removed_accounts": 0,
        }
        next_state = {"last_sampled_at": 100, "forecast": forecast}

        with patch.object(
            restock._pool_monitor, "fetch_pool_accounts", return_value=rows
        ), patch.object(restock, "update_forecast", return_value=(next_state, forecast)) as update:
            result = restock.run_restock_cycle(client=fake)

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "forecast_not_triggered")
        self.assertTrue(result["forecast_sampled"])
        self.assertFalse(result["forecast_trigger"])
        self.assertEqual(result["quantity"], 0)
        self.assertEqual(result["quota_forecast"]["eta_minutes"], 45)
        update.assert_called_once()
        self.assertEqual(fake.inventory_calls, [])
        self.assertEqual(fake.created, [])

    def test_forecast_mode_can_order_from_current_sample(self):
        restock.save_restock_config({
            "enabled": True,
            "trigger_mode": "forecast",
            "forecast_interrupt_minutes": 20,
            "forecast_target_minutes": 30,
            "max_purchase_per_order": 5,
        })
        state = restock._load_state()
        state["quota_forecast"] = {"last_sampled_at": 0}
        restock._save_state(state)
        fake = FakeClient()
        rows = [{"id": index, "status": "active", "schedulable": True} for index in range(4)]
        forecast = {
            "status": "ready",
            "eta_minutes": 5,
            "windows": {
                "10080m": {
                    "remaining_units": 0.5,
                    "planned_rate_units_per_min": 0.1,
                    "capacity_units_per_account": 1.0,
                },
            },
            "new_accounts": 0,
            "removed_accounts": 0,
        }
        next_state = {"last_sampled_at": 100, "forecast": forecast}

        with patch.object(
            restock._pool_monitor, "fetch_pool_accounts", return_value=rows
        ), patch.object(restock, "update_forecast", return_value=(next_state, forecast)):
            result = restock.run_restock_cycle(client=fake)

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "ordered")
        self.assertTrue(result["forecast_sampled"])
        self.assertTrue(result["forecast_trigger"])
        self.assertEqual(result["quantity"], 3)
        self.assertEqual(fake.created[0][1], 3)

    def test_cycle_counts_active_accounts_after_recovery_before_ordering(self):
        restock.save_restock_config({
            "enabled": True,
            "min_healthy": 5,
            "target_healthy": 10,
            "max_purchase_per_order": 10,
        })
        fake = FakeClient()
        all_rows = [{"id": 1, "status": "error", "schedulable": False}]
        active_after_recovery = [
            {"id": index, "status": "active", "schedulable": True}
            for index in range(1, 11)
        ]
        events = []

        def fetch_accounts(**kwargs):
            events.append("active_query" if kwargs.get("status") == "active" else "all_query")
            return active_after_recovery if kwargs.get("status") == "active" else all_rows

        def process_recoveries(*args, **kwargs):
            events.append("recoveries")
            return {"scanned": True, "repaired": 1, "recreated": 0}

        with patch.object(restock._pool_monitor, "fetch_pool_accounts", side_effect=fetch_accounts), patch.object(
            restock, "_process_recoveries", side_effect=process_recoveries
        ), patch.object(restock, "_provider_configured", side_effect=lambda provider, **kwargs: provider == "sogou"):
            result = restock.run_restock_cycle(client=fake)

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "inventory_ok")
        self.assertEqual(result["healthy"], 10)
        self.assertEqual(result["quantity"], 0)
        self.assertEqual(result["recovery"]["repaired"], 1)
        self.assertEqual(events, ["all_query", "recoveries", "active_query"])
        self.assertEqual(fake.inventory_calls, [])
        self.assertEqual(fake.created, [])

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

    def test_prepared_accounts_are_named_with_provider(self):
        cfg = restock.normalize_restock_config({})
        payload = {"accounts": [{"email": "named@example.com", "access_token": _jwt("named@example.com")}]}
        bugteam = restock._build_prepared(payload, cfg, order_id="bug-1", provider="bugteam")
        sogou = restock._build_prepared(payload, cfg, order_id="sogou-1", provider="sogou")
        self.assertEqual(bugteam[0][1]["name"], "BugTeam | named@example.com")
        self.assertEqual(sogou[0][1]["name"], "Sogou | named@example.com")
        self.assertNotIn("provider", bugteam[0][1]["extra"])
        self.assertNotIn("provider", sogou[0][1]["extra"])

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

    def test_sogou_cancel_order_uses_manual_order_endpoint(self):
        client = SogouEduClient()
        with patch.object(client, "_request_json", return_value={"status": "cancelled"}) as request:
            result = client.cancel_order("123")
        request.assert_called_once_with(
            "POST", "/api/customer/manual/orders/123/cancel", retry_network=False
        )
        self.assertEqual(result["status"], "cancelled")

    def test_partial_order_waits_one_minute_before_finalize(self):
        restock.save_restock_config({"enabled": True, "order_poll_interval_sec": 1})
        state = restock._load_state()
        state["current_order"] = {
            "order_id": "partial-1",
            "quantity": 10,
            "status": "ready_partial",
            "partial_ready_since": 900,
        }
        restock._save_state(state)
        fake = FakeClient()
        with patch.object(fake, "order_status", return_value={
            "order": {"id": "partial-1", "status": "ready_partial", "reserved": 5},
            "status": "ready_partial",
        }), patch.object(restock.time, "time", return_value=959):
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["action"], "waiting")
        self.assertEqual(result["reserved"], 5)
        self.assertEqual(fake.finalize_calls, [])
        self.assertEqual(restock._load_state()["current_order"]["partial_ready_since"], 900)

    def test_partial_timer_survives_waiting_inventory_with_reservations(self):
        restock.save_restock_config({"enabled": True, "order_poll_interval_sec": 1})
        state = restock._load_state()
        state["current_order"] = {
            "order_id": "partial-2",
            "quantity": 10,
            "status": "ready_partial",
            "partial_ready_since": 900,
        }
        restock._save_state(state)
        fake = FakeClient()
        with patch.object(fake, "order_status", return_value={
            "order": {"id": "partial-2", "status": "waiting_inventory", "reserved": 5},
            "status": "waiting_inventory",
        }), patch.object(restock.time, "time", return_value=959):
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["action"], "waiting")
        self.assertEqual(result["reserved"], 5)
        self.assertEqual(fake.finalize_calls, [])
        self.assertEqual(restock._load_state()["current_order"]["partial_ready_since"], 900)

    def test_bugteam_zero_inventory_timeout_cancels_and_skips_to_next_provider(self):
        cfg = restock.normalize_restock_config({
            "provider_priority": ["bugteam", "sogou"],
            "partial_retry_limit": 2,
            "order_poll_interval_sec": 1,
        })
        state = restock._load_state()
        state["current_order"] = {
            "provider": "bugteam",
            "provider_index": 0,
            "provider_retry_count": 0,
            "order_id": "bug-wait-1",
            "quantity": 5,
            "product": "team_1h",
            "status": "waiting_inventory",
            "created_at": 1000,
        }
        fake = FakeClient()

        with patch.object(fake, "order_status", return_value={
            "order_id": "bug-wait-1",
            "state": "waiting_inventory",
            "reserved": 0,
        }), patch.object(restock.time, "time", return_value=1010), patch.object(
            restock, "_now", return_value="1970-01-01T00:16:50+00:00"
        ):
            result = restock._process_current_order(fake, cfg, state)

        self.assertEqual(fake.cancel_calls, ["bug-wait-1"])
        self.assertEqual(result["action"], "provider_fallback_scheduled")
        self.assertEqual(result["provider"], "sogou")
        self.assertEqual(result["reason"], "bugteam:waiting_inventory_timeout")
        self.assertEqual(result["cancelled_status"], "cancelled")
        current = restock._load_state()["current_order"]
        self.assertEqual(current["provider"], "sogou")
        self.assertEqual(current["provider_retry_count"], 0)
        self.assertEqual(current["quantity"], 5)
        self.assertEqual(current["created_at"], "1970-01-01T00:16:50+00:00")
        self.assertNotIn("order_id", current)

    def test_sogou_zero_inventory_timeout_cancels_and_skips_to_next_provider(self):
        cfg = restock.normalize_restock_config({
            "provider_priority": ["sogou", "bugteam"],
            "partial_retry_limit": 2,
            "order_poll_interval_sec": 1,
        })
        state = restock._load_state()
        state["current_order"] = {
            "provider": "sogou",
            "provider_index": 0,
            "provider_retry_count": 0,
            "order_id": "sogou-wait-1",
            "quantity": 5,
            "product": "oauth_7d",
            "status": "waiting_inventory",
            "created_at": 1000,
        }
        fake = FakeClient()

        with patch.object(fake, "order_status", return_value={
            "order_id": "sogou-wait-1",
            "status": "waiting_inventory",
            "reserved": 0,
        }), patch.object(restock.time, "time", return_value=1010), patch.object(
            restock, "_now", return_value="1970-01-01T00:16:50+00:00"
        ):
            result = restock._process_current_order(fake, cfg, state)

        self.assertEqual(fake.cancel_calls, ["sogou-wait-1"])
        self.assertEqual(result["action"], "provider_fallback_scheduled")
        self.assertEqual(result["provider"], "bugteam")
        self.assertEqual(result["reason"], "sogou:waiting_inventory_timeout")
        current = restock._load_state()["current_order"]
        self.assertEqual(current["provider"], "bugteam")
        self.assertEqual(current["quantity"], 5)

    def test_zero_inventory_does_not_cancel_before_ten_seconds(self):
        cfg = restock.normalize_restock_config({"order_poll_interval_sec": 1})
        state = restock._load_state()
        state["current_order"] = {
            "provider": "sogou",
            "order_id": "sogou-wait-boundary",
            "quantity": 2,
            "status": "waiting_inventory",
            "created_at": 1000,
        }
        fake = FakeClient()
        with patch.object(fake, "order_status", return_value={
            "order_id": "sogou-wait-boundary", "status": "waiting_inventory", "reserved": 0,
        }), patch.object(restock.time, "time", return_value=1009):
            result = restock._process_current_order(fake, cfg, state)
        self.assertEqual(result["action"], "waiting")
        self.assertEqual(fake.cancel_calls, [])

    def test_bugteam_partial_settles_after_one_minute_then_cancels_remaining(self):
        cfg = restock.normalize_restock_config({
            "provider_priority": ["bugteam", "sogou"],
            "partial_retry_limit": 2,
            "order_poll_interval_sec": 1,
        })
        state = restock._load_state()
        state["current_order"] = {
            "provider": "bugteam",
            "provider_index": 0,
            "provider_retry_count": 0,
            "order_id": "bug-partial-1",
            "quantity": 2,
            "product": "team_1h",
            "status": "partial",
            "partial_ready_since": 1000,
        }
        restock._save_state(state)
        fake = FakeClient()
        with patch.object(fake, "order_status", return_value={
            "order_id": "bug-partial-1",
            "state": "partial",
            "reserved": 1,
        }), patch.object(fake, "take_order", return_value={
            "accounts": [{"email": "bug-partial@example.com", "access_token": _jwt("bug-partial@example.com")}]
        }), patch.object(restock, "push_prepared_accounts_to_pool", return_value={"success": 1, "failed": 0}), patch.object(
            restock.time, "time", return_value=1060
        ):
            result = restock._process_current_order(fake, cfg, state)

        self.assertEqual(fake.take_calls, 0)
        self.assertEqual(fake.cancel_calls, ["bug-partial-1"])
        self.assertEqual(result["action"], "provider_retry_scheduled")
        current = restock._load_state()["current_order"]
        self.assertEqual(current["provider"], "bugteam")
        self.assertEqual(current["quantity"], 1)

    def test_bugteam_partial_waits_before_one_minute(self):
        cfg = restock.normalize_restock_config({"order_poll_interval_sec": 1})
        state = restock._load_state()
        state["current_order"] = {
            "provider": "bugteam",
            "order_id": "bug-partial-2",
            "quantity": 2,
            "status": "partial",
            "partial_ready_since": 1000,
        }
        restock._save_state(state)
        fake = FakeClient()
        with patch.object(fake, "order_status", return_value={
            "order_id": "bug-partial-2", "state": "partial", "reserved": 1,
        }), patch.object(restock.time, "time", return_value=1059):
            result = restock._process_current_order(fake, cfg, state)
        self.assertEqual(result["action"], "waiting")
        self.assertEqual(fake.take_calls, 0)
        self.assertEqual(fake.cancel_calls, [])

    def test_partial_order_finalizes_after_one_minute(self):
        restock.save_restock_config({"enabled": True, "order_poll_interval_sec": 1})
        state = restock._load_state()
        state["current_order"] = {
            "order_id": "partial-3",
            "quantity": 10,
            "status": "ready_partial",
            "partial_ready_since": 900,
        }
        restock._save_state(state)
        fake = FakeClient()
        with patch.object(fake, "order_status", return_value={
            "order": {"id": "partial-3", "status": "ready_partial", "reserved": 5},
            "status": "ready_partial",
        }), patch.object(fake, "finalize_order", side_effect=lambda order_id: (
            fake.finalize_calls.append(order_id)
            or {"order": {"id": order_id, "status": "completed", "reserved": 6}, "status": "completed"}
        )), patch.object(restock.time, "time", return_value=960):
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["action"], "partial_finalized")
        self.assertEqual(result["reserved_before_finalize"], 5)
        self.assertEqual(result["reserved"], 6)
        self.assertEqual(fake.finalize_calls, ["partial-3"])
        self.assertEqual(fake.take_calls, 0)
        current = restock._load_state()["current_order"]
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["reserved"], 6)
        self.assertNotIn("partial_ready_since", current)

    def test_partial_finalized_exhausted_order_is_cleared(self):
        restock.save_restock_config({"enabled": True, "order_poll_interval_sec": 1})
        state = restock._load_state()
        state["current_order"] = {
            "order_id": "partial-exhausted",
            "quantity": 10,
            "status": "partial",
            "reserved": 4,
            "partial_finalized_at": "2026-08-14T00:00:00+00:00",
        }
        restock._save_state(state)
        fake = FakeClient()
        failed_items = [
            {
                "health_status": "failed",
                "reauthorization_status": "failed",
                "replacement_status": "failed",
            }
            for _ in range(4)
        ]
        with patch.object(fake, "order_status", return_value={
            "order": {
                "id": "partial-exhausted",
                "status": "partial",
                "reserved": 4,
                "items": failed_items,
            },
            "status": "partial",
        }):
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["action"], "order_failed")
        self.assertEqual(result["status"], "partial_exhausted")
        self.assertEqual(result["reason"], "部分结算后无可交付账号")
        self.assertIsNone(restock._load_state()["current_order"])
        self.assertEqual(fake.take_calls, 0)

    def test_partial_finalized_order_with_normal_item_is_taken_and_pushed(self):
        restock.save_restock_config({"enabled": True, "order_poll_interval_sec": 1})
        state = restock._load_state()
        state["current_order"] = {
            "order_id": "partial-keep",
            "quantity": 2,
            "status": "partial",
            "reserved": 1,
            "partial_finalized_at": "2026-08-14T00:00:00+00:00",
        }
        restock._save_state(state)
        fake = FakeClient()
        with patch.object(fake, "order_status", return_value={
            "order": {
                "id": "partial-keep",
                "status": "partial",
                "reserved": 1,
                "items": [{"health_status": "live_team"}],
            },
            "status": "partial",
        }), patch.object(
            restock, "push_prepared_accounts_to_pool", return_value={"success": 1, "failed": 0}
        ) as pushed:
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["action"], "pushed")
        self.assertEqual(fake.take_calls, 1)
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
        restock.save_restock_config({
            "enabled": True,
            "min_healthy": 0,
            "target_healthy": 0,
            "model_whitelist": ["gpt-5.5", "gpt-5.6-sol"],
        })
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
        self.assertEqual(updated.call_args.args[1]["credentials"]["model_mapping"], {
            "gpt-5.5": "gpt-5.5",
            "gpt-5.6-sol": "gpt-5.6-sol",
        })
        self.assertIsNone(listed.call_args.kwargs["before_id"])
        self.assertEqual(restock._load_state()["recovery_cursor"], "page-2")

    def test_recovery_matches_original_account_through_source_order(self):
        restock.save_restock_config({"enabled": True, "min_healthy": 0, "target_healthy": 0})
        fake = FakeClient()
        recovery = {
            "id": 27727,
            "inventory_id": 145291,
            "source_order_id": 68525,
            "delivery_status": "claimable",
            "claim_url": "/signed/claim",
        }
        order_detail = {"order": {"id": 68525, "items": [{
            "inventory_account_id": 145291,
            "recovery_id": 27727,
            "email": "original@example.com",
        }]}}
        claim_response = {"data": {"payload": {"accounts": [{
            "email": "original@example.com",
            "access_token": _jwt("original@example.com"),
        }]}}}
        existing = [{
            "id": 81,
            "name": "original@example.com",
            "status": "error",
            "extra": {
                "import_source": "sogouedu_auto_restock",
                "sogou_order_id": "68525",
            },
        }]
        with patch.object(fake, "list_recoveries", return_value={"data": {"items": [recovery]}}), patch.object(
            fake, "order_status", return_value=order_detail
        ) as order_status, patch.object(
            fake, "claim_recovery", return_value=claim_response
        ), patch.object(
            restock._pool_monitor, "fetch_pool_accounts", side_effect=[existing, []]
        ), patch.object(
            restock._pool_monitor, "_update_pool_credentials"
        ) as updated, patch.object(
            restock, "push_prepared_accounts_to_pool"
        ) as pushed:
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["recovery"]["repaired"], 1)
        self.assertEqual(result["recovery"]["recreated"], 0)
        order_status.assert_called_once_with("68525")
        updated.assert_called_once()
        self.assertEqual(updated.call_args.args[0], 81)
        pushed.assert_not_called()
        saved = restock._read_json(restock.RECOVERIES_PATH, [])
        row = next(x for x in saved if str(x.get("id")) == "27727")
        self.assertEqual(row["email"], "original@example.com")
        self.assertEqual(row["matched_by"], "source_order")

    def test_recovery_claim_email_is_fallback_for_original_account(self):
        restock.save_restock_config({"enabled": True, "min_healthy": 0, "target_healthy": 0})
        fake = FakeClient()
        recovery = {"id": "r-email", "delivery_status": "claimable"}
        claim_response = {"data": {"payload": {"accounts": [{
            "email": "fallback@example.com",
            "access_token": _jwt("fallback@example.com"),
        }]}}}
        existing = [{
            "id": 82,
            "name": "fallback@example.com",
            "status": "error",
            "extra": {"import_source": "sogouedu_auto_restock"},
        }]
        with patch.object(fake, "list_recoveries", return_value={"data": {"items": [recovery]}}), patch.object(
            fake, "claim_recovery", return_value=claim_response
        ), patch.object(
            restock._pool_monitor, "fetch_pool_accounts", side_effect=[existing, []]
        ), patch.object(
            restock._pool_monitor, "_update_pool_credentials"
        ) as updated, patch.object(
            restock, "push_prepared_accounts_to_pool"
        ) as pushed:
            result = restock.run_restock_cycle(client=fake)

        self.assertEqual(result["recovery"]["repaired"], 1)
        updated.assert_called_once()
        self.assertEqual(updated.call_args.args[0], 82)
        pushed.assert_not_called()

    def test_recovery_order_email_falls_back_to_inventory_id(self):
        recovery = {"id": "different", "inventory_id": 145291}
        order_detail = {"order": {"items": [{
            "inventory_account_id": 145291,
            "recovery_id": 27727,
            "email": "inventory-match@example.com",
        }]}}

        self.assertEqual(
            restock._recovery_order_email(recovery, order_detail),
            "inventory-match@example.com",
        )

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

    def test_processed_recovery_is_not_claimed_again(self):
        restock.save_restock_config({
            "enabled": True,
            "min_healthy": 0,
            "target_healthy": 0,
            "recovery_poll_interval_sec": 1,
        })
        fake = FakeClient()
        recovery = {
            "id": "bugteam-repaired",
            "provider": "bugteam",
            "pool_id": "42",
            "delivery_status": "claimable",
            "claim_url": "/signed/claim",
        }
        claim_response = {"data": {"payload": {"accounts": [{
            "email": "fixed@example.com",
            "access_token": _jwt("fixed@example.com"),
        }]}}}
        existing = [{
            "id": 42,
            "status": "error",
            "extra": {"import_source": "bugteam_auto_restock"},
            "credentials": {"email": "fixed@example.com"},
        }]
        state = restock._load_state()
        with patch.object(fake, "list_recoveries", return_value={"data": {"items": [recovery]}}), patch.object(
            fake, "claim_recovery", return_value=claim_response
        ) as claim, patch.object(
            restock._pool_monitor, "_update_pool_credentials"
        ) as updated, patch.object(
            restock.time, "time", return_value=100
        ):
            first = restock._process_recoveries(fake, restock.load_restock_config(), state, existing, provider="bugteam")

        self.assertEqual(first["repaired"], 1)
        self.assertEqual(claim.call_count, 1)
        self.assertEqual(updated.call_count, 1)

        state = restock._load_state()
        state["last_recovery_scan_at_bugteam"] = 0
        with patch.object(restock.time, "time", return_value=200):
            second = restock._process_recoveries(fake, restock.load_restock_config(), state, existing, provider="bugteam")

        self.assertEqual(second["repaired"], 0)
        self.assertEqual(second["recreated"], 0)
        self.assertEqual(claim.call_count, 1)
        self.assertEqual(updated.call_count, 1)
        saved = restock._read_json(restock.RECOVERIES_PATH, [])
        row = next(item for item in saved if item.get("id") == "bugteam-repaired")
        self.assertEqual(row["result"], "repaired")
        self.assertNotIn("last_error", row)

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

    def test_partial_delivery_retries_same_provider_twice_then_falls_back(self):
        cfg = restock.normalize_restock_config({
            "provider_priority": ["bugteam", "sogou"],
            "partial_retry_limit": 2,
        })
        state = restock._load_state()
        state["current_order"] = {
            "provider": "bugteam",
            "provider_index": 0,
            "provider_retry_count": 0,
            "order_id": "bugteam-1",
            "quantity": 4,
            "status": "completed",
            "remote_status": "cancelled",
            "reserved": 1,
            "remaining_quantity": 3,
            "delivered_quantity": 1,
            "last_error": "cancelled",
            "last_push": {"success": 1, "failed": 0},
            "status_url": "/old-status",
            "take_url": "/old-take",
        }
        restock._save_state(state)

        first = restock._schedule_followup_order(
            state["current_order"], cfg, state, remaining=3, reason="bugteam:partial_delivery:1/4"
        )
        self.assertEqual(first["action"], "provider_retry_scheduled")
        self.assertEqual(first["provider"], "bugteam")
        self.assertEqual(first["provider_retry_count"], 1)
        current = restock._load_state()["current_order"]
        for stale_key in (
            "remote_status", "reserved", "remaining_quantity", "delivered_quantity",
            "last_error", "last_push", "status_url", "take_url",
        ):
            self.assertNotIn(stale_key, current)

        state["current_order"]["order_id"] = "bugteam-2"
        second = restock._schedule_followup_order(
            state["current_order"], cfg, state, remaining=2, reason="bugteam:partial_delivery:1/3"
        )
        self.assertEqual(second["action"], "provider_retry_scheduled")
        self.assertEqual(second["provider_retry_count"], 2)

        state["current_order"]["order_id"] = "bugteam-3"
        fallback = restock._schedule_followup_order(
            state["current_order"], cfg, state, remaining=1, reason="bugteam:failed"
        )
        self.assertEqual(fallback["action"], "provider_fallback_scheduled")
        self.assertEqual(fallback["provider"], "sogou")
        self.assertEqual(fallback["provider_retry_count"], 0)
        current = restock._load_state()["current_order"]
        self.assertEqual(current["quantity"], 1)
        self.assertEqual(current["product"], "oauth_7d")

        reverse_cfg = restock.normalize_restock_config({
            "provider_priority": ["sogou", "bugteam"],
            "bugteam_product": "team_1h",
            "partial_retry_limit": 2,
        })
        reverse_state = restock._load_state()
        reverse_state["current_order"] = {
            "provider": "sogou",
            "provider_retry_count": 2,
            "order_id": "sogou-3",
            "quantity": 2,
            "product": "oauth_7d",
        }
        reverse = restock._schedule_followup_order(
            reverse_state["current_order"], reverse_cfg, reverse_state, remaining=1, reason="sogou:failed"
        )
        self.assertEqual(reverse["provider"], "bugteam")
        self.assertEqual(restock._load_state()["current_order"]["product"], "team_1h")

    def test_order_table_distinguishes_ordered_delivered_and_remaining(self):
        source = (
            Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("<th>下单</th><th>成功</th><th>剩余</th>", source)
        self.assertIn("row.delivered_quantity == null", source)
        self.assertIn("status === 'pushed' ? ordered : 0", source)
        self.assertIn("const remaining = status === 'pushed'", source)
        self.assertIn("const error = status === 'pushed' ? ''", source)
        self.assertIn("provider_retry_scheduled: '同供应商重试'", source)
        self.assertIn("结算前预留 ${row.reserved_before_finalize}", source)
        self.assertIn("最终结算 ${row.reserved} 个", source)

    def test_bugteam_completed_download_uses_same_pool_push_path(self):
        class BugTeamFake:
            def __init__(self):
                self.created = []
                self.sequence = 0

            def balance(self):
                return {"balance_fen": 10000}

            def inventory(self, product, quantity):
                return {"product": product, "available": quantity}

            def create_order(self, product, quantity, *, idempotency_key):
                self.sequence += 1
                order_id = f"bugteam-{self.sequence}"
                self.created.append((product, quantity, idempotency_key))
                return {"order": {"order_id": order_id, "state": "pending"}}

            def order_status(self, order_id, *, status_url=None):
                return {"order": {"order_id": order_id, "state": "completed", "delivered_quantity": 1}}

            def take_order(self, order_id, *, take_url=None):
                return {"accounts": [{"email": "bugteam@example.com", "access_token": _jwt("bugteam@example.com")}]}

            def list_recoveries(self, *, before_id=None, limit=100):
                return {"recoveries": []}

        restock.save_restock_config({
            "enabled": True,
            "min_healthy": 1,
            "target_healthy": 2,
            "max_purchase_per_order": 2,
            "provider_priority": ["bugteam", "sogou"],
            "bugteam_product": "team_1h",
            "partial_retry_limit": 2,
        })
        fake = BugTeamFake()
        with patch.object(restock._pool_monitor, "fetch_pool_accounts", return_value=[]), patch.object(
            restock, "_provider_configured", side_effect=lambda provider, **kwargs: provider == "bugteam"
        ), patch.object(restock, "_new_provider_client", return_value=fake), patch.object(
            restock, "push_prepared_accounts_to_pool", return_value={"success": 1, "failed": 0}
        ) as pushed:
            first = restock.run_restock_cycle()
            self.assertEqual(first["action"], "ordered")
            second = restock.run_restock_cycle()

        self.assertEqual(second["action"], "provider_retry_scheduled")
        self.assertEqual(second["provider"], "bugteam")
        self.assertEqual(second["remaining"], 1)
        self.assertEqual(fake.created[0][0:2], ("team_1h", 2))
        pushed.assert_called_once()

        history = restock._read_json(restock.ORDERS_PATH, [])
        entry = next(row for row in history if row.get("order_id") == "bugteam-1")
        self.assertEqual(entry["status"], "provider_retry_scheduled")
        self.assertEqual(entry["remote_status"], "completed")
        self.assertEqual(entry["delivered_quantity"], 1)
        pushed_account = pushed.call_args.args[0][0][1]
        self.assertEqual(pushed_account["name"], "BugTeam | bugteam@example.com")

    def test_recovery_backlog_is_isolated_by_provider(self):
        cfg = restock.normalize_restock_config({"recovery_poll_interval_sec": 1})
        restock._write_json(restock.RECOVERIES_PATH, [
            {"id": "sogou-old", "status": "claimable", "provider": "sogou"},
            {"id": "bugteam-ready", "state": "claimable", "provider": "bugteam"},
        ])
        fake = FakeClient()
        with patch.object(fake, "list_recoveries", return_value={"recoveries": []}), patch.object(
            fake, "claim_recovery", return_value={"accounts": [{
                "email": "bugteam-fix@example.com",
                "access_token": _jwt("bugteam-fix@example.com"),
            }]}
        ) as claimed, patch.object(
            restock, "push_prepared_accounts_to_pool", return_value={"success": 1, "failed": 0}
        ):
            result = restock._process_recoveries(fake, cfg, restock._load_state(), [], provider="bugteam")

        self.assertEqual(result["recreated"], 1)
        claimed.assert_called_once()
        self.assertEqual(claimed.call_args.args[0], "bugteam-ready")
        saved = restock._read_json(restock.RECOVERIES_PATH, [])
        self.assertTrue(any(row.get("id") == "sogou-old" for row in saved))


if __name__ == "__main__":
    unittest.main()
