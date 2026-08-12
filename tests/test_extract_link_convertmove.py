# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import extract_link_service as svc


class ExtractLinkConvertmoveTests(unittest.TestCase):
    def test_mode_mapping(self):
        self.assertEqual(svc._cm_mode("kakao_pay"), "kakao")
        self.assertEqual(svc._cm_mode("pix"), "pix")

    def test_create_submission_uses_convertmove_payload(self):
        calls = []

        def fake_http(method, url, body=None, headers=None, timeout=30):
            calls.append({"method": method, "url": url, "body": body, "headers": headers})
            return 202, {"ok": True, "task_id": "abc123", "status": "queued"}

        with patch.object(svc, "_api_base", return_value="https://convertmove.cc.cd"), \
             patch.object(svc, "_http_json", side_effect=fake_http):
            data = svc._cm_create(token="sk-test-token", link_type="kakao_pay", cdk="CDK-TEST")
        self.assertEqual(data["task_id"], "abc123")
        self.assertEqual(calls[0]["method"], "POST")
        self.assertTrue(calls[0]["url"].endswith("/api/v1/submissions"))
        self.assertEqual(calls[0]["body"]["mode"], "kakao")
        self.assertEqual(calls[0]["body"]["at"], "sk-test-token")


if __name__ == "__main__":
    unittest.main()
