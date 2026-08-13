# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

import requests

from core.sogouedu_client import SogouEduClient, SogouEduConfigError, SogouEduError


def _response(status, body, headers=None):
    r = Mock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = body
    r.content = b""
    r.text = ""
    return r


class SogouEduClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.sleeps = []
        self.client = SogouEduClient(
            base_url="https://sogou.example",
            username="u",
            password="p",
            session=self.session,
            sleep_fn=self.sleeps.append,
            max_retries=2,
        )

    def test_login_caches_token(self):
        self.session.request.return_value = _response(200, {"token": "tok-1"})
        body = self.client.login()
        self.assertEqual(body["token"], "tok-1")
        self.assertTrue(self.client.token_configured)
        kwargs = self.session.request.call_args.kwargs
        self.assertEqual(kwargs["json"], {"username": "u", "password": "p"})
        self.assertNotIn("X-Customer-Token", kwargs["headers"])

    def test_missing_credentials(self):
        client = SogouEduClient(base_url="https://sogou.example", username="", password="", session=self.session)
        with self.assertRaises(SogouEduConfigError):
            client.login()

    def test_401_relogin_once_then_retry(self):
        self.session.request.side_effect = [
            _response(200, {"token": "old"}),
            _response(401, {"error": "expired"}),
            _response(200, {"token": "new"}),
            _response(200, {"available": 3}),
        ]
        self.client.login()
        out = self.client.inventory("oauth_7d", 2)
        self.assertEqual(out["available"], 3)
        self.assertEqual(self.session.request.call_count, 4)
        self.assertEqual(self.client._token, "new")

    def test_429_waits_retry_after(self):
        self.session.request.side_effect = [
            _response(200, {"token": "tok"}),
            _response(429, {"error": "slow"}, {"Retry-After": "1.5"}),
            _response(200, {"balance_fen": 10}),
        ]
        self.client.login()
        out = self.client.balance()
        self.assertEqual(out["balance_fen"], 10)
        self.assertEqual(self.sleeps, [1.5])

    def test_order_uses_idempotency_key_and_accepts_202(self):
        self.session.request.side_effect = [
            _response(200, {"token": "tok"}),
            _response(202, {"order": {"id": "o-1"}}),
        ]
        self.client.login()
        body = self.client.create_order("oauth_7d", 2, idempotency_key="idem-1")
        self.assertEqual(body["order"]["id"], "o-1")
        kwargs = self.session.request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], "idem-1")

    def test_network_error_retries_get(self):
        self.session.request.side_effect = [
            _response(200, {"token": "tok"}),
            requests.ConnectionError("reset"),
            _response(200, {"available": 1}),
        ]
        self.client.login()
        body = self.client.inventory("oauth_7d", 1)
        self.assertEqual(body["available"], 1)
        self.assertEqual(self.sleeps, [1.0])

    def test_business_error_preserves_status_and_body(self):
        self.session.request.side_effect = [
            _response(200, {"token": "tok"}),
            _response(402, {"error": "insufficient balance"}),
        ]
        self.client.login()
        with self.assertRaises(SogouEduError) as ctx:
            self.client.create_order("oauth_7d", 1, idempotency_key="idem-2")
        self.assertEqual(ctx.exception.status_code, 402)
        self.assertEqual(ctx.exception.body["error"], "insufficient balance")

    def test_finalize_order_uses_manual_finalize_endpoint_without_network_retry(self):
        self.session.request.side_effect = [
            _response(200, {"token": "tok"}),
            _response(200, {"order": {"id": "o-2", "status": "completed"}}),
        ]
        self.client.login()

        body = self.client.finalize_order("o-2")

        self.assertEqual(body["order"]["status"], "completed")
        self.assertEqual(
            self.session.request.call_args.args[1],
            "https://sogou.example/api/customer/manual/orders/o-2/finalize",
        )
        self.assertEqual(self.session.request.call_args.args[0], "POST")

    def test_claim_uses_signed_claim_url(self):
        self.session.request.side_effect = [
            _response(200, {"token": "tok"}),
            _response(200, {"payload": {"accounts": []}}),
        ]
        self.client.login()
        out = self.client.claim_recovery(7, claim_url="https://sogou.example/signed/ticket")
        self.assertIn("payload", out)
        kwargs = self.session.request.call_args.kwargs
        self.assertEqual(self.session.request.call_args.args[1], "https://sogou.example/signed/ticket")
        self.assertEqual(kwargs["headers"]["X-Requested-With"], "customer-console")


if __name__ == "__main__":
    unittest.main()
