# -*- coding: utf-8 -*-
import email
import re
import unittest
from unittest.mock import MagicMock, patch

from core import email_provider, mailcom_client
from core.mailcom_protocol import MailComError


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

    def test_resolve_proxy_picks_pool_when_enabled(self):
        mailcom_client.forget_group_proxy()
        acc = mailcom_client.MailComAccount(email="a@mail.com", password="x")
        with patch.object(mailcom_client._email_cfg, "MAILCOM_USE_PROXY_POOL", True), \
             patch("config.proxy.pick_proxy", return_value="socks5://u:p@pool:3000"):
            self.assertEqual(mailcom_client.resolve_mailcom_proxy(acc), "socks5h://u:p@pool:3000")
            self.assertEqual(acc.proxy_url, "")

    def test_resolve_proxy_direct_when_disabled(self):
        acc = mailcom_client.MailComAccount(email="a@mail.com", password="x")
        with patch.object(mailcom_client._email_cfg, "MAILCOM_USE_PROXY_POOL", False):
            self.assertEqual(mailcom_client.resolve_mailcom_proxy(acc), "")

    def test_resolve_proxy_force_pool_even_when_disabled(self):
        mailcom_client.forget_group_proxy()
        acc = mailcom_client.MailComAccount(email="a@mail.com", password="x")
        with patch.object(mailcom_client._email_cfg, "MAILCOM_USE_PROXY_POOL", False), \
             patch("config.proxy.pick_proxy", return_value="socks5://u:p@pool:3000"):
            self.assertEqual(
                mailcom_client.resolve_mailcom_proxy(acc, force_pool=True),
                "socks5h://u:p@pool:3000",
            )

    def test_same_inbox_group_reuses_one_proxy(self):
        mailcom_client.forget_group_proxy()
        primary = mailcom_client.MailComAccount(email="main@mail.com", password="x", login_email="main@mail.com")
        alias = mailcom_client.MailComAccount(email="alias@usa.com", password="x", login_email="main@mail.com")
        other = mailcom_client.MailComAccount(email="b@mail.com", password="x", login_email="b@mail.com")
        picks = ["socks5://u:p@pool:3000", "socks5://u:p@pool:3001"]
        with patch.object(mailcom_client._email_cfg, "MAILCOM_USE_PROXY_POOL", True), \
             patch("config.proxy.pick_proxy", side_effect=picks) as mock_pick:
            first = mailcom_client.resolve_mailcom_proxy(primary)
            second = mailcom_client.resolve_mailcom_proxy(alias)
            third = mailcom_client.resolve_mailcom_proxy(other)
        self.assertEqual(first, second)
        self.assertEqual(first, "socks5h://u:p@pool:3000")
        self.assertEqual(third, "socks5h://u:p@pool:3001")
        self.assertEqual(mock_pick.call_count, 2)
        self.assertEqual(primary.proxy_url, "")
        self.assertEqual(alias.proxy_url, "")

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
        local = addr.split("@", 1)[0]
        self.assertIsNone(re.search(r"[0-9a-f]{6}$", local))
        self.assertTrue(any(ch.isalpha() for ch in local))
        self.assertRegex(local, r"\d{2,4}")
        self.assertNotIn("abcuser", local.replace(".", ""))

    def test_alias_local_independent_of_primary_prefix(self):
        used: set[str] = set()
        addrs: list[str] = []
        with patch.object(mailcom_client._email_cfg, "MAILCOM_ALIAS_DOMAINS", ["kittymail.com"]):
            for _ in range(9):
                addr = mailcom_client._new_alias_address("HortensiaHooverwzn@dallasmail.com", used)
                used.add(addr)
                addrs.append(addr)
                local = addr.split("@", 1)[0]
                compact = local.replace(".", "")
                self.assertTrue(addr.endswith("@kittymail.com"))
                self.assertNotIn("hortensia", compact)
                self.assertNotIn("hooverwzn", compact)
                self.assertRegex(local, r"\d{2,4}")
                self.assertIsNone(re.search(r"[0-9a-f]{6}$", local))
        self.assertEqual(len(set(addrs)), 9)

    def test_alias_domains_honor_configured_suffixes(self):
        with patch.object(mailcom_client._email_cfg, "MAILCOM_ALIAS_DOMAINS", ["usa.com", "engineer.com"]):
            choices = mailcom_client._alias_domain_choices("user@contractor.net")
            self.assertEqual(choices, ["usa.com", "engineer.com"])
            addr = mailcom_client._new_alias_address("user@contractor.net", set())
            self.assertTrue(addr.endswith("@usa.com") or addr.endswith("@engineer.com"))

    def test_empty_alias_domains_randomize_builtin_not_primary(self):
        used: set[str] = set()
        suffixes: list[str] = []
        with patch.object(mailcom_client._email_cfg, "MAILCOM_ALIAS_DOMAINS", []):
            for _ in range(20):
                addr = mailcom_client._new_alias_address("user@rocketship.com", used)
                used.add(addr)
                suffixes.append(addr.rsplit("@", 1)[1])
        self.assertNotIn("rocketship.com", suffixes)
        self.assertGreater(len(set(suffixes)), 1)

    def test_configured_alias_domains_are_picked_randomly(self):
        domains = ["kittymail.com", "engineer.com", "iname.com", "fireman.net", "workmail.com"]
        used: set[str] = set()
        suffixes: list[str] = []
        with patch.object(mailcom_client._email_cfg, "MAILCOM_ALIAS_DOMAINS", domains):
            for _ in range(30):
                addr = mailcom_client._new_alias_address("user@rocketship.com", used)
                used.add(addr)
                suffixes.append(addr.rsplit("@", 1)[1])
        self.assertTrue(all(s in domains for s in suffixes))
        self.assertGreater(len(set(suffixes)), 1)
        self.assertNotEqual(set(suffixes), {"kittymail.com"})

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

    def test_proxy_exclude_variants_cover_socks5h(self):
        variants = mailcom_client._proxy_exclude_variants("socks5h://u:p@host:3000")
        self.assertIn("socks5h://u:p@host:3000", variants)
        self.assertIn("socks5://u:p@host:3000", variants)

    @patch("core.mailcom_client.time.sleep", return_value=None)
    @patch("core.mailcom_client._persist_session")
    @patch("core.mailcom_client._client_for")
    @patch("core.mailcom_client.get_account_context")
    def test_expand_aliases_rotates_proxy_on_login_redirect(self, get_ctx, client_for, _persist, _sleep):
        acc = mailcom_client.MailComAccount(email="a@mail.com", password="x")
        get_ctx.return_value = acc
        bad = MagicMock()
        bad.proxy_url = "socks5h://u:p@bad:3000"
        bad.list_aliases.side_effect = MailComError(
            "登录未返回一次性令牌 loc=https://support.mail.com/account/login/index.html",
            kind="login_redirect",
        )
        good = MagicMock()
        good.proxy_url = "socks5h://u:p@good:3000"
        good.list_aliases.return_value = []
        good.add_alias.return_value = None
        client_for.side_effect = [bad, good]
        related = [[{"email": "a@mail.com"}], [{"email": "a@mail.com"}], [{"email": "a@mail.com"}, {"email": "n@mail.com"}]]
        with patch("core.db.list_mailcom_related", side_effect=related), \
             patch("core.db.import_mailcom_emails") as imp:
            out = mailcom_client.expand_aliases("a@mail.com", count=1)
        self.assertEqual(len(out["created"]), 1)
        self.assertEqual(client_for.call_count, 2)
        self.assertTrue(imp.called)
        exclude = client_for.call_args_list[1].kwargs.get("exclude") or set()
        self.assertIn("socks5h://u:p@bad:3000", exclude)

    @patch("core.mailcom_client.time.sleep", return_value=None)
    @patch("core.mailcom_client._client_for")
    @patch("core.mailcom_client.get_account_context")
    def test_expand_aliases_does_not_rotate_on_bad_password(self, get_ctx, client_for, _sleep):
        acc = mailcom_client.MailComAccount(email="a@mail.com", password="x")
        get_ctx.return_value = acc
        bad = MagicMock()
        bad.proxy_url = "socks5h://u:p@bad:3000"
        bad.list_aliases.side_effect = MailComError("logout?ls=wd", kind="bad_credentials")
        client_for.return_value = bad
        with self.assertRaises(mailcom_client.MailComMailError) as ctx:
            mailcom_client.expand_aliases("a@mail.com", count=1)
        self.assertIn("bad_credentials", str(ctx.exception))
        self.assertEqual(client_for.call_count, 1)


