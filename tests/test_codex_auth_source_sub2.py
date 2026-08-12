# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import codex as codex_config
from config import sub2api as sub2api_config
from core import codex_oauth


class CodexAuthSourceSub2Tests(unittest.TestCase):
    def test_auto_fallback_cpa_to_local_when_cpa_key_missing(self):
        with patch.object(codex_config, "CODEX_AUTH_URL_SOURCE", "cpa"), \
             patch.object(codex_config, "CPA_MANAGEMENT_KEY", ""), \
             patch.object(codex_config, "CPA_MANAGEMENT_URL", "http://127.0.0.1:8317"):
            self.assertEqual(codex_oauth._codex_auth_url_source(), "local")

    def test_sub2_alias_normalized(self):
        with patch.object(codex_config, "CODEX_AUTH_URL_SOURCE", "sub2api"), \
             patch.object(sub2api_config, "SUB2API_API_BASE", "https://sub.callai.one"), \
             patch.object(sub2api_config, "SUB2API_API_KEY", "admin-test-key"):
            self.assertEqual(codex_oauth._codex_auth_url_source(), "sub2")

    def test_request_sub2_authorize_url_parses_envelope(self):
        payload = {
            "code": 0,
            "message": "success",
            "data": {
                "auth_url": (
                    "https://auth.openai.com/oauth/authorize?client_id=app_x"
                    "&state=abc123state&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback"
                ),
                "session_id": "sess-1",
            },
        }
        with patch.object(sub2api_config, "SUB2API_API_BASE", "https://sub.callai.one"), \
             patch.object(sub2api_config, "SUB2API_API_KEY", "admin-test-key"), \
             patch.object(codex_oauth, "_sub2_codex_request_json", return_value=payload):
            result = codex_oauth._request_sub2_authorize_url()
        self.assertEqual(result["session_id"], "sess-1")
        self.assertEqual(result["state"], "abc123state")
        self.assertTrue(result["auth_url"].startswith("https://auth.openai.com/"))

    def test_submit_sub2_callback_create_from_oauth_body(self):
        captured = {}

        def fake_request(method, path, body=None):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            return {"code": 0, "message": "success", "data": {"id": 1, "email": "a@b.com"}}

        callback = "http://localhost:1455/auth/callback?code=CODE123&state=STATE456"
        with patch.object(sub2api_config, "SUB2API_API_BASE", "https://sub.callai.one"), \
             patch.object(sub2api_config, "SUB2API_API_KEY", "admin-test-key"), \
             patch.object(sub2api_config, "SUB2_CODEX_CALLBACK_PAYLOAD_MODE", "create_from_oauth"), \
             patch.object(codex_oauth, "_sub2_codex_request_json", side_effect=fake_request):
            codex_oauth._submit_sub2_callback(
                callback,
                session_id="sess-9",
                redirect_uri="http://localhost:1455/auth/callback",
                name="a@b.com",
            )
        self.assertEqual(captured["method"], "POST")
        self.assertIn("create-from-oauth", captured["path"])
        self.assertEqual(captured["body"]["session_id"], "sess-9")
        self.assertEqual(captured["body"]["code"], "CODE123")
        self.assertEqual(captured["body"]["state"], "STATE456")
        self.assertEqual(captured["body"]["name"], "a@b.com")
        self.assertEqual(captured["body"]["redirect_uri"], "http://localhost:1455/auth/callback")


if __name__ == "__main__":
    unittest.main()
