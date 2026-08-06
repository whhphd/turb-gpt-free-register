# -*- coding: utf-8 -*-
"""查活适配 password_totp：有密码+2FA 时不走邮箱 OTP。"""
import unittest
from unittest.mock import MagicMock, patch

from core import openai_auth
from core import account_liveness as al


class NeedsStepHelpersTests(unittest.TestCase):
    def test_needs_totp_and_email_otp(self):
        self.assertTrue(openai_auth.needs_totp_step("mfa_challenge", ""))
        self.assertTrue(openai_auth.needs_totp_step("", "https://auth.openai.com/multi-factor/totp"))
        self.assertTrue(openai_auth.needs_email_otp_step("email_otp_verification", ""))
        self.assertFalse(openai_auth.needs_totp_step("email_otp_verification", "https://auth.openai.com/email-verification"))


class LoadMaterialTests(unittest.TestCase):
    def test_password_totp_prefers_password_without_mail(self):
        acc = {
            "email": "a@b.com",
            "password": "Passw0rd!",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "email_source": "password_totp",
        }
        with patch("core.db.get_account_by_email", return_value=acc), \
             patch.object(al, "_account_has_mail_inbox", return_value=False):
            mat = al._load_account_login_material("a@b.com")
        self.assertTrue(mat["prefer_password"])
        self.assertEqual(mat["password"], "Passw0rd!")
        self.assertTrue(mat["totp_secret"])
        self.assertFalse(mat["has_mail"])

    def test_outlook_without_password_uses_email_otp(self):
        acc = {
            "email": "o@b.com",
            "password": "",
            "totp_secret": None,
            "email_source": "outlook",
        }
        with patch("core.db.get_account_by_email", return_value=acc), \
             patch.object(al, "_account_has_mail_inbox", return_value=True):
            mat = al._load_account_login_material("o@b.com")
        self.assertFalse(mat["prefer_password"])
        self.assertTrue(mat["has_mail"])


class PasswordTotpLoginPathTests(unittest.TestCase):
    def test_password_then_totp_then_session(self):
        session = MagicMock()
        session.device_id = "dev-1"
        session.proxy = "http://p"

        with patch.object(al, "submit_login_email", return_value={
            "page": {"type": "login_password"},
            "continue_url": "https://auth.openai.com/log-in/password",
        }), patch.object(al, "verify_login_password", return_value={
            "page": {"type": "mfa_challenge"},
            "continue_url": "https://auth.openai.com/multi-factor/totp",
        }) as mock_pwd, patch.object(al, "generate_totp_code", return_value="123456"), patch.object(
            al, "verify_login_totp", return_value={
                "continue_url": "https://auth.openai.com/api/accounts/authorize/continue?x=1",
                "page": {"type": "external_url"},
            }
        ) as mock_totp, patch.object(al, "follow_oauth_callback", return_value="https://chatgpt.com/"), patch.object(
            al, "fetch_session", return_value={
                "accessToken": "at-live",
                "user": {"id": "u1", "email": "a@b.com"},
                "account": {"planType": "plus"},
            }
        ):
            info = al._login_via_password_totp(
                session,
                "a@b.com",
                password="Passw0rd!",
                totp_secret="JBSWY3DPEHPK3PXP",
                final_url="https://auth.openai.com/log-in",
                has_mail=False,
            )

        self.assertEqual(info["accessToken"], "at-live")
        mock_pwd.assert_called_once()
        mock_totp.assert_called_once()
        self.assertEqual(mock_totp.call_args.args[1], "123456")

    def test_check_account_liveness_routes_password_totp(self):
        material = {
            "password": "Passw0rd!",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "email_source": "password_totp",
            "has_mail": False,
            "prefer_password": True,
        }
        session = MagicMock()
        session.device_id = "dev"
        session.proxy = None

        with patch.object(al, "_load_account_login_material", return_value=material), \
             patch.object(al, "_network_preflight_with_retry", return_value=(session, "https://auth.openai.com/api/accounts/authorize?x=1")), \
             patch.object(al, "follow_authorize", return_value="https://auth.openai.com/log-in"), \
             patch.object(al, "detect_account_unusable_text", return_value=None), \
             patch.object(al, "_login_via_password_totp", return_value={
                 "accessToken": "at-x",
                 "user": {"id": "u"},
                 "account": {"planType": "free"},
             }) as mock_pwd_login, \
             patch.object(al, "_login_via_email_otp") as mock_otp_login:
            result = al.check_account_liveness("a@b.com", proxy=None, clear_log=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "live")
        self.assertEqual(result["login_mode"], "password_totp")
        mock_pwd_login.assert_called_once()
        mock_otp_login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
