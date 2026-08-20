# -*- coding: utf-8 -*-
"""mail.com token：旧 sid 返回 HTTP 400 时应重新登录，而不是死磕。"""
import base64
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from core.mailcom_protocol import MailComClient, MailComError


def _jwt(*, exp: float | None = None, auth_id: str = "aid-1") -> str:
    payload = {"exp": int(exp if exp is not None else time.time() + 3600), "auth_id": auth_id}
    mid = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"hdr.{mid}.sig"


class MailComTokenReloginTests(unittest.TestCase):
    def test_ensure_mail_token_relogins_when_old_sid_returns_400(self):
        client = MailComClient("a@mail.com", "pw", state={"sid": "dead-sid"})
        bad = MagicMock(status_code=400, text='{"error":"invalid_grant"}')
        ok = MagicMock(status_code=200)
        ok.json.return_value = {"access_token": _jwt()}
        posts = [bad, ok]
        client.session.post = MagicMock(side_effect=lambda *a, **k: posts.pop(0))

        def _login():
            client.sid = "fresh-sid"

        with patch.object(client, "login", side_effect=_login) as mock_login:
            token = client.ensure_mail_token()

        self.assertTrue(token)
        self.assertEqual(client.sid, "fresh-sid")
        mock_login.assert_called()
        self.assertEqual(len(posts), 0)

    def test_fresh_login_400_stays_oauth_failed(self):
        client = MailComClient("a@mail.com", "pw", state={})
        bad = MagicMock(status_code=400, text='{"error":"WRONG_PUBLIC_SECRET"}')
        client.session.post = MagicMock(return_value=bad)

        def _login():
            client.sid = "new-sid"

        with patch.object(client, "login", side_effect=_login):
            with self.assertRaises(MailComError) as ctx:
                client.ensure_mail_token()
        self.assertEqual(ctx.exception.kind, "oauth_failed")

    def test_ensure_settings_token_relogins_on_400(self):
        client = MailComClient("a@mail.com", "pw", state={"sid": "dead-sid"})
        bad = MagicMock(status_code=400, text="bad sid")
        ok = MagicMock(status_code=200)
        ok.json.return_value = {"access_token": _jwt()}
        posts = [bad, ok]
        client.session.post = MagicMock(side_effect=lambda *a, **k: posts.pop(0))

        def _login():
            client.sid = "fresh-sid"

        with patch.object(client, "login", side_effect=_login):
            token = client.ensure_settings_token()
        self.assertTrue(token)
        self.assertEqual(client.sid, "fresh-sid")


class MailComInboxLockTests(unittest.TestCase):
    def test_same_login_shares_one_lock(self):
        from core import mailcom_client as mc

        a = mc._inbox_lock("Main@mail.com")
        b = mc._inbox_lock("main@mail.com")
        c = mc._inbox_lock("other@mail.com")
        self.assertIs(a, b)
        self.assertIsNot(a, c)


class MailComLoginRedirectTests(unittest.TestCase):
    def test_login_without_ott_includes_location(self):
        client = MailComClient("a@mail.com", "pw")
        client.session.get = MagicMock()
        client.session.get.return_value.ok = False
        resp = MagicMock(status_code=302, headers={"Location": "https://www.mail.com/logout?ls=te"})
        client.session.post = MagicMock(return_value=resp)
        with self.assertRaises(MailComError) as ctx:
            client._login_once()
        self.assertEqual(ctx.exception.kind, "login_redirect")
        self.assertIn("logout?ls=te", str(ctx.exception))

    def test_login_redirect_does_not_retry_same_proxy(self):
        client = MailComClient("a@mail.com", "pw")
        client._login_once = MagicMock(
            side_effect=MailComError("loc=https://support.mail.com/account/login/index.html", kind="login_redirect")
        )
        with patch("core.mailcom_protocol.time.sleep") as slept:
            with self.assertRaises(MailComError) as ctx:
                client.login(retries=3)
        self.assertEqual(ctx.exception.kind, "login_redirect")
        self.assertEqual(client._login_once.call_count, 1)
        slept.assert_not_called()


if __name__ == "__main__":
    unittest.main()
