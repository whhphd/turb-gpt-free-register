# -*- coding: utf-8 -*-
"""协议 Codex 登录：按页面分支，已过登录则不强制绑号。"""
import unittest
from unittest.mock import MagicMock, patch

from core import codex_oauth as cx


class ClassifyAuthStepTests(unittest.TestCase):
    def test_email_otp(self):
        self.assertEqual(
            cx._classify_auth_step({"page": {"type": "email_otp_verification"}, "continue_url": "https://auth.openai.com/email-verification"}),
            "email_otp",
        )

    def test_password(self):
        self.assertEqual(
            cx._classify_auth_step({"page": {"type": "login_password"}, "continue_url": "https://auth.openai.com/log-in/password"}),
            "password",
        )

    def test_plain_log_in_is_login_email(self):
        self.assertEqual(
            cx._classify_auth_step({"continue_url": "https://auth.openai.com/log-in"}),
            "login_email",
        )

    def test_totp(self):
        self.assertEqual(
            cx._classify_auth_step({"page": {"type": "mfa_challenge"}, "continue_url": "https://auth.openai.com/multi-factor/totp"}),
            "totp",
        )

    def test_phone(self):
        self.assertEqual(
            cx._classify_auth_step({"continue_url": "https://auth.openai.com/add-phone"}),
            "phone",
        )

    def test_workspace(self):
        self.assertEqual(
            cx._classify_auth_step({"continue_url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"}),
            "workspace",
        )

    def test_chatgpt_web_callback_is_not_workspace(self):
        self.assertNotEqual(
            cx._classify_auth_step({
                "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=ac_x",
            }),
            "workspace",
        )

    def test_add_phone_after_codex_authorize(self):
        self.assertEqual(
            cx._classify_auth_step({"continue_url": "https://auth.openai.com/add-phone"}),
            "phone",
        )


