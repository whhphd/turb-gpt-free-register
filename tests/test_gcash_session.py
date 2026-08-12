# -*- coding: utf-8 -*-
import inspect
import unittest
from unittest.mock import patch, MagicMock

from core import gcash_session as gs
from core import registration_service as reg_svc
from core.cloakbrowser_registration import run_cloak_registration
import main as main_mod


class GcashSessionUnitTests(unittest.TestCase):
    def setUp(self):
        with gs._LOCK:
            gs._SESSION = None

    def tearDown(self):
        with gs._LOCK:
            if gs._SESSION and gs._SESSION.get("driver"):
                try:
                    gs._SESSION["driver"].quit()
                except Exception:
                    pass
            gs._SESSION = None

    def test_cloak_registration_accepts_keep_and_skip_flags(self):
        sig = inspect.signature(run_cloak_registration)
        self.assertIn("keep_browser", sig.parameters)
        self.assertIn("skip_codex", sig.parameters)

    def test_run_registration_accepts_keep_and_skip_flags(self):
        sig = inspect.signature(main_mod.run_registration)
        self.assertIn("keep_browser", sig.parameters)
        self.assertIn("skip_codex", sig.parameters)

    def test_execute_registration_exists_for_gcash(self):
        # GCash 第一步应走普通注册 + 重试入口
        self.assertTrue(callable(reg_svc.execute_registration))
        src = inspect.getsource(gs._owner_loop)
        self.assertIn("execute_registration", src)
        self.assertNotIn("run_cloak_registration(", src)

    def test_execute_registration_retries_transient_failure(self):
        calls = {"n": 0}

        def fake_run(**kwargs):
            calls["n"] += 1
            self.assertTrue(kwargs.get("keep_browser"))
            self.assertTrue(kwargs.get("skip_codex"))
            if calls["n"] == 1:
                return {"success": False, "email": "a@b.com", "error": "Page.goto: net::ERR_SSL_PROTOCOL_ERROR"}
            return {
                "success": True,
                "email": "a@b.com",
                "account_id": 9,
                "access_token": "tok",
                "driver": object(),
                "opened": object(),
            }

        with patch.object(reg_svc, "_registration_retry_settings", return_value=(2, 0.0)), \
             patch.object(reg_svc, "_prepare_registration_args", return_value=("a@b.com", "Name", "1990-01-01")), \
             patch("main.run_registration", side_effect=fake_run):
            result = reg_svc.execute_registration(keep_browser=True, skip_codex=True)
        self.assertTrue(result.get("success"))
        self.assertEqual(calls["n"], 2)
        self.assertEqual(result.get("attempts"), 2)

    def test_public_view_idle(self):
        view = gs.get_session()
        self.assertFalse(view.get("active"))
        self.assertEqual(view.get("status"), "idle")

    def test_start_rejects_non_cloak_driver(self):
        with patch("config.roxybrowser.REGISTRATION_DRIVER", "protocol"):
            r = gs.start_session()
        self.assertFalse(r.get("ok"))
        self.assertIn("cloak", str(r.get("error") or "").lower())

    def test_open_url_requires_session(self):
        r = gs.open_payment_url("https://example.com/pay")
        self.assertFalse(r.get("ok"))

    def test_get_access_token_ready(self):
        with gs._LOCK:
            gs._SESSION = {
                "session_id": "abc",
                "status": "ready_at",
                "email": "a@b.com",
                "account_id": 1,
                "access_token": "sk-test-token-value",
                "logs": [],
            }
        r = gs.get_access_token()
        self.assertTrue(r.get("ok"))
        self.assertEqual(r.get("access_token"), "sk-test-token-value")


if __name__ == "__main__":
    unittest.main()
