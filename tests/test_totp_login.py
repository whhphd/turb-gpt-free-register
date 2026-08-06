# -*- coding: utf-8 -*-
import time
import unittest
from unittest.mock import patch

import pyotp

from core import totp_login


class TotpLoginHelpersTests(unittest.TestCase):
    def test_normalize_secret_and_otpauth(self):
        self.assertEqual(totp_login.normalize_totp_secret(" ab cd ef "), "ABCDEF")
        secret = totp_login.normalize_totp_secret(
            "otpauth://totp/OpenAI:user@example.com?secret=JBSWY3DPEHPK3PXP&issuer=OpenAI"
        )
        self.assertEqual(secret, "JBSWY3DPEHPK3PXP")

    def test_generate_totp_code_matches_pyotp(self):
        secret = "JBSWY3DPEHPK3PXP"
        code = totp_login.generate_totp_code(secret, wait_near_boundary=False)
        self.assertEqual(code, pyotp.TOTP(secret).now())
        self.assertRegex(code, r"^\d{6}$")

    def test_detect_totp_by_url_and_text(self):
        self.assertTrue(
            totp_login.is_totp_challenge(
                url="https://auth.openai.com/factor-totp",
                text="Enter the code from your authenticator app",
            )
        )
        self.assertTrue(
            totp_login.is_totp_challenge(
                url="https://auth.openai.com/log-in",
                text="Open your authenticator app and enter the 6-digit code",
            )
        )
        self.assertFalse(
            totp_login.is_totp_challenge(
                url="https://auth.openai.com/email-verification",
                text="Check your email for a temporary ChatGPT login code",
            )
        )

    def test_oauth_authorize_not_false_positive_2fa(self):
        """redirect_uri 里 %2Fauth 不能被裸子串 2fa 误判。"""
        url = (
            "https://auth.openai.com/oauth/authorize"
            "?client_id=app_xxx&response_type=code"
            "&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback"
            "&scope=openid+email+profile+offline_access"
            "&code_challenge_method=S256&prompt=login"
        )
        # 纯授权入口 + 登录文案
        self.assertFalse(totp_login.is_totp_challenge(url=url, text=""))
        self.assertFalse(
            totp_login.is_totp_challenge(url=url, text="Log in or sign up to continue")
        )
        self.assertFalse(
            totp_login.is_totp_challenge(url=url, text="Continue with email Email address")
        )
        # 真 TOTP path 仍要识别
        self.assertTrue(
            totp_login.is_totp_challenge(
                url="https://auth.openai.com/multi-factor/totp",
                text="Enter the code from your authenticator app",
            )
        )

    def test_email_otp_not_confused_with_totp(self):
        self.assertTrue(
            totp_login.is_email_otp_challenge(
                url="https://auth.openai.com/email-verification",
                text="We sent a code to your email address",
            )
        )
        self.assertFalse(
            totp_login.is_email_otp_challenge(
                url="https://auth.openai.com/factor-totp",
                text="Authenticator app",
            )
        )

    def test_load_totp_secret_prefers_explicit(self):
        secret = totp_login.load_totp_secret("a@b.com", explicit="JBSWY3DPEHPK3PXP")
        self.assertEqual(secret, "JBSWY3DPEHPK3PXP")

    def test_load_totp_secret_from_db(self):
        with patch("core.db.get_account_by_email", return_value={"totp_secret": "jbswy3dpehpk3pxp"}):
            secret = totp_login.load_totp_secret("user@example.com")
            self.assertEqual(secret, "JBSWY3DPEHPK3PXP")


if __name__ == "__main__":
    unittest.main()
