# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch, MagicMock

from core import codex_retry_service as svc


class CodexRetryWorkerTests(unittest.TestCase):
    def setUp(self):
        svc.release("a@b.com")

    def tearDown(self):
        svc.release("a@b.com")

    def test_should_fingerprint_after_protocol_retries(self):
        with patch("config.codex.CODEX_OAUTH_DRIVER", "protocol"):
            self.assertTrue(svc._should_fingerprint_after_protocol_retries({
                "ok": False,
                "status": "failed",
                "message": "RuntimeError: chatgpt-login status=403, body=Just a moment",
            }))
            self.assertFalse(svc._should_fingerprint_after_protocol_retries({
                "ok": False, "status": "failed", "message": "timeout",
            }))
        with patch("config.codex.CODEX_OAUTH_DRIVER", "cloak"):
            self.assertFalse(svc._should_fingerprint_after_protocol_retries({
                "ok": False,
                "status": "failed",
                "message": "chatgpt-login status=403",
            }))

    def test_is_terminal_codex_failure(self):
        self.assertTrue(svc._is_terminal_codex_failure({"ok": True, "status": "success"}))
        self.assertTrue(svc._is_terminal_codex_failure({"ok": False, "status": "deactivated"}))
        self.assertTrue(svc._is_terminal_codex_failure({"ok": False, "status": "stopped"}))
        self.assertTrue(svc._is_terminal_codex_failure({
            "ok": False, "status": "failed", "message": "AccountUnusableError: account_deactivated"
        }))
        self.assertFalse(svc._is_terminal_codex_failure({
            "ok": False, "status": "failed", "message": "Page.goto: net::ERR_SSL_PROTOCOL_ERROR"
        }))

    def test_run_worker_retries_transient_then_succeeds(self):
        calls = {"n": 0}

        def fake_oauth(email, force=True):
            calls["n"] += 1
            if calls["n"] < 3:
                return {"ok": False, "status": "failed", "message": "net::ERR_SSL_PROTOCOL_ERROR"}
            return {"ok": True, "status": "success", "file_path": "x.json", "callback_url": "http://cb"}

        self.assertTrue(svc.reserve("a@b.com"))
        with patch("core.codex_oauth.run_codex_oauth", side_effect=fake_oauth), \
             patch.object(svc.db, "update_account_codex_status", return_value=True), \
             patch.object(svc, "_codex_retry_settings", return_value=(3, 0.0)):
            result = svc.run_worker("a@b.com", clear_log=True, max_retries=3)
        self.assertTrue(result.get("ok"))
        self.assertEqual(calls["n"], 3)
        self.assertEqual(result.get("attempts"), 3)

    def test_run_worker_does_not_retry_deactivated(self):
        calls = {"n": 0}

        def fake_oauth(email, force=True):
            calls["n"] += 1
            return {"ok": False, "status": "deactivated", "message": "account_deactivated"}

        self.assertTrue(svc.reserve("a@b.com"))
        with patch("core.codex_oauth.run_codex_oauth", side_effect=fake_oauth), \
             patch.object(svc.db, "update_account_codex_status", return_value=True), \
             patch.object(svc, "_codex_retry_settings", return_value=(3, 0.0)):
            result = svc.run_worker("a@b.com", clear_log=True, max_retries=3)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status"), "deactivated")
        self.assertEqual(calls["n"], 1)

    def test_run_worker_exhausts_retries(self):
        calls = {"n": 0}

        def fake_oauth(email, force=True):
            calls["n"] += 1
            return {"ok": False, "status": "failed", "message": "timeout"}

        self.assertTrue(svc.reserve("a@b.com"))
        with patch("core.codex_oauth.run_codex_oauth", side_effect=fake_oauth), \
             patch.object(svc.db, "update_account_codex_status", return_value=True), \
             patch.object(svc, "_codex_retry_settings", return_value=(3, 0.0)):
            result = svc.run_worker("a@b.com", clear_log=True, max_retries=3)
        self.assertFalse(result.get("ok"))
        self.assertEqual(calls["n"], 4)  # 1 + 3 retries
        self.assertEqual(result.get("attempts"), 4)

    def test_run_worker_fingerprints_only_after_protocol_retries_exhausted(self):
        calls = []

        def fake_oauth(email, force=True, fingerprint_fallback=False, **kwargs):
            calls.append(bool(fingerprint_fallback))
            if fingerprint_fallback:
                return {"ok": True, "status": "success", "file_path": "x.json", "callback_url": "http://cb"}
            return {
                "ok": False,
                "status": "failed",
                "message": "RuntimeError: chatgpt-login status=403, body=Just a moment",
            }

        self.assertTrue(svc.reserve("a@b.com"))
        with patch("core.codex_oauth.run_codex_oauth", side_effect=fake_oauth), \
             patch.object(svc.db, "update_account_codex_status", return_value=True), \
             patch.object(svc, "_codex_retry_settings", return_value=(3, 0.0)), \
             patch.object(svc, "_should_fingerprint_after_protocol_retries", return_value=True):
            result = svc.run_worker("a@b.com", clear_log=True, max_retries=3)
        self.assertTrue(result.get("ok"))
        self.assertEqual(calls, [False, False, False, False, True])
        self.assertTrue(result.get("fingerprint_fallback"))

    def test_run_worker_timeout_does_not_fingerprint(self):
        calls = []

        def fake_oauth(email, force=True, fingerprint_fallback=False, **kwargs):
            calls.append(bool(fingerprint_fallback))
            return {"ok": False, "status": "failed", "message": "timeout"}

        self.assertTrue(svc.reserve("a@b.com"))
        with patch("core.codex_oauth.run_codex_oauth", side_effect=fake_oauth), \
             patch.object(svc.db, "update_account_codex_status", return_value=True), \
             patch.object(svc, "_codex_retry_settings", return_value=(3, 0.0)), \
             patch("config.codex.CODEX_OAUTH_DRIVER", "protocol"):
            result = svc.run_worker("a@b.com", clear_log=True, max_retries=3)
        self.assertFalse(result.get("ok"))
        self.assertEqual(calls, [False, False, False, False])
        self.assertNotIn(True, calls)


if __name__ == "__main__":
    unittest.main()