class ProtocolLoginFlowTests(unittest.TestCase):
    def test_password_then_totp_skips_email_otp(self):
        session = MagicMock()
        material = {
            "password": "Passw0rd!",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "prefer_password": True,
            "has_mail": False,
        }
        otp_provider = MagicMock(side_effect=AssertionError("不应走邮箱 OTP"))
        with patch.object(cx, "_load_codex_login_material", return_value=material), \
             patch.object(cx, "_submit_email", return_value={
                 "page": {"type": "login_password"},
                 "continue_url": "https://auth.openai.com/log-in/password",
             }), \
             patch.object(cx, "verify_login_password", return_value={
                 "page": {"type": "mfa_challenge"},
                 "continue_url": "https://auth.openai.com/mfa-challenge/abc",
             }) as mock_pwd, \
             patch.object(cx, "generate_totp_code", return_value="654321"), \
             patch.object(cx, "verify_login_totp", return_value={
                 "continue_url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
             }) as mock_totp, \
             patch.object(cx, "human_delay"):
            data = cx._complete_protocol_login(session, "a@b.com", otp_provider)

        mock_pwd.assert_called_once()
        mock_totp.assert_called_once()
        self.assertEqual(mock_totp.call_args.args[1], "654321")
        otp_provider.assert_not_called()
        self.assertIn("consent", str(data.get("continue_url") or ""))

    def test_password_page_without_openai_password_uses_email_otp(self):
        session = MagicMock()
        material = {"password": "", "totp_secret": None, "prefer_password": False, "has_mail": True}
        otp_provider = MagicMock(return_value="111222")
        with patch.object(cx, "_load_codex_login_material", return_value=material), \
             patch.object(cx, "_submit_email", return_value={
                 "page": {"type": "login_password"},
                 "continue_url": "https://auth.openai.com/log-in/password",
             }), \
             patch.object(cx, "_request_passwordless_login_otp", return_value={
                 "page": {"type": "email_otp_verification"},
                 "continue_url": "https://auth.openai.com/email-verification",
             }) as mock_otp_intent, \
             patch.object(cx, "send_email_otp") as mock_send, \
             patch.object(cx, "_submit_email_otp", return_value={
                 "continue_url": "https://auth.openai.com/add-phone",
             }) as mock_otp, \
             patch.object(cx, "human_delay"):
            data = cx._complete_protocol_login(session, "o@b.com", otp_provider)

        mock_otp_intent.assert_called_once()
        mock_send.assert_not_called()
        mock_otp.assert_called_once()
        self.assertEqual(cx._classify_auth_step(data), "phone")

    def test_authorize_password_page_skips_submit_email(self):
        session = MagicMock()
        material = {"password": "", "totp_secret": None, "prefer_password": False, "has_mail": True}
        otp_provider = MagicMock(return_value="777888")
        with patch.object(cx, "_load_codex_login_material", return_value=material), \
             patch.object(cx, "_submit_email") as mock_submit, \
             patch.object(cx, "_request_passwordless_login_otp", return_value={
                 "page": {"type": "email_otp_verification"},
                 "continue_url": "https://auth.openai.com/email-verification",
             }) as mock_intent, \
             patch.object(cx, "_submit_email_otp", return_value={
                 "continue_url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
             }), \
             patch.object(cx, "human_delay"):
            data = cx._complete_protocol_login(
                session,
                "m@b.com",
                otp_provider,
                start_url="https://auth.openai.com/log-in/password",
            )
        mock_submit.assert_not_called()
        mock_intent.assert_called_once()
        self.assertEqual(cx._classify_auth_step(data), "workspace")

    def test_authorize_already_on_email_verification_skips_submit_email(self):
        session = MagicMock()
        material = {"password": "", "totp_secret": None, "prefer_password": False, "has_mail": True}
        otp_provider = MagicMock(return_value="555666")
        with patch.object(cx, "_load_codex_login_material", return_value=material), \
             patch.object(cx, "_submit_email") as mock_submit, \
             patch.object(cx, "_submit_email_otp", return_value={
                 "continue_url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
             }), \
             patch.object(cx, "human_delay"):
            data = cx._complete_protocol_login(
                session,
                "m@b.com",
                otp_provider,
                start_url="https://auth.openai.com/email-verification",
            )
        mock_submit.assert_not_called()
        self.assertEqual(cx._classify_auth_step(data), "workspace")

    def test_ensure_oai_context_url_adds_login_hint(self):
        session = MagicMock()
        session.device_id = "did-1"
        session.auth_session_logging_id = "log-1"
        url = "https://auth.openai.com/oauth/authorize?client_id=app&prompt=login"
        out = cx._ensure_oai_context_url(url, session, email="a@b.com")
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(out).query)
        self.assertEqual(qs["login_hint"], ["a@b.com"])
        self.assertEqual(qs["screen_hint"], ["login_or_signup"])
        self.assertEqual(qs["ccaps"], ["login_methods"])

    def test_codex_sso_url_strips_login_prompt(self):
        session = MagicMock()
        session.device_id = "did-1"
        session.auth_session_logging_id = "log-1"
        url = (
            "https://auth.openai.com/oauth/authorize?client_id=app_codex"
            "&prompt=login&login_hint=a%40b.com&screen_hint=login_or_signup&ccaps=login_methods"
            "&code_challenge=abc"
        )
        out = cx._codex_sso_authorize_url(url, session)
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(out).query)
        self.assertNotIn("prompt", qs)
        self.assertNotIn("login_hint", qs)
        self.assertNotIn("screen_hint", qs)
        self.assertNotIn("ccaps", qs)
        self.assertEqual(qs["ext-oai-did"], ["did-1"])
        self.assertEqual(qs["code_challenge"], ["abc"])

    def test_codex_sso_url_keeps_login_hint_without_prompt(self):
        session = MagicMock()
        session.device_id = "did-1"
        session.auth_session_logging_id = "log-1"
        url = "https://auth.openai.com/oauth/authorize?client_id=app_codex&prompt=login"
        out = cx._codex_sso_authorize_url(url, session, email="a@b.com")
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(out).query)
        self.assertNotIn("prompt", qs)
        self.assertEqual(qs["login_hint"], ["a@b.com"])

    def test_plain_email_otp_then_consent_does_not_force_phone_classify(self):
        session = MagicMock()
        material = {"password": "", "totp_secret": None, "prefer_password": False, "has_mail": True}
        otp_provider = MagicMock(return_value="333444")
        with patch.object(cx, "_load_codex_login_material", return_value=material), \
             patch.object(cx, "_submit_email", return_value={
                 "page": {"type": "email_otp_verification"},
                 "continue_url": "https://auth.openai.com/email-verification",
             }), \
             patch.object(cx, "_submit_email_otp", return_value={
                 "continue_url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
             }), \
             patch.object(cx, "human_delay"):
            data = cx._complete_protocol_login(session, "m@b.com", otp_provider)
        self.assertEqual(cx._classify_auth_step(data), "workspace")


