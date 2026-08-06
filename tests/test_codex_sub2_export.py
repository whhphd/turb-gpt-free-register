# -*- coding: utf-8 -*-
import unittest

from core import codex_oauth


class CodexSub2ExportTests(unittest.TestCase):
    def test_extract_sub2_account_ref_from_callback_receipt(self):
        local = {
            "type": "codex_sub2_callback",
            "email": "a@b.com",
            "sub2_submit_response": {
                "code": 0,
                "data": {
                    "id": 13564,
                    "name": "a@b.com",
                    "credentials": {"email": "a@b.com", "plan_type": "plus"},
                },
            },
        }
        ref = codex_oauth.extract_sub2_account_ref(local)
        self.assertEqual(ref["id"], 13564)
        self.assertEqual(ref["email"], "a@b.com")
        self.assertEqual(ref["plan"], "plus")

    def test_offline_convert_sub2_callback_without_live_api(self):
        local = {
            "type": "codex_sub2_callback",
            "email": "a@b.com",
            "sub2_submit_response": {
                "code": 0,
                "message": "success",
                "data": {
                    "id": 99,
                    "name": "a@b.com",
                    "platform": "openai",
                    "type": "oauth",
                    "credentials": {
                        "email": "a@b.com",
                        "plan_type": "plus",
                        "chatgpt_account_id": "acc-1",
                        "chatgpt_user_id": "user-1",
                        "client_id": "app_x",
                    },
                    "concurrency": 3,
                    "priority": 50,
                    "rate_multiplier": 1,
                    "auto_pause_on_expired": True,
                },
            },
        }
        payload, meta = codex_oauth.download_sub2api_export_for_local(
            local, local_filename="codex-a@b.com-sub2-callback.json"
        )
        self.assertEqual(meta["email"], "a@b.com")
        self.assertEqual(meta["source"], "local_sub2_callback")
        self.assertIn("accounts", payload)
        self.assertEqual(len(payload["accounts"]), 1)
        acc = payload["accounts"][0]
        self.assertEqual(acc["platform"], "openai")
        self.assertEqual(acc["type"], "oauth")
        self.assertEqual(acc["credentials"]["email"], "a@b.com")
        self.assertEqual(acc["credentials"]["plan_type"], "plus")
        self.assertEqual(acc["credentials"]["chatgpt_account_id"], "acc-1")

    def test_offline_convert_cpa_codex_credential(self):
        local = {
            "type": "codex",
            "email": "c@d.com",
            "access_token": "at-xxx",
            "refresh_token": "rt-xxx",
            "id_token": "id-xxx",
            "account_id": "acc-2",
            "plan_type": "plus",
            "expired": "2026-09-01T00:00:00Z",
        }
        payload, meta = codex_oauth.download_sub2api_export_for_local(local, local_filename="codex-c@d.com-plus.json")
        self.assertTrue(meta["has_refresh_token"])
        self.assertTrue(meta["has_access_token"])
        acc = payload["accounts"][0]
        self.assertEqual(acc["credentials"]["refresh_token"], "rt-xxx")
        self.assertEqual(acc["credentials"]["access_token"], "at-xxx")
        self.assertEqual(acc["credentials"]["chatgpt_account_id"], "acc-2")

    def test_bulk_offline_export_no_api(self):
        items = [
            (
                "f1.json",
                {
                    "type": "codex_sub2_callback",
                    "email": "a@b.com",
                    "sub2_submit_response": {
                        "data": {
                            "id": 1,
                            "name": "a@b.com",
                            "platform": "openai",
                            "type": "oauth",
                            "credentials": {"email": "a@b.com", "plan_type": "plus"},
                        }
                    },
                },
            ),
            (
                "f2.json",
                {
                    "type": "codex",
                    "email": "c@d.com",
                    "access_token": "at",
                    "refresh_token": "rt",
                    "account_id": "acc",
                },
            ),
        ]
        payload, added, errors = codex_oauth.download_sub2api_export_bulk(items)
        self.assertEqual(errors, [])
        self.assertEqual(len(payload["accounts"]), 2)
        self.assertEqual(len(added), 2)
        self.assertEqual(payload["proxies"], [])
        self.assertIn("exported_at", payload)


if __name__ == "__main__":
    unittest.main()
