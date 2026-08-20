# -*- coding: utf-8 -*-
"""查活协议应先预热登录页，403 耗尽后再走指纹浏览器。"""
import unittest
from unittest.mock import MagicMock, patch

from core import account_liveness as al


class _FakeSession:
    def __init__(self, proxy=None, **kwargs):
        self.proxy = "" if proxy == "" else (proxy or "socks5h://pool.example:1080")
        self.device_id = "dev"
        self.exit_geo = {"ip": "1.2.3.4"}
        self.session = MagicMock()

    def fingerprint_summary(self):
        return {"device_id": self.device_id}

    def fingerprint_summary_text(self):
        return "device_id=dev"


class LiveCheckProtocolWarmupTests(unittest.TestCase):
    def test_preflight_warms_up_before_providers(self):
        order = []

        def _warmup(session):
            order.append("warmup")

        def _providers(session):
            order.append("providers")
            return {"openai": {}}

        def _csrf(session):
            order.append("csrf")
            return "csrf-token"

        def _signin(session, csrf, email):
            order.append("signin")
            self.assertEqual(csrf, "csrf-token")
            self.assertEqual(email, "a@b.com")
            return "https://auth.openai.com/api/accounts/authorize?x=1"

        with patch.object(al, "BrowserSession", _FakeSession), \
             patch.object(al, "_warmup_live_check_session", side_effect=_warmup), \
             patch.object(al, "get_providers", side_effect=_providers), \
             patch.object(al, "get_csrf_token", side_effect=_csrf), \
             patch.object(al, "signin_openai", side_effect=_signin):
            session, url = al._network_preflight_with_retry("a@b.com", proxy="", max_attempts=1)

        self.assertEqual(order, ["warmup", "providers", "csrf", "signin"])
        self.assertTrue(url.startswith("https://auth.openai.com/"))
        self.assertEqual(session.device_id, "dev")

    def test_warmup_403_retries_new_session_before_providers(self):
        created = []
        providers_calls = []

        class _Sess(_FakeSession):
            def __init__(self, proxy=None, **kwargs):
                super().__init__(proxy=proxy, **kwargs)
                created.append(self.proxy)

        def _warmup(session):
            if len(created) == 1:
                raise RuntimeError("chatgpt-login status=403, body=Just a moment")
            return None

        def _providers(session):
            providers_calls.append(session.proxy)
            return {}

        with patch.object(al, "BrowserSession", _Sess), \
             patch.object(al, "_warmup_live_check_session", side_effect=_warmup), \
             patch.object(al, "get_providers", side_effect=_providers), \
             patch.object(al, "get_csrf_token", return_value="csrf"), \
             patch.object(al, "signin_openai", return_value="https://auth.openai.com/api/accounts/authorize?x=1"), \
             patch.object(al.time, "sleep"):
            al._network_preflight_with_retry("a@b.com", proxy="", max_attempts=2)

        self.assertEqual(created, ["", ""])
        self.assertEqual(providers_calls, [""])

    def test_check_account_liveness_falls_back_to_cloak_after_protocol_403(self):
        material = {
            "password": "",
            "totp_secret": None,
            "email_source": "outlook",
            "has_mail": True,
            "prefer_password": False,
        }
        cloak_info = {
            "accessToken": "at-cloak",
            "user": {"id": "u-cloak"},
            "account": {"planType": "plus"},
        }
        meta = {"proxy_used": "socks5h://pool", "device_id": "cloak-profile", "driver": "cloak"}

        with patch.object(al, "_load_account_login_material", return_value=material), \
             patch.object(al, "_network_preflight_with_retry", side_effect=RuntimeError("HTTP Error 403: providers")), \
             patch.object(al, "_login_via_cloak_browser", return_value=(cloak_info, meta)) as mock_cloak, \
             patch.object(al, "_login_via_email_otp") as mock_otp, \
             patch.object(al, "follow_authorize") as mock_follow:
            result = al.check_account_liveness("a@b.com", proxy=None, clear_log=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "live")
        self.assertEqual(result["access_token"], "at-cloak")
        self.assertEqual(result["driver"], "cloak")
        mock_cloak.assert_called_once()
        mock_otp.assert_not_called()
        mock_follow.assert_not_called()

    def test_check_account_liveness_does_not_cloak_on_business_error(self):
        material = {
            "password": "",
            "totp_secret": None,
            "email_source": "outlook",
            "has_mail": True,
            "prefer_password": False,
        }
        with patch.object(al, "_load_account_login_material", return_value=material), \
             patch.object(al, "_network_preflight_with_retry", side_effect=RuntimeError("登录落到密码页但账号无 password")), \
             patch.object(al, "_login_via_cloak_browser") as mock_cloak:
            result = al.check_account_liveness("a@b.com", proxy=None, clear_log=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        mock_cloak.assert_not_called()


if __name__ == "__main__":
    unittest.main()
