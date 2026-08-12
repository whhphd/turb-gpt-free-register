# -*- coding: utf-8 -*-
import unittest
import importlib.util
from unittest.mock import patch

if importlib.util.find_spec("flask") is not None:
    from webui.app import create_app
else:  # pragma: no cover - CI without WebUI optional dependencies
    create_app = None


@unittest.skipUnless(create_app is not None, "WebUI API tests require Flask")
class SogouRestockApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(auth_code="test-auth")
        cls.app.testing = True

    def setUp(self):
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def test_status_is_redacted_and_starts_monitor(self):
        with patch("core.sogouedu_restock.ensure_restock_monitor_started") as started, patch(
            "core.sogouedu_restock.get_restock_status",
            return_value={"config": {"enabled": False}, "credentials_configured": True, "current_order": None},
        ):
            response = self.client.get("/api/pool-admin/sogou-restock", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["credentials_configured"])
        started.assert_called_once()

    def test_config_route_ignores_sensitive_fields_and_returns_no_password(self):
        with patch("core.sogouedu_restock.save_restock_config", return_value={"enabled": True}) as save, patch(
            "core.sogouedu_restock.ensure_restock_monitor_started"
        ):
            response = self.client.post(
                "/api/pool-admin/sogou-restock/config",
                json={"enabled": True, "password": "should-not-be-used", "SOGOUEDU_PASSWORD": "secret"},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200)
        save.assert_called_once()
        self.assertNotIn("secret", response.get_data(as_text=True))

    def test_run_orders_logs_routes(self):
        with patch(
            "core.sogouedu_restock.trigger_restock_run_now",
            return_value={"ok": True, "action": "inventory_ok"},
        ):
            run = self.client.post("/api/pool-admin/sogou-restock/run", headers=self.headers)
        self.assertEqual(run.status_code, 200)
        self.assertEqual(run.get_json()["action"], "inventory_ok")
        with patch("core.sogouedu_restock.list_restock_orders", return_value=[]), patch(
            "core.sogouedu_restock.get_restock_log_tail", return_value=[]
        ):
            self.assertEqual(self.client.get("/api/pool-admin/sogou-restock/orders", headers=self.headers).status_code, 200)
            self.assertEqual(self.client.get("/api/pool-admin/sogou-restock/logs", headers=self.headers).status_code, 200)


if __name__ == "__main__":
    unittest.main()
