# -*- coding: utf-8 -*-
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from core import roxy_codex_oauth as rbx
from core.codex_oauth import PhoneOtpNeedReauth


class _FakeDriver:
    def __init__(self, title="", body="", url="https://auth.openai.com/phone-verification"):
        self.title = title
        self.current_url = url
        self._body = body

    def execute_script(self, script):
        return self._body


class BrowserPhoneReauthTests(unittest.TestCase):
    def test_page_looks_like_http_500(self):
        self.assertTrue(rbx._page_looks_like_http_500(_FakeDriver(title="500", body="")))
        self.assertTrue(rbx._page_looks_like_http_500(
            _FakeDriver(title="Error", body="Internal Server Error")
        ))
        self.assertFalse(rbx._page_looks_like_http_500(
            _FakeDriver(title="Verify your phone", body="Enter the code")
        ))

    def test_send_committed_needs_reauth(self):
        driver = _FakeDriver()
        self.assertTrue(rbx._phone_session_needs_reauth(
            RuntimeError("empty_phone_otp"), driver, send_committed=True,
        ))

    def test_back_500_needs_reauth_even_before_send(self):
        driver = _FakeDriver(title="500", body="Internal Server Error")
        self.assertTrue(rbx._phone_session_needs_reauth(
            RuntimeError("otp_no_add_phone_form: 验证码页无法退回输入表单"),
            driver,
            send_committed=False,
        ))

    def test_fraud_guard_on_add_phone_does_not_reauth(self):
        driver = _FakeDriver(
            title="Add phone",
            body="suspicious behavior",
            url="https://auth.openai.com/add-phone",
        )
        with patch.object(rbx, "_is_phone_code_page", return_value=False), \
             patch.object(rbx, "_page_looks_like_http_500", return_value=False):
            self.assertFalse(rbx._phone_session_needs_reauth(
                RuntimeError("fraud_guard: suspicious behavior"),
                driver,
                send_committed=False,
            ))

    def test_phone_timeout_after_send_raises_reauth_and_does_not_buy_again(self):
        driver = MagicMock()
        driver.current_url = "https://auth.openai.com/phone-verification"
        http = MagicMock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(rbx, "_has_strict_add_phone_form", return_value=True))
            stack.enter_context(patch.object(rbx, "_is_phone_code_page", return_value=True))
            stack.enter_context(patch.object(rbx, "_ensure_add_phone_input"))
            stack.enter_context(patch.object(rbx, "_set_phone_value", return_value={"e164": "+56911111111"}))
            stack.enter_context(patch.object(rbx, "_blur_active_input_and_wait"))
            stack.enter_context(patch.object(rbx, "_verify_add_phone_value_before_submit", return_value={}))
            stack.enter_context(patch.object(rbx, "_dismiss_phone_country_dropdown"))
            stack.enter_context(patch.object(rbx, "_select_sms_channel_or_raise"))
            stack.enter_context(patch.object(rbx, "_click_add_phone_continue_button", return_value={"ok": True}))
            stack.enter_context(patch.object(rbx, "_wait_page_settle_after_submit"))
            stack.enter_context(patch.object(rbx, "_wait_after_phone_send", return_value="code_page"))
            stack.enter_context(patch.object(rbx, "_sleep_before_phone_retry"))
            stack.enter_context(patch.object(rbx.sms_provider, "_http", return_value=http))
            acquire = stack.enter_context(patch.object(
                rbx.sms_provider, "acquire_number", return_value=("act1", "56911111111"),
            ))
            stack.enter_context(patch.object(rbx.sms_provider, "mark_sms_sent"))
            stack.enter_context(patch.object(rbx.sms_provider, "wait_for_sms_code", return_value=""))
            cancel = stack.enter_context(patch.object(rbx.sms_provider, "cancel"))
            stack.enter_context(patch.object(rbx.sms_provider, "mark_activation_send_rejected"))
            stack.enter_context(patch.object(rbx.sms_provider._cfg, "SMS_MAX_RETRIES", 10))
            stack.enter_context(patch.object(rbx.sms_provider._cfg, "SMS_CODE_WAIT", 12))
            stack.enter_context(patch.object(rbx.sms_provider._cfg, "SMS_POLL_INTERVAL", 5))
            stack.enter_context(patch.object(rbx, "human_delay"))
            stack.enter_context(patch.object(rbx, "_sms_channel_selection_state", return_value={}))
            with self.assertRaises(PhoneOtpNeedReauth) as ctx:
                rbx._do_phone_verification_if_present(driver, sms_attempt_start=3)
        self.assertEqual(ctx.exception.sms_attempts_used, 3)
        acquire.assert_called_once()
        cancel.assert_called()


if __name__ == "__main__":
    unittest.main()