class ChatgptWebOtpLoginTests(unittest.TestCase):
    def test_chatgpt_web_otp_uses_email_verification_landing(self):
        session = MagicMock()
        otp_provider = MagicMock(return_value="418525")
        with patch("core.chatgpt_auth.get_providers"), \
             patch("core.chatgpt_auth.get_csrf_token", return_value="csrf"), \
             patch("core.chatgpt_auth.signin_openai", return_value="https://auth.openai.com/oauth/authorize?login_hint=a@b.com") as mock_signin, \
             patch.object(cx, "follow_authorize", return_value="https://auth.openai.com/email-verification") as mock_follow, \
             patch.object(cx, "validate_email_otp", return_value={
                 "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=ac_x",
                 "page": {"type": "external_url"},
             }) as mock_validate, \
             patch("core.account_export.follow_oauth_callback") as mock_cb, \
             patch.object(cx, "send_email_otp") as mock_send, \
             patch.object(cx, "human_delay"):
            data = cx._login_via_chatgpt_web_otp(session, "a@b.com", otp_provider)
        mock_signin.assert_called_once()
        mock_follow.assert_called_once()
        mock_validate.assert_called_once_with(session, "418525")
        mock_cb.assert_called_once()
        mock_send.assert_not_called()
        self.assertIn("callback", str(data.get("continue_url") or ""))

    def test_chatgpt_web_otp_rejects_password_landing(self):
        session = MagicMock()
        with patch("core.chatgpt_auth.get_providers"), \
             patch("core.chatgpt_auth.get_csrf_token", return_value="csrf"), \
             patch("core.chatgpt_auth.signin_openai", return_value="https://auth.openai.com/oauth/authorize"), \
             patch.object(cx, "follow_authorize", return_value="https://auth.openai.com/log-in/password"):
            with self.assertRaisesRegex(RuntimeError, "email-verification"):
                cx._login_via_chatgpt_web_otp(session, "a@b.com", MagicMock())


class PhoneSendReasonTests(unittest.TestCase):
    def test_invalid_auth_step_not_generic_reject(self):
        reason = cx._phone_failure_reason(
            "Invalid authorization step. invalid_request_error invalid_auth_step https://auth.openai.com/log-in",
            400,
        )
        self.assertEqual(reason, "invalid_auth_step")

    def test_phone_in_use_still_rotates(self):
        reason = cx._phone_failure_reason(
            "Phone number already in use. Please use a different phone number. invalid_request_error phone_number_in_use",
            400,
        )
        self.assertTrue(reason)


