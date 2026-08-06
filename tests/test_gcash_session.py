# -*- coding: utf-8 -*-
import inspect
import unittest
from unittest.mock import patch, MagicMock

from core import gcash_session as gs
from core.cloakbrowser_registration import run_cloak_registration


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
