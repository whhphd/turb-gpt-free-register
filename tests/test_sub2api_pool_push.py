# -*- coding: utf-8 -*-
import base64
import json
import unittest
from unittest.mock import patch

from core.sub2api_pool_push import (
    build_pool_account_from_codex_json,
    detect_upload_json_format,
    normalize_upload_json_to_codex_entries,
    push_uploaded_json_to_pool,
)


def _fake_jwt(payload: dict) -> str:
    head = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{head}.{body}.sig"


class Sub2PoolPushTests(unittest.TestCase):
    def test_build_account_payload(self):
        at = _fake_jwt({
            "exp": 1893456000,
            "https://api.openai.com/profile": {"email": "u@example.com"},
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acc-1",
                "user_id": "user-1",
                "chatgpt_plan_type": "plus",
            },
        })
        content = {
            "email": "u@example.com",
            "access_token": at,
            "refresh_token": "rt.xxx",
            "id_token": "id.xxx",
            "account_id": "acc-1",
            "plan_type": "plus",
            "expires_at": "2030-01-01T00:00:00Z",
            "type": "codex",
        }
        acc = build_pool_account_from_codex_json(
            content,
            filename="codex-u@example.com-plus.json",
            group_id=8,
            concurrency=50,
        )
        self.assertEqual(acc["platform"], "openai")
        self.assertEqual(acc["type"], "oauth")
        self.assertEqual(acc["name"], "u@example.com")
        self.assertEqual(acc["group_ids"], [8])
        self.assertEqual(acc["concurrency"], 50)
        self.assertEqual(acc["credentials"]["access_token"], at)
        self.assertEqual(acc["credentials"]["refresh_token"], "rt.xxx")
        self.assertEqual(acc["credentials"]["chatgpt_account_id"], "acc-1")
        self.assertTrue(acc["auto_pause_on_expired"])

    def test_reject_sub2_callback_receipt(self):
        with self.assertRaises(ValueError):
            build_pool_account_from_codex_json(
                {"type": "codex_sub2_callback", "email": "a@b.com"},
                filename="codex-a@b.com-sub2-callback.json",
            )

    def test_pool_config_overrides_json_fields_credentials_intact(self):
        """JSON 里的 concurrency/group 等被忽略；token 原样保留。"""
        at = _fake_jwt({
            "exp": 1893456000,
            "https://api.openai.com/profile": {"email": "x@y.com"},
            "https://api.openai.com/auth": {"chatgpt_account_id": "acc-x"},
        })
        # sub2api 导出形态：自带错误的号池参数
        content = {
            "name": "x@y.com",
            "platform": "openai",
            "type": "oauth",
            "concurrency": 1,
            "priority": 99,
            "load_factor": 1,
            "rate_multiplier": 9.9,
            "group_ids": [1, 2, 3],
            "auto_pause_on_expired": False,
            "credentials": {
                "access_token": at,
                "refresh_token": "rt.keep-me",
                "email": "x@y.com",
                "chatgpt_account_id": "acc-x",
            },
        }
        acc = build_pool_account_from_codex_json(
            content,
            filename="export-x.json",
            group_id=8,
            concurrency=50,
            priority=1,
            load_factor=10,
            rate_multiplier=1.0,
        )
        self.assertEqual(acc["credentials"]["access_token"], at)
        self.assertEqual(acc["credentials"]["refresh_token"], "rt.keep-me")
        self.assertEqual(acc["group_ids"], [8])
        self.assertEqual(acc["concurrency"], 50)
        self.assertEqual(acc["priority"], 1)
        self.assertEqual(acc["load_factor"], 10)
        self.assertEqual(acc["rate_multiplier"], 1.0)
        self.assertTrue(acc["auto_pause_on_expired"])

    def test_detect_and_normalize_cpa_and_sub2_formats(self):
        at = _fake_jwt({
            "exp": 1893456000,
            "https://api.openai.com/profile": {"email": "cpa@e.com"},
        })
        cpa = {
            "type": "codex",
            "email": "cpa@e.com",
            "access_token": at,
            "refresh_token": "rt.cpa",
        }
        self.assertEqual(detect_upload_json_format(cpa, filename="codex-cpa@e.com.json"), "codex_cpa")
        entries = normalize_upload_json_to_codex_entries(cpa, filename="codex-cpa@e.com.json")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["access_token"], at)
        self.assertEqual(entries[0]["email"], "cpa@e.com")

        at2 = _fake_jwt({
            "exp": 1893456000,
            "https://api.openai.com/profile": {"email": "s@e.com"},
        })
        export = {
            "accounts": [
                {
                    "name": "s@e.com",
                    "platform": "openai",
                    "concurrency": 3,
                    "credentials": {"access_token": at2, "refresh_token": "rt.s", "email": "s@e.com"},
                }
            ],
            "proxies": [],
        }
        self.assertEqual(detect_upload_json_format(export), "sub2api_export")
        entries2 = normalize_upload_json_to_codex_entries(export, filename="export.json")
        self.assertEqual(len(entries2), 1)
        self.assertEqual(entries2[0]["access_token"], at2)
        self.assertEqual(entries2[0]["_format"], "sub2api_export")

    def test_push_uploaded_uses_saved_pool_config(self):
        at = _fake_jwt({
            "exp": 1893456000,
            "https://api.openai.com/profile": {"email": "u@z.com"},
        })
        payload = {
            "accounts": [{
                "name": "u@z.com",
                "concurrency": 2,
                "group_ids": [99],
                "credentials": {"access_token": at, "refresh_token": "rt.z", "email": "u@z.com"},
            }]
        }
        captured = []

        def fake_post(accounts, *, timeout):
            captured.extend(accounts)
            return {"code": 0, "data": {"success": len(accounts), "failed": 0}}

        with patch("core.sub2api_pool_push._post_batch", side_effect=fake_post):
            result = push_uploaded_json_to_pool(
                [("batch.json", payload)],
                group_id=8,
                concurrency=50,
            )
        self.assertEqual(result["success"], 1)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["concurrency"], 50)
        self.assertEqual(captured[0]["group_ids"], [8])
        self.assertEqual(captured[0]["credentials"]["access_token"], at)
        self.assertEqual(captured[0]["credentials"]["refresh_token"], "rt.z")
        self.assertEqual(captured[0]["extra"]["import_source"], "codex_pool_upload")


if __name__ == "__main__":
    unittest.main()
