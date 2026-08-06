# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock

from core.roxy_registration import _is_oauth_consent_like
from core import roxy_codex_oauth


class _Driver:
    def __init__(self, url: str, script_result=None):
        self.current_url = url
        self._script_result = script_result

    def execute_script(self, script, *args):
        if self._script_result is not None:
            return self._script_result
        # 默认走 URL 分支：把 script 里的 location.href 逻辑用 current_url 近似不了，
        # 所以直接按 URL 模拟 _is_oauth_consent_like 的 JS 决策。
        url = (self.current_url or "").lower()
        if any(x in url for x in (
            "/oauth/authorize", "/log-in", "/login", "/signup", "/create-account",
            "identifier", "email-verification", "add-phone", "phone-verification", "password",
        )):
            return False
        if any(x in url for x in ("/consent", "sign-in-with-chatgpt/codex/consent", "/workspace")):
            return True
        return False


class CodexLoginPageDetectTests(unittest.TestCase):
    def test_authorize_url_is_not_consent(self):
        driver = _Driver("https://auth.openai.com/oauth/authorize?client_id=app_x&state=1")
        self.assertFalse(_is_oauth_consent_like(driver))

    def test_login_url_is_not_consent(self):
        driver = _Driver("https://auth.openai.com/log-in")
        self.assertFalse(_is_oauth_consent_like(driver))

    def test_consent_url_is_consent(self):
        driver = _Driver("https://auth.openai.com/sign-in-with-chatgpt/codex/consent")
        self.assertTrue(_is_oauth_consent_like(driver))

    def test_past_email_login_detects_consent_and_otp(self):
        d1 = _Driver("https://auth.openai.com/sign-in-with-chatgpt/codex/consent")
        self.assertTrue(roxy_codex_oauth._codex_page_past_email_login(d1))
        d2 = _Driver("https://auth.openai.com/email-verification")
        # email-verification may also rely on _is_email_verification_page; URL alone should pass
        self.assertTrue(roxy_codex_oauth._codex_page_past_email_login(d2))
        d3 = _Driver("https://auth.openai.com/oauth/authorize?client_id=x")
        self.assertFalse(roxy_codex_oauth._codex_page_past_email_login(d3))


if __name__ == "__main__":
    unittest.main()
