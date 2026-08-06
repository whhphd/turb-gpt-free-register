# -*- coding: utf-8 -*-
"""查活代理语义：proxy='' 必须直连；sticky 403 后必须换池。"""
import unittest
from unittest.mock import MagicMock, patch


class LiveCheckProxySemanticsTests(unittest.TestCase):
    def test_preflight_empty_proxy_means_direct(self):
        from core import account_liveness as al

        created = []

        class _Sess:
            def __init__(self, proxy=None, **kwargs):
                # 复刻 BrowserSession 语义
                if proxy is None:
                    self.proxy = "socks5h://pool.example:1080"
                else:
                    self.proxy = proxy
                self.device_id = "dev"
                self.exit_geo = {"ip": "1.2.3.4"}
                self.session = MagicMock()
                created.append(proxy)

        with patch.object(al, "BrowserSession", _Sess), patch.object(al, "get_providers", side_effect=RuntimeError("HTTP Error 403: ")), patch.object(al, "get_csrf_token"), patch.object(al, "signin_openai"), patch.object(al.time, "sleep"):
            with self.assertRaises(RuntimeError):
                al._network_preflight_with_retry("a@b.com", proxy="", max_attempts=2)

        # 两轮都必须传 ""，不能变成 None
        self.assertEqual(created, ["", ""])

    def test_sticky_proxy_switches_after_403(self):
        from core import account_liveness as al

        created = []
        picks = ["http://sticky-b", "http://sticky-c"]

        class _Sess:
            def __init__(self, proxy=None, **kwargs):
                if proxy is None:
                    self.proxy = "http://from-none"
                else:
                    self.proxy = proxy
                self.device_id = "dev"
                self.exit_geo = {"ip": "1.2.3.4"}
                self.session = MagicMock()
                created.append(self.proxy)

        def _fake_pick(exclude=None):
            return picks.pop(0) if picks else "http://sticky-z"

        with patch.object(al, "BrowserSession", _Sess), patch.object(al, "_pick_live_check_proxy", side_effect=lambda exclude: _fake_pick(exclude)), patch.object(al, "get_providers", side_effect=RuntimeError("HTTP Error 403: ")), patch.object(al.time, "sleep"), patch("config.proxy.mark_proxy_cooldown"):
            with self.assertRaises(RuntimeError):
                al._network_preflight_with_retry(
                    "a@b.com",
                    proxy="http://sticky-a",
                    max_attempts=3,
                )

        # 第 1 轮用指定 sticky-a，之后必须换到池里其它 sticky
        self.assertEqual(created[0], "http://sticky-a")
        self.assertEqual(created[1], "http://sticky-b")
        self.assertEqual(created[2], "http://sticky-c")


if __name__ == "__main__":
    unittest.main()
