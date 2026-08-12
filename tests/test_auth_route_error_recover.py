# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock

from core import roxy_registration as reg


class _Driver:
    def __init__(self, url: str, script_result=None):
        self.current_url = url
        self._script_result = script_result

    def execute_script(self, script, *args):
        if self._script_result is not None:
            return self._script_result
        return {
            "url": self.current_url,
            "title": "Oops, an error occurred! - OpenAI",
            "inputs": [],
            "buttons": [{"text": "Try again"}],
            "errors": [],
            "text": 'Oops, an error occurred!\nRoute Error (400 Invalid content type: text/html; charset=UTF-8)',
        }


class AuthRouteErrorRecoverTests(unittest.TestCase):
    def test_detect_route_error_page(self):
        d = _Driver("https://auth.openai.com/email-verification")
        self.assertTrue(reg._is_auth_route_error_page(d))

    def test_otp_not_passed_on_route_error(self):
        d = _Driver("https://auth.openai.com/email-verification")
        self.assertFalse(reg._otp_flow_already_passed(d))

    def test_safe_element_text_without_text_attr(self):
        el = MagicMock(spec=["get_attribute"])
        el.get_attribute.side_effect = lambda k: "Try again" if k == "data-dd-action-name" else None
        # no .text attribute on spec
        self.assertEqual(reg._safe_element_text(el), "Try again")

    def test_dom_element_handle_rejects_json_value_dict(self):
        # Cloak 把 DOM 塞进 dict 回传后，json_value 得到普通对象，不能再当元素点
        self.assertFalse(reg._is_dom_element_handle(None))
        self.assertFalse(reg._is_dom_element_handle({"ok": True, "input": {}}))
        self.assertFalse(reg._is_dom_element_handle({}))
        self.assertFalse(reg._is_dom_element_handle("button"))

        class CloakElement:
            pass

        self.assertTrue(reg._is_dom_element_handle(CloakElement()))

    def test_human_click_rejects_non_element(self):
        with self.assertRaises(RuntimeError) as ctx:
            reg._human_click(MagicMock(), {"fake": True}, label="password_submit")
        self.assertIn("非 DOM 元素", str(ctx.exception))

    def test_human_scroll_to_ignores_non_element(self):
        # 不应抛错
        reg._human_scroll_to(MagicMock(), {"fake": True})


if __name__ == "__main__":
    unittest.main()
