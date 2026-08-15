# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock

import requests

from core.bugteam_client import BugTeamClient, BugTeamConfigError, BugTeamError
from config.env_loader import SECRET_ENV_KEYS
from webui.config_editor import EDITABLE_FIELDS


def _response(status, body, headers=None):
    response = Mock()
    response.status_code = status
    response.headers = headers or {}
    response.json.return_value = body
    response.content = b""
    response.text = ""
    return response


class BugTeamClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.sleeps = []
        self.client = BugTeamClient(
            base_url="https://bugteam.example",
            token="cfk-test-token",
            session=self.session,
            sleep_fn=self.sleeps.append,
            max_retries=2,
        )

    def test_missing_token_is_rejected_before_request(self):
        client = BugTeamClient(base_url="https://bugteam.example", token="", session=self.session)
        with self.assertRaises(BugTeamConfigError):
            client.balance()
        self.session.request.assert_not_called()

    def test_default_timeout_and_retry_budget(self):
        client = BugTeamClient(base_url="https://bugteam.example", token="cfk-test-token", session=self.session)
        self.assertEqual(client.timeout, 8.0)
        self.assertEqual(client.max_retries, 1)

    def test_token_is_declared_as_server_side_secret(self):
        fields = {field["key"]: field for field in EDITABLE_FIELDS}
        self.assertIn("BUGTEAM_API_TOKEN", SECRET_ENV_KEYS)
        self.assertTrue(fields["BUGTEAM_API_TOKEN"]["secret"])
        self.assertEqual(fields["BUGTEAM_API_TOKEN"]["storage"], "env")

    def test_inventory_sends_customer_token_and_params(self):
        self.session.request.return_value = _response(200, {"available": 12})
        body = self.client.inventory("team_1h", 5)
        self.assertEqual(body["available"], 12)
        kwargs = self.session.request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["X-Customer-Token"], "cfk-test-token")
        self.assertEqual(kwargs["params"], {"product": "team_1h", "quantity": 5})

    def test_create_order_uses_idempotency_key(self):
        self.session.request.return_value = _response(
            202, {"order": {"order_id": "order-1", "state": "pending"}}
        )
        body = self.client.create_order("team_1h", 2, idempotency_key="idem-1")
        self.assertEqual(body["order"]["order_id"], "order-1")
        kwargs = self.session.request.call_args.kwargs
        self.assertEqual(kwargs["json"], {"product": "team_1h", "quantity": 2})
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], "idem-1")

    def test_429_retries_after_header(self):
        self.session.request.side_effect = [
            _response(429, {"error": "slow"}, {"Retry-After": "1.5"}),
            _response(200, {"balance_fen": 100}),
        ]
        body = self.client.balance()
        self.assertEqual(body["balance_fen"], 100)
        self.assertEqual(self.sleeps, [1.5])

    def test_recovery_claim_sends_ticket_and_idempotency(self):
        self.session.request.return_value = _response(200, {"accounts": [{"email": "a@example.com"}]})
        body = self.client.claim_recovery(
            "r-1",
            claim_url="https://bugteam.example/signed/claim",
            ticket="ticket-1",
            idempotency_key="recovery-idem-1",
        )
        self.assertEqual(body["accounts"][0]["email"], "a@example.com")
        call = self.session.request.call_args
        self.assertEqual(call.args[1], "https://bugteam.example/signed/claim")
        self.assertEqual(call.kwargs["headers"]["X-Recovery-Ticket"], "ticket-1")
        self.assertEqual(call.kwargs["headers"]["Idempotency-Key"], "recovery-idem-1")

    def test_recovery_claim_default_idempotency_is_stable(self):
        self.session.request.return_value = _response(200, {"accounts": []})
        self.client.claim_recovery("r-2", ticket="ticket-2")
        first_key = self.session.request.call_args.kwargs["headers"]["Idempotency-Key"]
        self.client.claim_recovery("r-2", ticket="ticket-2")
        second_key = self.session.request.call_args.kwargs["headers"]["Idempotency-Key"]
        self.assertEqual(first_key, "bugteam-recovery-r-2")
        self.assertEqual(second_key, first_key)

    def test_download_uses_sub2_format(self):
        self.session.request.return_value = _response(200, {"accounts": []})
        self.client.download_order("order-1")
        kwargs = self.session.request.call_args.kwargs
        self.assertEqual(kwargs["params"], {"format": "sub2"})

    def test_network_error_retries_get(self):
        self.session.request.side_effect = [
            requests.ConnectionError("reset"),
            _response(200, {"available": 1}),
        ]
        body = self.client.inventory("team_1h", 1)
        self.assertEqual(body["available"], 1)
        self.assertEqual(self.sleeps, [1.0])

    def test_business_error_preserves_status_without_token_in_message(self):
        self.session.request.return_value = _response(404, {"error": "商品不可用"})
        with self.assertRaises(BugTeamError) as ctx:
            self.client.inventory("oauth_30d", 1)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertNotIn("cfk-test-token", str(ctx.exception))

    def test_signed_claim_query_is_redacted_from_error_message(self):
        self.session.request.return_value = _response(403, {"error": "ticket expired"})
        with self.assertRaises(BugTeamError) as ctx:
            self.client.claim_recovery(
                "r-3",
                claim_url="https://bugteam.example/api/customer/recoveries/r-3/claim?ticket=private-ticket",
                ticket="private-ticket",
            )
        self.assertNotIn("private-ticket", str(ctx.exception))
        self.assertIn("/api/customer/recoveries/r-3/claim", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