class MailComOtpRecipientTests(unittest.TestCase):
    def _msg(self, **headers) -> email.message.Message:
        msg = email.message.EmailMessage()
        for key, value in headers.items():
            msg[key.replace("_", "-")] = value
        return msg

    def test_primary_does_not_eat_alias_mail_via_delivered_to(self):
        primary = "effie_cameronilx@californiamail.com"
        alias = "gary.ford78@kittymail.com"
        msg = self._msg(
            To=alias,
            **{"Delivered-To": primary},
        )
        self.assertFalse(mailcom_client._otp_mail_is_for_recipient(msg, primary))
        self.assertTrue(mailcom_client._otp_mail_is_for_recipient(msg, alias))

    def test_primary_matches_own_to(self):
        primary = "effie_cameronilx@californiamail.com"
        msg = self._msg(To=f"ChatGPT <{primary}>")
        self.assertTrue(mailcom_client._otp_mail_is_for_recipient(msg, primary))

    def test_substring_address_does_not_match(self):
        msg = self._msg(To="john@mail.com")
        self.assertFalse(mailcom_client._otp_mail_is_for_recipient(msg, "n@mail.com"))

    def test_cc_and_original_to_match(self):
        alias = "lisa.russell366@europe.com"
        self.assertTrue(mailcom_client._otp_mail_is_for_recipient(self._msg(Cc=alias), alias))
        self.assertTrue(
            mailcom_client._otp_mail_is_for_recipient(self._msg(**{"X-Original-To": alias}), alias)
        )


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
    @patch("core.mailcom_client.get_account_context")
    @patch("core.mailcom_client._imap_otp_candidates")
    def test_fetch_prefers_six_digit_verification(self, imap_cands, get_ctx, _sleep):
        get_ctx.return_value = mailcom_client.MailComAccount(
            email="a@kittymail.com", password="x"
        )
        imap_cands.return_value = [
            (1, 2_000_000_000.0, "3243"),
            (2, 1_900_000_000.0, "996557"),
        ]
        code = mailcom_client.fetch_latest_otp("a@kittymail.com", after_ts=0, max_wait=1, settle_seconds=0)
        self.assertEqual(code, "996557")
        imap_cands.assert_called()


if __name__ == "__main__":
    unittest.main()
