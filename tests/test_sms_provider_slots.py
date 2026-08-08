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
        # 单测不落盘、不读生产 delivery_stats.json
        sms_provider._DELIVERY_PERSIST_ENABLED = False
        sms_provider._DELIVERY_LOADED = True
        sms_provider.clear_country_no_numbers(provider="smsbower")
        sms_provider._SLOT_FAILS.pop("smsbower", None)
        sms_provider._ACTIVATION_META.clear()
        sms_provider.clear_delivery_stats(persist=False)

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

    def test_send_reject_soft_cools_slot_not_whole_country(self):
        sms_provider.remember_activation_meta("a1", {
            "country": "12", "provider_id": "3209", "channel": "smsbower",
        })
        sms_provider.mark_activation_send_rejected("a1", reason="send_not_accepted")
        cool = sms_provider._slot_bucket("smsbower")
        self.assertIn("12:3209", cool)
        # 同国其它供应商不应被整国标记
        self.assertNotIn("12:-", cool)
        # 软降权：有分，但不踢候选
        pen, info = sms_provider.soft_cooldown_score_delta("12", "3209", channel="smsbower")
        self.assertGreater(pen, 5.0)
        self.assertEqual(info.get("soft_cool_reason"), "send_not_accepted")
        pen_other, _ = sms_provider.soft_cooldown_score_delta("12", "9999", channel="smsbower")
        self.assertEqual(pen_other, 0.0)

    def test_list_candidates_merges_gold_and_silver_from_v3(self):
        """Top partners=Gold 价高被滤掉时，仍应并入 V3 的 Silver 供应商。"""
        top_rows = [{
            "country": "151",
            "name": "chile",
            "price": 0.226,
            "count": 100,
            "partners": [{"provider_id": "3371", "price": 0.226, "count": 100}],
        }]
        v3 = {
            "151": {
                "dr": {
                    "3371": {"count": 100, "price": 0.226},  # gold, 超 max
                    "3109": {"count": 50, "price": 0.07},    # silver, 可买
                    "3160": {"count": 80, "price": 0.108},   # silver
                }
            }
        }
        http = _Http([json.dumps(v3)])
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), \
             patch.object(codex_config, "SMSBOWER_API_KEY", "k"), \
             patch.object(codex_config, "SMSBOWER_API_BASE", "https://smsbower.page/stubs/handler_api.php"), \
             patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(codex_config, "SMS_MAX_PRICE", "0.15"), \
             patch.object(codex_config, "SMS_MIN_PRICE", "0.05"), \
             patch.object(codex_config, "SMS_PREFERRED_COUNTRIES", "52"), \
             patch.object(codex_config, "SMS_PROVIDER_MIN_STOCK", 1), \
             patch.object(codex_config, "SMS_AUTO_COUNTRY_MIN_STOCK", 0), \
             patch.object(codex_config, "SMS_PRICE_FLOOR_RATIO", 0.1), \
             patch.object(codex_config, "SMS_USE_TOP_COUNTRIES_WHITELIST", True), \
             patch.object(codex_config, "SMS_COUNTRY_WHITELIST", ""), \
             patch.object(codex_config, "SMS_ALLOW_OUTSIDE_WHITELIST", False), \
             patch.object(sms_provider, "resolve_country_whitelist", return_value=(["151"], top_rows)), \
             patch.object(sms_provider, "get_top_countries_by_service", return_value=top_rows):
            rows = sms_provider.list_provider_candidates(
                http, provider="smsbower", service="dr", countries=None, include_low_quality=False
            )
        pids = {r["provider_id"] for r in rows}
        self.assertIn("3109", pids)
        self.assertIn("3160", pids)
        self.assertNotIn("3371", pids)  # 0.226 > 0.15
        # silver 应被标出
        silver = [r for r in rows if r.get("tier") == "silver"]
        self.assertTrue(silver)
        # 同国更便宜的 3109 应排在 3160 前
        self.assertEqual(rows[0]["provider_id"], "3109")

    def test_score_balances_price_over_top_rank(self):
        """排名靠前但更贵，应输给排名稍后但明显更便宜的槽。"""
        top_rows = [
            {"country": "151", "name": "chile", "partners": [{"provider_id": "g1", "price": 0.12, "count": 100}]},
            {"country": "22", "name": "india", "partners": [{"provider_id": "g2", "price": 0.05, "count": 100}]},
        ]
        v3_151 = {"151": {"dr": {"g1": {"count": 100, "price": 0.12}, "s1": {"count": 50, "price": 0.108}}}}
        v3_22 = {"22": {"dr": {"g2": {"count": 100, "price": 0.05}, "s2": {"count": 80, "price": 0.054}}}}
        http = _Http([json.dumps(v3_151), json.dumps(v3_22)])
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), \
             patch.object(codex_config, "SMSBOWER_API_KEY", "k"), \
             patch.object(codex_config, "SMSBOWER_API_BASE", "https://smsbower.page/stubs/handler_api.php"), \
             patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(codex_config, "SMS_MAX_PRICE", "0.15"), \
             patch.object(codex_config, "SMS_MIN_PRICE", "0.05"), \
             patch.object(codex_config, "SMS_PREFERRED_COUNTRIES", ""), \
             patch.object(codex_config, "SMS_PROVIDER_MIN_STOCK", 1), \
             patch.object(codex_config, "SMS_AUTO_COUNTRY_MIN_STOCK", 0), \
             patch.object(codex_config, "SMS_PRICE_FLOOR_RATIO", 0.1), \
             patch.object(codex_config, "SMS_USE_TOP_COUNTRIES_WHITELIST", True), \
             patch.object(codex_config, "SMS_COUNTRY_WHITELIST", ""), \
             patch.object(codex_config, "SMS_ALLOW_OUTSIDE_WHITELIST", False), \
             patch.object(sms_provider, "resolve_country_whitelist", return_value=(["151", "22"], top_rows)), \
             patch.object(sms_provider, "get_top_countries_by_service", return_value=top_rows):
            rows = sms_provider.list_provider_candidates(
                http, provider="smsbower", service="dr", countries=None, include_low_quality=False
            )
        self.assertTrue(rows)
        # 印度 0.05 应压过智利 0.108/0.12
        self.assertEqual(rows[0]["country"], "22")
        self.assertLessEqual(float(rows[0]["price"]), 0.06)

    def test_global_delivery_demotes_dead_slot_and_boosts_success(self):
        """全局投递：死槽降权，到码成功槽加权；跨任务共享。"""
        # 巴西某槽连跪 4 次
        for i in range(4):
            sms_provider.record_delivery_outcome(
                country="73", provider_id="3406", channel="smsbower",
                outcome="timeout", activation_id=f"br-fail-{i}",
            )
        # 墨西哥槽到码 2 次
        for i in range(2):
            sms_provider.record_delivery_outcome(
                country="54", provider_id="3193", channel="smsbower",
                outcome="success", activation_id=f"mx-ok-{i}",
            )

        br_delta, br_info = sms_provider.delivery_score_delta("73", "3406", channel="smsbower")
        mx_delta, mx_info = sms_provider.delivery_score_delta("54", "3193", channel="smsbower")
        self.assertGreater(br_delta, 8.0)
        self.assertLess(mx_delta, 0.0)
        self.assertEqual(br_info["slot_ok"], 0)
        self.assertEqual(br_info["slot_n"], 4)
        self.assertEqual(mx_info["slot_ok"], 2)

        # 整国 0 到码：未试档也沉底；但同国另一已试死档是 slot dead
        br_new_delta, br_new_info = sms_provider.delivery_score_delta(
            "73", "9999", channel="smsbower"
        )
        self.assertEqual(br_new_info.get("country_mode"), "dead_country_zero_ok")
        self.assertGreaterEqual(br_new_delta, 45.0)
        br_slot, br_slot_info = sms_provider.delivery_score_delta(
            "73", "3406", channel="smsbower"
        )
        self.assertEqual(br_slot_info.get("slot_mode"), "dead_slot_zero_ok")
        self.assertGreaterEqual(br_slot, 45.0)

        top_rows = [
            {"country": "73", "name": "brazil", "partners": [{"provider_id": "3406", "price": 0.054, "count": 100}]},
            {"country": "54", "name": "mexico", "partners": [{"provider_id": "3193", "price": 0.08, "count": 100}]},
        ]
        # 仅死槽 3406 vs 墨，不含未试巴西商
        v3_73 = {"73": {"dr": {"3406": {"count": 100, "price": 0.054}}}}
        v3_54 = {"54": {"dr": {"3193": {"count": 100, "price": 0.08}}}}
        http = _Http([json.dumps(v3_73), json.dumps(v3_54)])
        with patch.object(codex_config, "SMS_PROVIDER", "smsbower"), \
             patch.object(codex_config, "SMSBOWER_API_KEY", "k"), \
             patch.object(codex_config, "SMSBOWER_API_BASE", "https://smsbower.page/stubs/handler_api.php"), \
             patch.object(codex_config, "SMS_SERVICE", "dr"), \
             patch.object(codex_config, "SMS_MAX_PRICE", "0.15"), \
             patch.object(codex_config, "SMS_MIN_PRICE", "0.05"), \
             patch.object(codex_config, "SMS_PREFERRED_COUNTRIES", ""), \
             patch.object(codex_config, "SMS_PROVIDER_MIN_STOCK", 1), \
             patch.object(codex_config, "SMS_AUTO_COUNTRY_MIN_STOCK", 0), \
             patch.object(codex_config, "SMS_PRICE_FLOOR_RATIO", 0.1), \
             patch.object(codex_config, "SMS_USE_TOP_COUNTRIES_WHITELIST", True), \
             patch.object(codex_config, "SMS_COUNTRY_WHITELIST", ""), \
             patch.object(codex_config, "SMS_ALLOW_OUTSIDE_WHITELIST", False), \
             patch.object(sms_provider, "resolve_country_whitelist", return_value=(["73", "54"], top_rows)), \
             patch.object(sms_provider, "get_top_countries_by_service", return_value=top_rows):
            rows = sms_provider.list_provider_candidates(
                http, provider="smsbower", service="dr", countries=None, include_low_quality=False
            )
        self.assertTrue(rows)
        # 巴西 3406 连跪大罚后，墨应排前
        self.assertEqual(rows[0]["country"], "54")
        self.assertEqual(rows[0]["provider_id"], "3193")
        br_rows = [r for r in rows if r["country"] == "73" and r["provider_id"] == "3406"]
        self.assertTrue(br_rows)
        self.assertGreater(float(br_rows[0].get("delivery_adj") or 0), 5.0)

    def test_country_contagion_needs_multiple_dead_slots(self):
        """整国传染：需多个不同死槽；有好槽时不误伤同国未试商。"""
        # 英 2442 连跪 3 次（< dead min 4）→ 未试 3248 不重罚
        for i in range(3):
            sms_provider.record_delivery_outcome(
                country="16", provider_id="2442", channel="smsbower",
                outcome="timeout", activation_id=f"uk-bad-{i}",
            )
        d_untried, info_u = sms_provider.delivery_score_delta("16", "3248", channel="smsbower")
        self.assertLess(d_untried, 4.0)

        # 3248 验证成功后，同国未试商不因死国沉底
        for i in range(3):
            sms_provider.record_delivery_outcome(
                country="16", provider_id="3248", channel="smsbower",
                outcome="success", activation_id=f"uk-good-{i}",
            )
        d2, info2 = sms_provider.delivery_score_delta("16", "9999", channel="smsbower")
        self.assertNotEqual(info2.get("country_mode"), "dead_country_zero_ok")
        self.assertLess(d2, 5.0)

        # 巴西 0 到码且 n>=4 → 死国沉底
        sms_provider.clear_delivery_stats(persist=False)
        for i in range(5):
            sms_provider.record_delivery_outcome(
                country="73", provider_id="2906", channel="smsbower",
                outcome="timeout", activation_id=f"br-dead-{i}",
            )
        d_br, info_br = sms_provider.delivery_score_delta("73", "8888", channel="smsbower")
        self.assertEqual(info_br.get("country_mode"), "dead_country_zero_ok")
        self.assertGreaterEqual(d_br, 45.0)

    def test_empty_provider_id_not_phantom_slot(self):
        """无 provider_id 的成功只记国家级，不产生 187:- 幽灵槽。"""
        sms_provider.record_delivery_outcome(
            country="187", provider_id="", channel="smsbower",
            outcome="success", activation_id="bare-us-1",
        )
        sms_provider.record_delivery_outcome(
            country="187", provider_id="2266", channel="smsbower",
            outcome="timeout", activation_id="us-2266-1",
        )
        snap = sms_provider.get_delivery_stats_snapshot(provider="smsbower")
        slots = {r["slot"] for r in snap}
        self.assertNotIn("187:-", slots)
        self.assertIn("187:2266", slots)
        # bare 成功不应变成 187:2266 的 slot_ok
        d, info = sms_provider.delivery_score_delta("187", "2266", channel="smsbower")
        self.assertEqual(info["slot_n"], 1)
        self.assertEqual(info["slot_ok"], 0)
        self.assertGreaterEqual(info["country_ok"], 1)

    def test_shard_provider_candidates_rotates_near_ties(self):
        rows = [
            {"country": "16", "provider_id": "a", "score": 1.0, "price": 0.05},
            {"country": "187", "provider_id": "b", "score": 1.2, "price": 0.13},
            {"country": "54", "provider_id": "c", "score": 1.5, "price": 0.08},
            {"country": "73", "provider_id": "d", "score": 20.0, "price": 0.05},
        ]
        s0 = sms_provider.shard_provider_candidates(rows, salt=0)
        s1 = sms_provider.shard_provider_candidates(rows, salt=1)
        s2 = sms_provider.shard_provider_candidates(rows, salt=2)
        # 第一梯队应被旋转到不同首位
        heads = {s0[0]["provider_id"], s1[0]["provider_id"], s2[0]["provider_id"]}
        self.assertGreaterEqual(len(heads), 2)
        # 明显更差的 d 仍在末尾区
        for s in (s0, s1, s2):
            self.assertEqual(s[-1]["provider_id"], "d")

    def test_activation_delivery_dedup_timeout_then_reject(self):
        """同一 activation 超时后再 send_reject 不双计。"""
        sms_provider.remember_activation_meta("act-x", {
            "country": "73", "provider_id": "3406", "channel": "smsbower",
        })
        self.assertTrue(sms_provider.record_activation_delivery("act-x", "timeout"))
        self.assertFalse(sms_provider.record_activation_delivery("act-x", "send_reject"))
        snap = sms_provider.get_delivery_stats_snapshot(provider="smsbower")
        br = [r for r in snap if r["slot"] == "73:3406"]
        self.assertEqual(len(br), 1)
        self.assertEqual(br[0]["n"], 1)
        self.assertEqual(br[0]["ok"], 0)

        # 若后来又记 success（极少见），应升级
        self.assertTrue(sms_provider.record_activation_delivery("act-x", "success"))
        snap2 = sms_provider.get_delivery_stats_snapshot(provider="smsbower")
        br2 = [r for r in snap2 if r["slot"] == "73:3406"]
        self.assertEqual(br2[0]["n"], 1)
        self.assertEqual(br2[0]["ok"], 1)

    def test_consec_fail_heavy_then_success_recovers(self):
        """墨连跪 3 次扣大分；成功一次后连跪清零，可再被低价选中。"""
        for i in range(3):
            sms_provider.record_delivery_outcome(
                country="54", provider_id="3193", channel="smsbower",
                outcome="timeout", activation_id=f"mx-tmo-{i}",
            )
        d_bad, info_bad = sms_provider.delivery_score_delta("54", "3193", channel="smsbower")
        self.assertEqual(info_bad.get("slot_consec_fail"), 3)
        self.assertGreaterEqual(float(info_bad.get("consec_penalty") or 0), 14.0)
        self.assertGreaterEqual(d_bad, 14.0)

        # 抖动结束：成功一发
        sms_provider.record_delivery_outcome(
            country="54", provider_id="3193", channel="smsbower",
            outcome="success", activation_id="mx-ok-recover",
        )
        d_ok, info_ok = sms_provider.delivery_score_delta("54", "3193", channel="smsbower")
        self.assertEqual(info_ok.get("slot_consec_fail"), 0)
        self.assertEqual(float(info_ok.get("consec_penalty") or 0), 0.0)
        # 恢复后惩罚应远小于连跪期（允许轻微 rate 分）
        self.assertLess(d_ok, 6.0)
        self.assertLess(d_ok, d_bad - 8.0)

    def test_timeout_soft_cool_weaker_than_send_reject(self):
        """超时软降权窗口更短、基础分更低；真拒绝更重——但仍都留在候选里。"""
        import time as _t
        sms_provider.mark_slot_cooldown("54", "3193", channel="smsbower", reason="timeout")
        ent = sms_provider._slot_bucket("smsbower").get("54:3193") or {}
        self.assertLess(float(ent.get("sec") or 0), 200)
        pen_tmo, _ = sms_provider.soft_cooldown_score_delta("54", "3193", channel="smsbower")
        self.assertGreater(pen_tmo, 0.0)
        self.assertLessEqual(pen_tmo, 6.0 + 0.1)

        sms_provider.clear_country_no_numbers(provider="smsbower")
        sms_provider.mark_slot_cooldown("54", "3193", channel="smsbower", reason="send_not_accepted")
        ent2 = sms_provider._slot_bucket("smsbower").get("54:3193") or {}
        self.assertGreater(float(ent2.get("sec") or 0), 1000)
        pen_rej, _ = sms_provider.soft_cooldown_score_delta("54", "3193", channel="smsbower")
        self.assertGreater(pen_rej, pen_tmo)

    def test_soft_cool_does_not_exclude_from_candidates(self):
        """软降权后槽仍出现在候选列表（纯权重调度）。"""
        sms_provider.mark_slot_cooldown("52", "200", channel="smsbower", reason="no_numbers")
        v3 = {
            "52": {
                "dr": {
                    # 价差不大时，no_numbers 软降权应让 200 排到 300 后
                    "200": {"count": 50, "price": 0.18, "provider_id": 200},
                    "300": {"count": 80, "price": 0.20, "provider_id": 300},
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
        cooled = next(r for r in rows if r["provider_id"] == "200")
        self.assertGreater(float(cooled.get("soft_cool_adj") or 0), 0.0)
        # 被软降权后应排在更贵但干净的 300 后面
        self.assertEqual(rows[0]["provider_id"], "300")

    def test_price_weight_prefers_cheaper_when_delivery_equal(self):
        """价权 300：投递分相同时更便宜的槽分更低；美墨价差约 15 分。"""
        self.assertEqual(sms_provider._SCORE_PRICE_WEIGHT, 300.0)
        # 无投递样本时，仅比价
        d0, _ = sms_provider.delivery_score_delta("54", "1", channel="smsbower")
        d1, _ = sms_provider.delivery_score_delta("187", "1", channel="smsbower")
        self.assertEqual(d0, 0.0)
        self.assertEqual(d1, 0.0)
        score_mx = 0.08 * sms_provider._SCORE_PRICE_WEIGHT
        score_us = 0.13 * sms_provider._SCORE_PRICE_WEIGHT
        self.assertLess(score_mx, score_us)
        self.assertAlmostEqual(score_us - score_mx, 0.05 * 300.0, places=3)
        self.assertEqual(sms_provider._FALLBACK_COUNTRY_PENALTY.get("187"), 6.0)

    def test_rate_hat_adapts_when_success_moves(self):
        """国家从全灭到有成功后，不再是 dead，rate_hat 上升。"""
        for i in range(5):
            sms_provider.record_delivery_outcome(
                country="54", provider_id="3193", channel="smsbower",
                outcome="timeout", activation_id=f"mx-fail-{i}",
            )
        d0, info0 = sms_provider.delivery_score_delta("54", "3193", channel="smsbower")
        self.assertEqual(info0.get("slot_mode"), "dead_slot_zero_ok")
        self.assertEqual(sms_provider._slot_tier(info0), "dead")
        for i in range(5):
            sms_provider.record_delivery_outcome(
                country="54", provider_id="3193", channel="smsbower",
                outcome="success", activation_id=f"mx-ok-{i}",
            )
        d1, info1 = sms_provider.delivery_score_delta("54", "3193", channel="smsbower")
        self.assertNotEqual(info1.get("slot_mode"), "dead_slot_zero_ok")
        tier = sms_provider._slot_tier(info1)
        self.assertIn(tier, ("hot", "warm"))

    def test_same_country_different_slots_split_tiers(self):
        """同国不同档位可分属 hot/dead：英好档与坏档分开。"""
        for i in range(5):
            sms_provider.record_delivery_outcome(
                country="16", provider_id="2442", channel="smsbower",
                outcome="timeout", activation_id=f"uk-bad-{i}",
            )
        for i in range(4):
            sms_provider.record_delivery_outcome(
                country="16", provider_id="3248", channel="smsbower",
                outcome="success", activation_id=f"uk-good-{i}",
            )
        d_bad, info_bad = sms_provider.delivery_score_delta("16", "2442", channel="smsbower")
        d_good, info_good = sms_provider.delivery_score_delta("16", "3248", channel="smsbower")
        self.assertEqual(info_bad.get("slot_mode"), "dead_slot_zero_ok")
        self.assertGreaterEqual(d_bad, 45.0)
        self.assertLess(d_good, d_bad - 20)
        tier_bad = sms_provider._slot_tier(info_bad)
        tier_good = sms_provider._slot_tier(info_good)
        self.assertEqual(tier_bad, "dead")
        self.assertEqual(tier_good, "hot")
        # 未试第三档：同国有好档 → explore，不连坐 dead
        d_new, info_new = sms_provider.delivery_score_delta("16", "9999", channel="smsbower")
        self.assertEqual(info_new.get("country_mode"), "sibling_good_explore")
        self.assertEqual(sms_provider._slot_tier(info_new), "explore")
        self.assertLess(d_new, 20.0)

    def test_adaptive_hot_before_dead_and_rate_moves_tiers(self):
        """高到码率档变 hot；0 成功档/国沉底。"""
        for i in range(6):
            sms_provider.record_delivery_outcome(
                country="73", provider_id="2906", channel="smsbower",
                outcome="timeout", activation_id=f"br-dead-{i}",
            )
        for i in range(4):
            sms_provider.record_delivery_outcome(
                country="22", provider_id="2266", channel="smsbower",
                outcome="success", activation_id=f"in-ok-{i}",
            )
        for i in range(4):
            sms_provider.record_delivery_outcome(
                country="187", provider_id="2266", channel="smsbower",
                outcome="success", activation_id=f"us-ok-{i}",
            )
        d_br, info_br = sms_provider.delivery_score_delta("73", "2906", channel="smsbower")
        self.assertEqual(info_br.get("slot_mode"), "dead_slot_zero_ok")

        rows = [
            {
                "country": "73", "provider_id": "2906", "price": 0.05, "count": 100,
                "score": 99, "slot_key": "73:2906",
                "delivery_info": {
                    "slot_n": 6, "slot_ok": 0, "slot_mode": "dead_slot_zero_ok",
                    "country_n": 6, "country_ok": 0,
                },
            },
            {
                "country": "22", "provider_id": "2266", "price": 0.054, "count": 100,
                "score": 5, "slot_key": "22:2266",
                "delivery_info": {"slot_n": 4, "slot_ok": 4, "country_n": 4, "country_ok": 4},
            },
            {
                "country": "187", "provider_id": "2266", "price": 0.13, "count": 100,
                "score": 8, "slot_key": "187:2266",
                "delivery_info": {"slot_n": 4, "slot_ok": 4, "country_n": 4, "country_ok": 4},
            },
            {
                "country": "33", "provider_id": "1", "price": 0.05, "count": 50,
                "score": 12, "slot_key": "33:1",
                "delivery_info": {"slot_n": 0, "slot_ok": 0, "country_n": 0, "country_ok": 0},
            },
        ]
        # 样本质量标签仍可区分
        self.assertEqual(sms_provider._slot_tier(rows[1]["delivery_info"]), "hot")
        self.assertEqual(sms_provider._slot_tier(rows[0]["delivery_info"]), "dead")
        ordered = sms_provider.order_candidates_for_acquire(rows, attempt_index=4, explore_prob=0.0)
        buckets = [o.get("bucket") for o in ordered]
        self.assertNotIn("junk", buckets)
        self.assertTrue(all(o.get("country") != "73" for o in ordered))

    def test_inflight_pushes_slot_back(self):
        """占槽后同槽对其它排序沉后。"""
        rows = [
            {"country": "22", "provider_id": "a", "price": 0.05, "count": 10,
             "score": 10.0, "slot_key": "22:a", "rate_hat": 0.4,
             "delivery_info": {"slot_n": 4, "slot_ok": 2, "country_n": 4, "country_ok": 2}},
            {"country": "22", "provider_id": "b", "price": 0.06, "count": 10,
             "score": 12.0, "slot_key": "22:b", "rate_hat": 0.4,
             "delivery_info": {"slot_n": 4, "slot_ok": 2, "country_n": 4, "country_ok": 2}},
        ]
        sms_provider.claim_slot_inflight("22:a")
        try:
            ordered = sms_provider.order_candidates_for_acquire(rows, attempt_index=1, explore_prob=0.0)
            self.assertEqual(ordered[0]["provider_id"], "b")
        finally:
            sms_provider.release_slot_inflight("22:a")

    def test_budget_phase_blocks_fallback_until_attempt_4(self):
        """前 3 次不开放高价兜底；第 4 次起开放。分类来自价格分位+到码率。"""
        rows = [
            {
                "country": "22", "provider_id": "cheap", "price": 0.05, "count": 50,
                "score": 5, "slot_key": "22:cheap", "rate_hat": 0.5,
                "delivery_info": {"slot_n": 6, "slot_ok": 3, "country_n": 6, "country_ok": 3},
            },
            {
                "country": "187", "provider_id": "us", "price": 0.13, "count": 50,
                "score": 20, "slot_key": "187:us", "rate_hat": 0.7,
                "delivery_info": {"slot_n": 6, "slot_ok": 4, "country_n": 6, "country_ok": 4},
            },
            {
                "country": "73", "provider_id": "junk", "price": 0.04, "count": 50,
                "score": 80, "slot_key": "73:junk", "rate_hat": 0.0,
                "delivery_info": {
                    "slot_n": 6, "slot_ok": 0, "slot_mode": "dead_slot_zero_ok",
                    "country_n": 6, "country_ok": 0,
                },
            },
        ]
        o1 = sms_provider.order_candidates_for_acquire(rows, attempt_index=1, explore_prob=0.0)
        self.assertTrue(o1)
        self.assertNotEqual(o1[0].get("bucket"), "fallback")
        self.assertNotEqual(o1[0].get("bucket"), "junk")
        # 前 3 次列表中不应出现兜底（除非平价为空）
        self.assertTrue(all(r.get("bucket") != "fallback" for r in o1) or o1[0]["bucket"] == "value")
        o4 = sms_provider.order_candidates_for_acquire(rows, attempt_index=4, explore_prob=0.0)
        buckets4 = [r.get("bucket") for r in o4]
        self.assertIn("fallback", buckets4)
        # 常规路径（不探索）不带垃圾
        self.assertNotIn("junk", buckets4)
        self.assertTrue(all(r.get("country") != "73" for r in o4))
        # 探索开启且强制 junk 探索：可出现 1 个垃圾
        o_ex = sms_provider.order_candidates_for_acquire(
            rows, attempt_index=4, explore_prob=1.0, junk_in_explore_prob=1.0,
        )
        junk_rows = [r for r in o_ex if r.get("bucket") == "junk"]
        self.assertEqual(len(junk_rows), 1)
        self.assertTrue(junk_rows[0].get("explore_junk"))

    def test_delivery_window_is_count_based_not_time(self):
        """滑动窗口按总次数：超出 N 后最老事件被挤掉（与空闲多久无关）。"""
        old = sms_provider._DELIVERY_MAX_EVENTS
        try:
            sms_provider._DELIVERY_MAX_EVENTS = 5
            for i in range(5):
                sms_provider.record_delivery_outcome(
                    country="73", provider_id="1", channel="smsbower",
                    outcome="timeout", activation_id=f"old-{i}",
                )
            # 再记 3 次墨西哥成功 → 窗口只剩最近 5 条，巴西旧失败应被挤掉一部分
            for i in range(3):
                sms_provider.record_delivery_outcome(
                    country="54", provider_id="3193", channel="smsbower",
                    outcome="success", activation_id=f"mx-{i}",
                )
            evs = sms_provider._DELIVERY_EVENTS.get("smsbower") or []
            self.assertEqual(len(evs), 5)
            # 最近 3 条应是墨
            self.assertEqual(sum(1 for e in evs if e.get("country") == "54"), 3)
            self.assertEqual(sum(1 for e in evs if e.get("country") == "73"), 2)
        finally:
            sms_provider._DELIVERY_MAX_EVENTS = old

    def test_delivery_stats_persist_roundtrip(self):
        """落盘后可再次加载。"""
        import tempfile
        from pathlib import Path
        td = tempfile.TemporaryDirectory()
        try:
            root = Path(td.name)
            path = root / "delivery_stats.json"
            with patch.object(sms_provider, "_SMS_STATE_DIR", root), \
                 patch.object(sms_provider, "_DELIVERY_STATE_PATH", path), \
                 patch.object(sms_provider, "_DELIVERY_PERSIST_ENABLED", True), \
                 patch.object(sms_provider, "_DELIVERY_LOADED", True):
                sms_provider.clear_delivery_stats(persist=False)
                sms_provider.record_delivery_outcome(
                    country="54", provider_id="3193", channel="smsbower",
                    outcome="success", activation_id="persist-1",
                )
                self.assertTrue(path.is_file())
                # 模拟重启
                sms_provider._DELIVERY_EVENTS.clear()
                sms_provider._DELIVERY_BY_AID.clear()
                sms_provider._DELIVERY_LOADED = False
                sms_provider.ensure_delivery_stats_loaded()
                d, info = sms_provider.delivery_score_delta("54", "3193", channel="smsbower")
                self.assertEqual(info.get("slot_ok"), 1)
                self.assertEqual(info.get("slot_n"), 1)
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
