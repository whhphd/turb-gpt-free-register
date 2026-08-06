# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import extract_link_service as svc


class ExtractLinkOai9Tests(unittest.TestCase):
    def test_pick_link_priority(self):
        url = svc._pick_link_url({
            "long_url": "http://long",
            "provider_redirect_url": "http://provider",
            "kakao_pay_url": "http://kakao",
            "nicepay_checkout_url": "http://nicepay",
        })
        self.assertEqual(url, "http://nicepay")

    def test_oai9_check_and_submit_flow(self):
        http_calls = []

        def fake_http(method, url, body=None, headers=None, timeout=30):
            http_calls.append({"method": method, "url": url, "body": body})
            if url.endswith("/api/promo-coupon/check"):
                return 200, {
                    "results": [
                        {"index": 0, "eligible": True, "state": "eligible"},
                    ]
                }
            if url.endswith("/api/kakao-link/tasks") and method == "POST":
                return 200, {
                    "tasks": [{"job_id": "job-1", "status": "queued"}],
                    "active_duplicates": [],
                }
            if "/api/kakao-link/tasks/job-1" in url and method == "GET":
                return 200, {
                    "status": "done",
                    "job_id": "job-1",
                    "nicepay_checkout_url": "https://pay.nicepay.co.kr/x",
                    "card_charged": True,
                    "kakao_status": "READY",
                }
            return 404, {"error": "unexpected " + url}

        with patch.object(svc, "_provider", return_value="oai9"), \
             patch.object(svc, "_api_base", return_value="https://oai9.example"), \
             patch.object(svc, "_http_json", side_effect=fake_http), \
             patch.object(svc.time, "sleep", return_value=None), \
             patch.object(svc.db, "mark_account_extract_running", return_value=True), \
             patch.object(svc.db, "update_account_extract", return_value=True):
            # release queue slot at end of _run_extract
            svc._QUEUE_SLOTS.acquire()
            out = svc._run_extract(
                account_id=1,
                email="a@b.com",
                access_token="token-1",
                link_type="kakao_pay",
                cdk="CARD-1",
                trigger="manual",
            )
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("status"), "success")
        self.assertEqual(out["result"]["long_url"], "https://pay.nicepay.co.kr/x")
        # check then submit then poll
        self.assertTrue(any("/api/promo-coupon/check" in c["url"] for c in http_calls))
        self.assertTrue(any(c["url"].endswith("/api/kakao-link/tasks") and c["method"] == "POST" for c in http_calls))
        post = next(c for c in http_calls if c["url"].endswith("/api/kakao-link/tasks") and c["method"] == "POST")
        self.assertEqual(post["body"]["card"], "CARD-1")
        self.assertEqual(post["body"]["accessTokens"], ["token-1"])

    def test_oai9_skips_ineligible(self):
        def fake_http(method, url, body=None, headers=None, timeout=30):
            if url.endswith("/api/promo-coupon/check"):
                return 200, {"results": [{"index": 0, "eligible": False, "state": "ineligible", "error": "not free trial"}]}
            return 500, {"error": "should not submit"}

        with patch.object(svc, "_provider", return_value="oai9"), \
             patch.object(svc, "_api_base", return_value="https://oai9.example"), \
             patch.object(svc, "_http_json", side_effect=fake_http), \
             patch.object(svc.db, "mark_account_extract_running", return_value=True), \
             patch.object(svc.db, "update_account_extract", return_value=True):
            svc._QUEUE_SLOTS.acquire()
            out = svc._run_extract(
                account_id=2,
                email="b@b.com",
                access_token="token-2",
                link_type="kakao_pay",
                cdk="CARD-1",
                trigger="manual",
            )
        self.assertFalse(out.get("ok"))
        self.assertIn("资格预检未通过", out.get("error") or "")


if __name__ == "__main__":
    unittest.main()