class PhoneIfNeededTests(unittest.TestCase):
    def test_skip_when_already_on_workspace(self):
        session = MagicMock()
        with patch.object(cx, "_do_phone_verification") as mock_phone, \
             patch.object(cx, "_probe_phone_required", return_value=True):
            did = cx._do_phone_verification_if_needed(
                session,
                {"continue_url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"},
            )
        self.assertFalse(did)
        mock_phone.assert_not_called()

    def test_run_when_payload_asks_phone(self):
        session = MagicMock()
        with patch.object(cx, "_do_phone_verification") as mock_phone:
            did = cx._do_phone_verification_if_needed(
                session,
                {"continue_url": "https://auth.openai.com/add-phone"},
            )
        self.assertTrue(did)
        mock_phone.assert_called_once_with(session, sms_attempt_start=1)

    def test_probe_false_skips(self):
        session = MagicMock()
        with patch.object(cx, "_do_phone_verification") as mock_phone, \
             patch.object(cx, "_probe_phone_required", return_value=False):
            did = cx._do_phone_verification_if_needed(session, {})
        self.assertFalse(did)
        mock_phone.assert_not_called()

    def test_skip_phone_when_still_on_log_in(self):
        session = MagicMock()
        with patch.object(cx, "_do_phone_verification") as mock_phone, \
             patch.object(cx, "_probe_phone_required", return_value=True):
            did = cx._do_phone_verification_if_needed(
                session,
                {"continue_url": "https://auth.openai.com/log-in"},
            )
        self.assertFalse(did)
        mock_phone.assert_not_called()


