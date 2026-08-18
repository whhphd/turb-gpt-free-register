# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from core import email_provider, mailcom_client


class MailComDetectTests(unittest.TestCase):
    def test_kittymail_is_mailcom_domain(self):
        self.assertTrue(mailcom_client.is_mailcom_address("Fabulousuoi@kittymail.com"))
        self.assertTrue(mailcom_client.is_mailcom_address("a@mail.com"))
        self.assertFalse(mailcom_client.is_mailcom_address("user@gmail.com"))

    def test_proxy_normalize(self):
        self.assertEqual(
            mailcom_client.normalize_mailcom_proxy("gate.example:1000:u:p"),
            "http://u:p@gate.example:1000",
        )
        self.assertTrue(mailcom_client.looks_like_mailcom_proxy("socks5://127.0.0.1:1080"))


class MailComProviderRoutingTests(unittest.TestCase):
    def test_parse_email_sources_accepts_mailcom(self):
        self.assertIn("mailcom", email_provider.parse_email_sources("outlook,mailcom"))

    def test_resolve_proxy_uses_account_proxy_first(self):
        acc = mailcom_client.MailComAccount(
            email="a@mail.com", password="x", proxy_url="socks5://u:p@host:3000"
        )
        with patch.object(mailcom_client._email_cfg, "MAILCOM_USE_PROXY_POOL", True):
            self.assertEqual(mailcom_client.resolve_mailcom_proxy(acc), "socks5h://u:p@host:3000")

    def test_resolve_proxy_picks_pool_when_enabled(self):
        acc = mailcom_client.MailComAccount(email="a@mail.com", password="x")
        with patch.object(mailcom_client._email_cfg, "MAILCOM_USE_PROXY_POOL", True), \
             patch("config.proxy.pick_proxy", return_value="socks5://u:p@pool:3000"), \
             patch("core.db.update_mailcom_proxy"):
            self.assertEqual(mailcom_client.resolve_mailcom_proxy(acc), "socks5h://u:p@pool:3000")
            self.assertEqual(acc.proxy_url, "socks5h://u:p@pool:3000")

    def test_resolve_proxy_direct_when_disabled(self):
        acc = mailcom_client.MailComAccount(email="a@mail.com", password="x")
        with patch.object(mailcom_client._email_cfg, "MAILCOM_USE_PROXY_POOL", False):
            self.assertEqual(mailcom_client.resolve_mailcom_proxy(acc), "")

    def test_resolve_proxy_force_pool_even_when_disabled(self):
        acc = mailcom_client.MailComAccount(email="a@mail.com", password="x")
        with patch.object(mailcom_client._email_cfg, "MAILCOM_USE_PROXY_POOL", False), \
             patch("config.proxy.pick_proxy", return_value="socks5://u:p@pool:3000"), \
             patch("core.db.update_mailcom_proxy"):
            self.assertEqual(
                mailcom_client.resolve_mailcom_proxy(acc, force_pool=True),
                "socks5h://u:p@pool:3000",
            )

    def test_auto_expand_skipped_when_count_zero(self):
        with patch.object(mailcom_client._email_cfg, "MAILCOM_AUTO_ALIAS_COUNT", 0), \
             patch.object(mailcom_client, "expand_aliases") as exp:
            out = mailcom_client.auto_expand_imported_primaries(["a@mail.com"])
        self.assertEqual(out["created"], 0)
        exp.assert_not_called()

    def test_new_alias_address_unique(self):
        used = {"abc123@mail.com"}
        addr = mailcom_client._new_alias_address("AbcUser@mail.com", used)
        self.assertTrue(addr.endswith("@mail.com") or "@" in addr)
        self.assertNotIn(addr, used)

    @patch("core.mailcom_client.resolve_mailcom_proxy", return_value="socks5h://u:p@pool:3000")
    @patch("core.mailcom_client._persist_session")
    @patch("core.mailcom_client._client_for")
    @patch("core.mailcom_client.get_account_context")
    def test_expand_aliases_creates_and_imports(self, get_ctx, client_for, _persist, _proxy):
        acc = mailcom_client.MailComAccount(email="a@mail.com", password="x")
        get_ctx.return_value = acc
        client = MagicMock()
        client.list_aliases.return_value = ["oldalias@mail.com"]
        client.add_alias.return_value = None
        client_for.return_value = client
        with patch("core.db.list_mailcom_related", side_effect=[[{"email": "a@mail.com"}], [{"email": "a@mail.com"}, {"email": "oldalias@mail.com"}], [{"email": "a@mail.com"}, {"email": "oldalias@mail.com"}, {"email": "new@mail.com"}]]), \
             patch("core.db.import_mailcom_emails") as imp:
            out = mailcom_client.expand_aliases("a@mail.com", count=1)
        self.assertEqual(out["imported_existing"], ["oldalias@mail.com"])
        self.assertEqual(len(out["created"]), 1)
        self.assertGreaterEqual(imp.call_count, 2)


class MailComOtpFilterTests(unittest.TestCase):
    def test_skip_signin_keep_verification(self):
        self.assertFalse(
            mailcom_client._is_chatgpt_otp_mail(
                "New sign-in to your OpenAI account",
                "OpenAI <noreply@tm.openai.com>",
                "A new device signed in. Code 3243",
            )
        )
        self.assertTrue(
            mailcom_client._is_chatgpt_otp_mail(
                "Your temporary ChatGPT verification code",
                "ChatGPT <noreply@tm.openai.com>",
                "Your code is 996557",
            )
        )

    @patch("core.mailcom_client.time.sleep", return_value=None)
    @patch("core.mailcom_client._persist_session")
    @patch("core.mailcom_client.get_account_context")
    @patch("core.mailcom_client._client_for")
    def test_fetch_prefers_six_digit_verification(self, client_for, get_ctx, _persist, _sleep):
        get_ctx.return_value = mailcom_client.MailComAccount(
            email="a@kittymail.com", password="x"
        )
        signin = MagicMock(
            mail_id="1",
            subject="New sign-in to your OpenAI account",
            sender="OpenAI <noreply@tm.openai.com>",
            date_ms=2_000_000_000_000,
        )
        verify = MagicMock(
            mail_id="2",
            subject="Your temporary ChatGPT verification code",
            sender="ChatGPT <noreply@tm.openai.com>",
            date_ms=1_900_000_000_000,
        )
        client = MagicMock()
        client.query_messages.return_value = [signin, verify]
        client.get_body.side_effect = lambda mail_id: (
            "New sign-in alert 3243" if mail_id == "1" else "Your code is 996557"
        )
        client_for.return_value = client
        code = mailcom_client.fetch_latest_otp("a@kittymail.com", after_ts=0, max_wait=1, settle_seconds=0)
        self.assertEqual(code, "996557")


if __name__ == "__main__":
    unittest.main()
