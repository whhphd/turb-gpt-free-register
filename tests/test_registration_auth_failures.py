# -*- coding: utf-8 -*-
"""注册阶段：限流 / 废号识别与停用规则。"""
import base64
import json
import unittest
from unittest.mock import MagicMock

from core.openai_auth import (
    AccountUnusableError,
    RateLimitError,
    decode_auth_error_payload,
    detect_account_unusable_text,
    detect_auth_failure_from_url,
    detect_rate_limit_text,
    should_disable_registration_email_for_error,
)
from core import registration_service as reg_svc
from core import roxy_registration as roxy


def _payload_url(error_code: str = "rate_limit_exceeded") -> str:
    payload = {
        "kind": "AuthApiFailure",
        "errorCode": error_code,
        "requestId": "test-req",
        "retryUrl": "https://chatgpt.com/auth/login_with?callback_path=/",
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"https://auth.openai.com/error?payload={raw}&session_id=authsess_x"


class OpenAIAuthFailureDetectTests(unittest.TestCase):
    def test_decode_rate_limit_payload(self):
        url = _payload_url("rate_limit_exceeded")
        data = decode_auth_error_payload(url)
        self.assertEqual(data.get("errorCode"), "rate_limit_exceeded")
        kind, code = detect_auth_failure_from_url(url)
        self.assertEqual(kind, "rate_limit")
        self.assertEqual(code, "rate_limit_exceeded")
        self.assertEqual(detect_rate_limit_text(url), "rate_limit_exceeded")

    def test_detect_account_deactivated_page_text(self):
        text = (
            "Authentication Error\n"
            "You do not have an account because it has been deleted or deactivated.\n"
            "error_code: account_deactivated\n"
            "request_id: abc"
        )
        self.assertEqual(detect_account_unusable_text(text), "account_deactivated")
        self.assertTrue(should_disable_registration_email_for_error(text))
        self.assertTrue(
            should_disable_registration_email_for_error(
                AccountUnusableError("dead", error_code="account_deactivated")
            )
        )

    def test_rate_limit_not_disable_email(self):
        err = RateLimitError("OpenAI Auth 限流 error_code=rate_limit_exceeded")
        self.assertTrue(reg_svc._is_rate_limit_registration_error(err))
        self.assertFalse(should_disable_registration_email_for_error(err))
        self.assertGreaterEqual(reg_svc._retry_delay_for_error(2.0, err), 12.0)

    def test_password_page_still_disables(self):
        text = "邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: url=https://auth.openai.com/log-in/password"
        self.assertTrue(reg_svc._should_disable_failed_registration_email(text))

    def test_mailcom_otp_timeout_is_not_disable(self):
        text = "MailComMailError: 等待 mail.com 验证码超时: a@kittymail.com; last=尚未出现 after_ts 之后的 ChatGPT 6 位验证码邮件"
        self.assertFalse(reg_svc._should_disable_failed_registration_email(text))
        from core.email_provider import is_otp_not_received_error
        self.assertTrue(is_otp_not_received_error(text))

    def test_mailcom_registration_has_zero_retries(self):
        retries, _delay = reg_svc._registration_retry_settings("alias@kittymail.com")
        self.assertEqual(retries, 0)
        other, _ = reg_svc._registration_retry_settings("user@outlook.com")
        self.assertGreaterEqual(other, 0)

    def test_mailcom_otp_attempts_is_one(self):
        from core.email_provider import registration_otp_attempts, is_otp_not_received_error
        self.assertEqual(registration_otp_attempts("a@kittymail.com"), 1)
        self.assertEqual(registration_otp_attempts("a@gmail.com"), 3)
        self.assertFalse(is_otp_not_received_error("MailComMailError: IMAP 登录失败"))
        # EMAIL_SOURCE=mailcom 时也不能把未知 gmail 当成 mail.com
        retries, _ = reg_svc._registration_retry_settings("someone@gmail.com")
        self.assertGreater(retries, 0)


class RoxyAuthPageInspectTests(unittest.TestCase):
    def test_inspect_rate_limit_from_url(self):
        url = _payload_url("rate_limit_exceeded")
        driver = MagicMock()
        driver.current_url = url
        driver.execute_script.return_value = {"url": url, "title": "Error", "text": ""}
        info = roxy._inspect_auth_page_failure(driver)
        self.assertEqual(info.get("kind"), "rate_limit")
        self.assertEqual(info.get("error_code"), "rate_limit_exceeded")

    def test_inspect_account_unusable_from_body(self):
        url = "https://auth.openai.com/email-verification"
        body = (
            "Authentication Error You do not have an account because it has been "
            "deleted or deactivated. error_code: account_deactivated"
        )
        driver = MagicMock()
        driver.current_url = url
        driver.execute_script.return_value = {
            "url": url,
            "title": "Authentication Error - OpenAI",
            "text": body,
        }
        info = roxy._inspect_auth_page_failure(driver)
        self.assertEqual(info.get("kind"), "account_unusable")
        self.assertEqual(info.get("error_code"), "account_deactivated")

        with self.assertRaises(AccountUnusableError) as ctx:
            roxy._raise_if_auth_page_terminal(driver, where="unit")
        self.assertEqual(ctx.exception.error_code, "account_deactivated")

    def test_raise_rate_limit(self):
        url = _payload_url("rate_limit_exceeded")
        driver = MagicMock()
        driver.current_url = url
        driver.execute_script.return_value = {"url": url, "title": "", "text": ""}
        with self.assertRaises(RateLimitError) as ctx:
            roxy._raise_if_auth_page_terminal(driver, where="unit")
        self.assertEqual(ctx.exception.error_code, "rate_limit_exceeded")


if __name__ == "__main__":
    unittest.main()