class AddPhoneResetTests(unittest.TestCase):
    def test_auth_page_step(self):
        self.assertEqual(cx._auth_page_step("https://auth.openai.com/add-phone"), "add_phone")
        self.assertEqual(cx._auth_page_step("https://auth.openai.com/phone-verification"), "phone_otp")
        self.assertEqual(cx._auth_page_step("https://auth.openai.com/log-in"), "login")
        self.assertEqual(
            cx._auth_page_step("https://auth.openai.com/sign-in-with-chatgpt/codex/consent"),
            "workspace",
        )

    def test_ensure_add_phone_stops_on_login(self):
        session = MagicMock()
        with patch.object(cx, "_navigate_auth_page", return_value="https://auth.openai.com/log-in"):
            with self.assertRaisesRegex(RuntimeError, "登录页"):
                cx._ensure_add_phone_step(session)

    def test_ensure_add_phone_retries_from_otp_page(self):
        session = MagicMock()
        with patch.object(cx, "_navigate_auth_page", side_effect=[
            "https://auth.openai.com/phone-verification",
            "https://auth.openai.com/add-phone",
        ]) as nav:
            landed = cx._ensure_add_phone_step(session)
        self.assertIn("add-phone", landed)
        self.assertEqual(nav.call_count, 2)
        self.assertIn("phone-verification", str(nav.call_args.kwargs.get("referer") or ""))

    def test_ensure_add_phone_stops_if_stuck_on_otp(self):
        session = MagicMock()
        with patch.object(cx, "_navigate_auth_page", return_value="https://auth.openai.com/phone-verification"):
            with self.assertRaisesRegex(RuntimeError, "未能回到 add-phone"):
                cx._ensure_add_phone_step(session)

    def test_wait_sms_stops_if_session_falls_to_login(self):
        session = MagicMock()
        with patch.object(cx.sms_provider, "wait_for_sms_code", side_effect=cx.sms_provider.SmsCodeTimeout("t")), \
             patch.object(cx, "_keep_phone_otp_session", return_value="login"), \
             patch.object(cx._cfg, "SMS_CODE_WAIT", 12), \
             patch.object(cx._cfg, "SMS_POLL_INTERVAL", 5):
            with self.assertRaisesRegex(RuntimeError, "登录页"):
                cx._wait_sms_code_keep_session(session, "act", MagicMock())

    def test_phone_loop_resets_add_phone_before_buying_and_lands_otp_after_send(self):
        session = MagicMock()
        send_ok = MagicMock(status_code=200, text="")
        send_ok.json.return_value = {}
        val_ok = MagicMock(status_code=200, text="")
        val_ok.json.return_value = {}
        http = MagicMock()
        with patch.object(cx._cfg, "SMS_MAX_RETRIES", 3), \
             patch.object(cx._cfg, "SMS_CODE_WAIT", 60), \
             patch.object(cx._cfg, "SMS_POLL_INTERVAL", 5), \
             patch.object(cx, "_sms_provider_name", return_value="smsbower"), \
             patch.object(cx.sms_provider, "_http", return_value=http), \
             patch.object(cx.sms_provider, "acquire_number", return_value=("act1", "255784894261")) as acquire, \
             patch.object(cx.sms_provider, "wait_for_sms_code", return_value="123456"), \
             patch.object(cx.sms_provider, "mark_sms_sent") as mark_sent, \
             patch.object(cx.sms_provider, "complete") as complete, \
             patch.object(cx.sms_provider, "cancel") as cancel, \
             patch.object(cx, "_ensure_add_phone_step") as ensure, \
             patch.object(cx, "_keep_phone_otp_session", return_value="phone_otp") as keep, \
             patch.object(cx, "_post_json", side_effect=[send_ok, val_ok]) as post:
            cx._do_phone_verification(session)
        ensure.assert_called_once_with(session)
        acquire.assert_called_once()
        self.assertEqual(ensure.call_args_list[0][0][0], session)
        self.assertLessEqual(ensure.call_count, acquire.call_count)
        keep.assert_called()
        mark_sent.assert_called_once()
        complete.assert_called_once()
        cancel.assert_not_called()
        self.assertEqual(
            post.call_args_list[0].args[1],
            "https://auth.openai.com/api/accounts/add-phone/send",
        )

    def test_send_fail_rotates_number_within_sms_retries(self):
        session = MagicMock()
        send_bad = MagicMock(status_code=400, text="fraud_guard")
        send_bad.json.return_value = {
            "error": {"message": "suspicious behavior", "code": "fraud_guard"}
        }
        send_ok = MagicMock(status_code=200, text="")
        send_ok.json.return_value = {}
        val_ok = MagicMock(status_code=200, text="")
        val_ok.json.return_value = {}
        with patch.object(cx._cfg, "SMS_MAX_RETRIES", 10), \
             patch.object(cx._cfg, "SMS_CODE_WAIT", 60), \
             patch.object(cx._cfg, "SMS_POLL_INTERVAL", 5), \
             patch.object(cx, "_sms_provider_name", return_value="smsbower"), \
             patch.object(cx.sms_provider, "_http", return_value=MagicMock()), \
             patch.object(cx.sms_provider, "acquire_number", side_effect=[("a1", "1"), ("a2", "2")]) as acquire, \
             patch.object(cx.sms_provider, "wait_for_sms_code", return_value="111111"), \
             patch.object(cx.sms_provider, "mark_sms_sent"), \
             patch.object(cx.sms_provider, "complete"), \
             patch.object(cx.sms_provider, "cancel") as cancel, \
             patch.object(cx, "_ensure_add_phone_step"), \
             patch.object(cx, "_keep_phone_otp_session", return_value="phone_otp"), \
             patch.object(cx, "_sleep_before_phone_retry"), \
             patch.object(cx, "_post_json", side_effect=[send_bad, send_ok, val_ok]):
            cx._do_phone_verification(session, sms_attempt_start=1)
        self.assertEqual(acquire.call_count, 2)
        self.assertEqual(acquire.call_args_list[0].kwargs.get("attempt_index"), 1)
        self.assertEqual(acquire.call_args_list[1].kwargs.get("attempt_index"), 2)
        cancel.assert_called()

    def test_sent_sms_timeout_reauth_counts_sms_retry_and_does_not_buy_again(self):
        session = MagicMock()
        send_ok = MagicMock(status_code=200, text="")
        send_ok.json.return_value = {}
        with patch.object(cx._cfg, "SMS_MAX_RETRIES", 10), \
             patch.object(cx._cfg, "SMS_CODE_WAIT", 12), \
             patch.object(cx._cfg, "SMS_POLL_INTERVAL", 5), \
             patch.object(cx, "_sms_provider_name", return_value="smsbower"), \
             patch.object(cx.sms_provider, "_http", return_value=MagicMock()), \
             patch.object(cx.sms_provider, "acquire_number", return_value=("act1", "56959345642")) as acquire, \
             patch.object(cx, "_wait_sms_code_keep_session", side_effect=cx.sms_provider.SmsCodeTimeout("t")), \
             patch.object(cx.sms_provider, "mark_sms_sent"), \
             patch.object(cx.sms_provider, "cancel"), \
             patch.object(cx, "_ensure_add_phone_step"), \
             patch.object(cx, "_keep_phone_otp_session", return_value="phone_otp"), \
             patch.object(cx, "_post_json", return_value=send_ok):
            with self.assertRaises(cx.PhoneOtpNeedReauth) as ctx:
                cx._do_phone_verification(session, sms_attempt_start=3)
        self.assertEqual(ctx.exception.sms_attempts_used, 3)
        acquire.assert_called_once()
        self.assertEqual(acquire.call_args.kwargs.get("attempt_index"), 3)

    def test_invalid_auth_step_reauth_does_not_keep_buying(self):
        session = MagicMock()
        send_bad = MagicMock(status_code=400, text="")
        send_bad.json.return_value = {
            "error": {
                "message": "Invalid authorization step.",
                "code": "invalid_auth_step",
            }
        }
        with patch.object(cx._cfg, "SMS_MAX_RETRIES", 10), \
             patch.object(cx, "_sms_provider_name", return_value="smsbower"), \
             patch.object(cx.sms_provider, "_http", return_value=MagicMock()), \
             patch.object(cx.sms_provider, "acquire_number", return_value=("act1", "1")) as acquire, \
             patch.object(cx.sms_provider, "cancel"), \
             patch.object(cx, "_ensure_add_phone_step"), \
             patch.object(cx, "_post_json", return_value=send_bad):
            with self.assertRaises(cx.PhoneOtpNeedReauth) as ctx:
                cx._do_phone_verification(session)
        self.assertEqual(ctx.exception.sms_attempts_used, 1)
        acquire.assert_called_once()


