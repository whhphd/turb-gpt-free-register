# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from config import codex as codex_config
from core import codex_oauth


class CodexLocalOauthTests(unittest.TestCase):
    def test_default_auth_source_is_local(self):
        with patch.object(codex_config, "CODEX_AUTH_URL_SOURCE", "local"):
            self.assertEqual(codex_oauth._codex_auth_url_source(), "local")

    def test_sub2_without_key_falls_back_to_local(self):
        with patch.object(codex_config, "CODEX_AUTH_URL_SOURCE", "sub2"), \
             patch.object(codex_oauth, "_sub2_configured", return_value=False):
            self.assertEqual(codex_oauth._codex_auth_url_source(), "local")

    def test_cpa_without_key_falls_back_to_local(self):
        with patch.object(codex_config, "CODEX_AUTH_URL_SOURCE", "cpa"), \
             patch.object(codex_oauth, "_cpa_configured", return_value=False):
            self.assertEqual(codex_oauth._codex_auth_url_source(), "local")

    def test_build_codex_storage_includes_export_fields(self):
        token_resp = {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "id_token": "id-1",
            "expires_in": 3600,
        }
        claims = {"email": "a@b.com", "account_id": "acc-1", "plan_type": "plus"}
        storage = codex_oauth.build_codex_storage(token_resp, claims)
        self.assertEqual(storage["type"], "codex")
        self.assertEqual(storage["access_token"], "at-1")
        self.assertEqual(storage["refresh_token"], "rt-1")
        self.assertEqual(storage["email"], "a@b.com")
        self.assertEqual(storage["plan_type"], "plus")
        self.assertEqual(storage["chatgpt_account_id"], "acc-1")
        self.assertTrue(storage.get("client_id"))

    def test_complete_local_codex_oauth_saves_credentials(self):
        token_resp = {
            "access_token": "at-xyz",
            "refresh_token": "rt-xyz",
            "id_token": "hdr.payload.sig",
            "expires_in": 7200,
        }
        fake_session = MagicMock()
        with patch.object(codex_oauth, "BrowserSession", return_value=fake_session), \
             patch.object(codex_oauth, "exchange_codex_token", return_value=token_resp) as m_ex, \
             patch.object(codex_oauth, "_parse_id_token", return_value={
                 "email": "u@example.com",
                 "account_id": "acc-9",
                 "plan_type": "plus",
             }), \
             patch.object(codex_oauth, "save_codex_credential", return_value=codex_oauth.Path("/tmp/codex-u@example.com-plus.json")) as m_save:
            done = codex_oauth.complete_local_codex_oauth(
                email="u@example.com",
                code="ac_test",
                code_verifier="verifier",
                callback_url="http://localhost:1455/auth/callback?code=ac_test&state=s",
            )
        m_ex.assert_called_once()
        self.assertEqual(done["email"], "u@example.com")
        self.assertIn("plus", str(done["path"]))
        saved_storage = m_save.call_args.args[0]
        self.assertEqual(saved_storage["refresh_token"], "rt-xyz")
        self.assertEqual(saved_storage["access_token"], "at-xyz")
        self.assertEqual(saved_storage["plan_type"], "plus")

    def test_offline_export_from_local_credential_has_tokens(self):
        local = {
            "type": "codex",
            "email": "u@example.com",
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "id_token": "id-1",
            "account_id": "acc-1",
            "plan_type": "plus",
            "client_id": "app_x",
        }
        payload, meta = codex_oauth.download_sub2api_export_for_local(local, local_filename="codex-u@example.com-plus.json")
        self.assertTrue(meta["has_refresh_token"])
        self.assertTrue(meta["has_access_token"])
        acc = payload["accounts"][0]
        self.assertEqual(acc["credentials"]["refresh_token"], "rt-1")
        self.assertEqual(acc["platform"], "openai")
        self.assertEqual(acc["type"], "oauth")


if __name__ == "__main__":
    unittest.main()
