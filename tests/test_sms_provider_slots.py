# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from config import codex as codex_config
from core import sms_provider


class _Resp:
    status_code = 200

    def __init__(self, text: str):
        self.text = text

    def json(self):
        return json.loads(self.text)


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        if not self.responses:
            raise AssertionError(f"unexpected request: {params}")
        return _Resp(self.responses.pop(0))

    def close(self):
        self.closed = True


class SmsProviderSlotTests(unittest.TestCase):
    def setUp(self):
        sms_provider.clear_country_no_numbers(provider="smsbower")
        sms_provider._SLOT_FAILS.pop("smsbower", None)
        sms_provider._ACTIVATION_META.clear()

    def test_list_provider_candidates_prefers_cheaper_valid_slot(self):
        v3 = {
            "52": {
                "dr": {
                    "100": {"count": 5, "price": 0.01, "provider_id": 100},   # 低库存
                    "200": {"count": 50, "price": 0.10, "provider_id": 200},
                    "300": {"count": 80, "price": 0.20, "provider_id": 300},
                    "400": {"count": 90000, "price": 0.004, "provider_id": 400},  # 异常低价
                }
            }
        }
        http = _Http([json.dumps(v3)])
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), \
             patch.object(codex_config, "SMSBOWER_API_KEY", "k"), \
             patch.object(codex_config, "SMSBOWER_API_BASE", "https://smsbower.page/stubs/handler_api.php"), \
             patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(codex_config, "SMS_MAX_PRICE", "0.35"), \
             patch.object(codex_config, "SMS_MIN_PRICE", ""), \
             patch.object(codex_config, "SMS_PREFERRED_COUNTRIES", "52"), \
             patch.object(codex_config, "SMS_PROVIDER_MIN_STOCK", 15), \
             patch.object(codex_config, "SMS_AUTO_COUNTRY_MIN_STOCK", 0), \
             patch.object(codex_config, "SMS_PRICE_FLOOR_RATIO", 0.25), \
             patch.object(codex_config, "SMS_AUTO_COUNTRY", False):
            rows = sms_provider.list_provider_candidates(
                http, provider="smsbower", service="dr", countries=["52"], include_low_quality=False
            )
        pids = [r["provider_id"] for r in rows]
        self.assertIn("200", pids)
        self.assertIn("300", pids)
        self.assertNotIn("100", pids)  # stock too low
        # 0.004 相对中位异常低且库存巨大，首轮应被过滤
        self.assertNotIn("400", pids)
        self.assertEqual(rows[0]["provider_id"], "200")

    def test_acquire_uses_provider_ids_and_max_price(self):
        v3 = {
            "52": {
                "dr": {
                    "3193": {"count": 100, "price": 0.12, "provider_id": 3193},
                    "3309": {"count": 50, "price": 0.25, "provider_id": 3309},
                }
            }
        }
        http = _Http([
            json.dumps(v3),  # getPricesV3
            "ACCESS_NUMBER:act-1:66811112222",
        ])
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), \
             patch.object(codex_config, "SMSBOWER_API_KEY", "k"), \
             patch.object(codex_config, "SMSBOWER_API_BASE", "https://smsbower.page/stubs/handler_api.php"), \
             patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(codex_config, "SMS_COUNTRY", "52"), \
             patch.object(codex_config, "SMS_MAX_PRICE", "0.35"), \
             patch.object(codex_config, "SMS_MIN_PRICE", ""), \
             patch.object(codex_config, "SMS_PREFERRED_COUNTRIES", "52"), \
             patch.object(codex_config, "SMS_PROVIDER_MIN_STOCK", 10), \
             patch.object(codex_config, "SMS_AUTO_COUNTRY_MIN_STOCK", 0), \
             patch.object(codex_config, "SMS_PRICE_FLOOR_RATIO", 0.25), \
             patch.object(codex_config, "SMS_AUTO_COUNTRY", False), \
             patch.object(codex_config, "SMS_ACQUIRE_MAX_SLOTS", 5):
            aid, phone = sms_provider.acquire_number(http=http, country="52")
        self.assertEqual(aid, "act-1")
        self.assertEqual(phone, "66811112222")
        get_number = [c for c in http.calls if c["params"].get("action") == "getNumber"][0]
        self.assertEqual(get_number["params"]["providerIds"], "3193")
        self.assertEqual(get_number["params"]["country"], "52")
        self.assertIn("maxPrice", get_number["params"])
        meta = sms_provider._ACTIVATION_META.get("act-1")
        self.assertEqual(meta["provider_id"], "3193")

    def test_send_reject_cools_slot_not_whole_country(self):
        sms_provider.remember_activation_meta("a1", {
            "country": "12", "provider_id": "3209", "channel": "smsbower",
        })
        sms_provider.mark_activation_send_rejected("a1", reason="send_not_accepted")
        cool = sms_provider._slot_bucket("smsbower")
        self.assertIn("12:3209", cool)
        # 同国其它供应商不应被整国排除
        self.assertNotIn("12:-", cool)


if __name__ == "__main__":
    unittest.main()