class CodexFingerprintFallbackTests(unittest.TestCase):
    def test_protocol_cf_403_does_not_fingerprint_inside_single_run(self):
        session = MagicMock()
        session.session = MagicMock()
        with patch.object(cx, "BrowserSession", return_value=session), \
             patch.object(cx, "network_preflight", side_effect=RuntimeError(
                 "chatgpt-login status=403, body=Just a moment"
             )), \
             patch.object(cx, "_codex_auth_url_source", return_value="local"), \
             patch.object(cx, "_generate_pkce", return_value=("v", "c")), \
             patch.object(cx, "_generate_state", return_value="st"), \
             patch.object(cx, "_build_authorize_url", return_value="https://auth.openai.com/x"), \
             patch.object(cx._cfg, "ENABLE_CODEX_AUTO", True), \
             patch("config.codex.CODEX_OAUTH_DRIVER", "protocol"), \
             patch.object(cx, "_run_codex_fingerprint_oauth") as fb:
            result = cx.run_codex_oauth("a@b.com", force=True)
        self.assertFalse(result["ok"])
        self.assertIn("403", str(result.get("message") or ""))
        fb.assert_not_called()

    def test_business_error_does_not_fingerprint_fallback(self):
        session = MagicMock()
        session.session = MagicMock()
        with patch.object(cx, "BrowserSession", return_value=session), \
             patch.object(cx, "network_preflight"), \
             patch.object(cx, "_codex_auth_url_source", return_value="local"), \
             patch.object(cx, "_generate_pkce", return_value=("v", "c")), \
             patch.object(cx, "_generate_state", return_value="st"), \
             patch.object(cx, "_build_authorize_url", return_value="https://auth.openai.com/x"), \
             patch.object(cx, "_bootstrap_authorize", side_effect=RuntimeError("登录落到密码页但账号无 password")), \
             patch.object(cx._cfg, "ENABLE_CODEX_AUTO", True), \
             patch("config.codex.CODEX_OAUTH_DRIVER", "protocol"), \
             patch.object(cx, "_run_codex_fingerprint_oauth") as fb:
            result = cx.run_codex_oauth("a@b.com", force=True)
        self.assertFalse(result["ok"])
        fb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
