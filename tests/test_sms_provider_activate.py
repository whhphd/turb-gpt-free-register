# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from core import sms_provider
from config import codex as codex_config
from config import env_loader
from webui import config_editor


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
        return _Resp(self.responses.pop(0))

    def close(self):
        self.closed = True


class ActivateSmsProviderTests(unittest.TestCase):
    def test_secret_registry_and_webui_fields(self):
        self.assertIn("HEROSMS_API_KEY", env_loader.SECRET_ENV_KEYS)
        self.assertIn("SMSBOWER_API_KEY", env_loader.SECRET_ENV_KEYS)
        fields = {f["key"]: f for f in config_editor.EDITABLE_FIELDS}
        self.assertIn("HEROSMS_API_KEY", fields)
        self.assertIn("SMSBOWER_API_KEY", fields)
        self.assertIn("SMS_MAX_PRICE", fields)
        self.assertIn("SMS_AUTO_COUNTRY", fields)
        self.assertIn("SMS_COUNTRY_WHITELIST", fields)
        self.assertIn("SMS_ALLOW_OUTSIDE_WHITELIST", fields)
        self.assertTrue(fields["HEROSMS_API_KEY"].get("secret"))
        self.assertTrue(fields["SMSBOWER_API_KEY"].get("secret"))

    def test_herosms_acquire_uses_separate_key_and_dr(self):
        http = _Http(["ACCESS_NUMBER:act-1:66812345678"])
        with patch.object(codex_config, "SMS_PROVIDER", "herosms"), \
             patch.object(codex_config, "HEROSMS_API_KEY", "hero-key"), \
             patch.object(codex_config, "HEROSMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"), \
             patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(codex_config, "SMS_COUNTRY", "52"), \
             patch.object(codex_config, "SMS_MAX_PRICE", "0.5"), \
             patch.object(codex_config, "SMS_AUTO_COUNTRY", False), \
             patch.object(codex_config, "SMS_API_KEY", "grizzly-should-not-use"), \
             patch.object(sms_provider, "list_provider_candidates", return_value=[]):
            activation_id, phone = sms_provider.acquire_number(http=http, country="52")

        self.assertEqual(activation_id, "act-1")
        self.assertEqual(phone, "66812345678")
        call = http.calls[0]
        self.assertIn("hero-sms.com", call["url"])
        self.assertEqual(call["params"]["api_key"], "hero-key")
        self.assertEqual(call["params"]["service"], "dr")
        self.assertEqual(call["params"]["country"], "52")
        self.assertEqual(call["params"]["maxPrice"], "0.5")
        self.assertEqual(call["params"]["action"], "getNumber")

    def test_smsbower_acquire_uses_separate_key(self):
        http = _Http(["ACCESS_NUMBER:b-9:66819999999"])
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), \
             patch.object(codex_config, "SMSBOWER_API_KEY", "bower-key"), \
             patch.object(codex_config, "SMSBOWER_API_BASE", "https://smsbower.page/stubs/handler_api.php"), \
             patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(codex_config, "SMS_COUNTRY", "52"), \
             patch.object(codex_config, "SMS_MAX_PRICE", ""), \
             patch.object(codex_config, "SMS_AUTO_COUNTRY", False), \
             patch.object(sms_provider, "list_provider_candidates", return_value=[]):
            activation_id, phone = sms_provider.acquire_number(http=http, country="52")

        self.assertEqual(activation_id, "b-9")
        self.assertEqual(phone, "66819999999")
        call = http.calls[0]
        self.assertIn("smsbower.page", call["url"])
        self.assertEqual(call["params"]["api_key"], "bower-key")
        self.assertEqual(call["params"]["service"], "dr")
        self.assertNotIn("maxPrice", call["params"])

    def test_get_balance_parses_access_balance(self):
        http = _Http(["ACCESS_BALANCE:12.34"])
        with patch.object(codex_config, "SMS_PROVIDER", "herosms"), \
             patch.object(codex_config, "HEROSMS_API_KEY", "hero-key"), \
             patch.object(codex_config, "HEROSMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"):
            bal = sms_provider.get_balance(http=http, provider="herosms")
        self.assertEqual(bal, 12.34)
        self.assertEqual(http.calls[0]["params"]["action"], "getBalance")

    def test_get_best_country_prefers_thailand_whitelist(self):
        # 直接用供应商候选结果
        cands = [
            {"country": "52", "provider_id": "1", "price": 0.15, "count": 30, "score": 0.15},
            {"country": "1", "provider_id": "2", "price": 0.01, "count": 999, "score": 1000.01},
        ]
        with patch.object(sms_provider, "list_provider_candidates", return_value=cands):
            best = sms_provider.get_best_country(provider="herosms", service="dr")
        self.assertEqual(best, "52")

    def test_country_whitelist_hard_filter(self):
        """白名单硬限制：候选国家列表不得扩到白名单外。"""
        prices_payload = {
            "52": {"dr": {"1": {"price": 0.12, "count": 40, "provider_id": "1"}}},
            "64": {"dr": {"9": {"price": 0.01, "count": 999, "provider_id": "9"}}},
        }

        def fake_prices_v3(http, provider=None, service=None, country=None):
            return {str(country): prices_payload.get(str(country), {})}

        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), \
             patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(codex_config, "SMS_COUNTRY_WHITELIST", "52"), \
             patch.object(codex_config, "SMS_PREFERRED_COUNTRIES", "52"), \
             patch.object(codex_config, "SMS_USE_TOP_COUNTRIES_WHITELIST", False), \
             patch.object(codex_config, "SMS_ALLOW_OUTSIDE_WHITELIST", False), \
             patch.object(codex_config, "SMS_AUTO_COUNTRY", True), \
             patch.object(codex_config, "SMS_MAX_PRICE", "0.5"), \
             patch.object(codex_config, "SMS_MIN_PRICE", ""), \
             patch.object(codex_config, "SMS_PROVIDER_MIN_STOCK", 1), \
             patch.object(sms_provider, "get_prices_v3", side_effect=fake_prices_v3), \
             patch.object(sms_provider, "get_top_countries", return_value=[
                 {"country": "64", "price": 0.01, "count": 999},
                 {"country": "52", "price": 0.12, "count": 40},
             ]):
            cands = sms_provider.list_provider_candidates(http=_Http([]), provider="smsbower")
        countries = {str(c.get("country")) for c in cands}
        self.assertEqual(countries, {"52"})
        self.assertNotIn("64", countries)

    def test_preferred_acts_as_whitelist_when_whitelist_empty(self):
        with patch.object(codex_config, "SMS_COUNTRY_WHITELIST", ""), \
             patch.object(codex_config, "SMS_PREFERRED_COUNTRIES", "52,6,16"), \
             patch.object(codex_config, "SMS_COUNTRY", "187"):
            self.assertEqual(sms_provider._cfg_country_whitelist(), ["52", "6", "16"])
        with patch.object(codex_config, "SMS_COUNTRY_WHITELIST", "36,52"), \
             patch.object(codex_config, "SMS_PREFERRED_COUNTRIES", "52,6"):
            self.assertEqual(sms_provider._cfg_country_whitelist(), ["36", "52"])

    def test_parse_top_countries_by_service_partner_format(self):
        data = {
            "chile": {
                "3419": {"price": 0.052, "count": 1843},
                "3371": {"price": 0.226, "count": 824},
            },
            "united-states": {
                "2266": {"price": 0.13, "count": 5402},
            },
        }
        rows = sms_provider._parse_top_countries_response(data)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["slug"], "chile")
        self.assertEqual(rows[0]["price"], 0.052)
        self.assertEqual(len(rows[0]["partners"]), 2)
        self.assertEqual(rows[0]["partners"][0]["provider_id"], "3419")
        self.assertEqual(rows[1]["slug"], "united-states")

    def test_list_candidates_uses_top_service_gold_partners(self):
        top_rows = [
            {
                "country": "151",
                "name": "chile",
                "slug": "chile",
                "price": 0.052,
                "count": 1843,
                "partners": [
                    {"provider_id": "3419", "price": 0.052, "count": 1843},
                    {"provider_id": "3371", "price": 0.226, "count": 824},
                ],
            },
            {
                "country": "6",
                "name": "indonesia",
                "slug": "indonesia",
                "price": 0.027,
                "count": 1076,
                "partners": [{"provider_id": "1329", "price": 0.027, "count": 1076}],
            },
        ]
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), \
             patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(codex_config, "SMS_COUNTRY_WHITELIST", ""), \
             patch.object(codex_config, "SMS_USE_TOP_COUNTRIES_WHITELIST", True), \
             patch.object(codex_config, "SMS_ALLOW_OUTSIDE_WHITELIST", False), \
             patch.object(codex_config, "SMS_MAX_PRICE", "0.5"), \
             patch.object(codex_config, "SMS_MIN_PRICE", ""), \
             patch.object(codex_config, "SMS_PROVIDER_MIN_STOCK", 1), \
             patch.object(sms_provider, "resolve_country_whitelist", return_value=(["151", "6"], top_rows)):
            cands = sms_provider.list_provider_candidates(http=_Http([]), provider="smsbower")
        countries = [str(c.get("country")) for c in cands]
        providers = {str(c.get("provider_id")) for c in cands}
        self.assertIn("151", countries)
        self.assertIn("6", countries)
        self.assertIn("3419", providers)
        self.assertIn("1329", providers)
        # 不应再去 getPrices 扩到其它国
        self.assertTrue(all(c.get("from_top") for c in cands))

    def test_get_best_country_skips_excluded_and_falls_back(self):
        cands = [
            {"country": "52", "provider_id": "1", "price": 0.15, "count": 30, "score": 0.15},
            {"country": "66", "provider_id": "2", "price": 0.10, "count": 50, "score": 0.10},
        ]
        with patch.object(sms_provider, "list_provider_candidates", return_value=cands):
            best = sms_provider.get_best_country(provider="smsbower", service="dr", exclude={"52"})
        self.assertEqual(best, "66")

    def test_acquire_rotates_slots_on_no_numbers(self):
        cands = [
            {"country": "52", "provider_id": "A", "price": 0.12, "count": 40, "score": 0.12},
            {"country": "52", "provider_id": "B", "price": 0.20, "count": 30, "score": 0.20},
        ]
        http = _Http(["NO_NUMBERS", "ACCESS_NUMBER:act-b:66810000000"])
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), \
             patch.object(codex_config, "SMSBOWER_API_KEY", "bower-key"), \
             patch.object(codex_config, "SMSBOWER_API_BASE", "https://smsbower.page/stubs/handler_api.php"), \
             patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(codex_config, "SMS_MAX_PRICE", "0.5"), \
             patch.object(codex_config, "SMS_MIN_PRICE", ""), \
             patch.object(codex_config, "SMS_ACQUIRE_MAX_SLOTS", 5), \
             patch.object(sms_provider, "list_provider_candidates", return_value=cands):
            sms_provider.clear_country_no_numbers(provider="smsbower")
            activation_id, phone = sms_provider.acquire_number(http=http, country="52")

        self.assertEqual(activation_id, "act-b")
        self.assertEqual(phone, "66810000000")
        providers = [c["params"].get("providerIds") for c in http.calls if c["params"].get("action") == "getNumber"]
        self.assertEqual(providers, ["A", "B"])
        # A 槽应被冷却
        self.assertIn("52:A", sms_provider._slot_bucket("smsbower"))

    def test_wait_for_sms_code_herosms(self):
        http = _Http(["STATUS_OK:654321"])
        with patch.object(codex_config, "SMS_PROVIDER", "herosms"), \
             patch.object(codex_config, "HEROSMS_API_KEY", "hero-key"), \
             patch.object(codex_config, "HEROSMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php"):
            code = sms_provider.wait_for_sms_code("act-1", http=http, max_wait=1, poll_interval=1)
        self.assertEqual(code, "654321")
        self.assertEqual(http.calls[-1]["params"]["action"], "getStatus")
        self.assertEqual(http.calls[-1]["params"]["id"], "act-1")


if __name__ == "__main__":
    unittest.main()
