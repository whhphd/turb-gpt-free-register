# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import sub2api_pool_monitor as mon


class ClassifyPoolAccountTests(unittest.TestCase):
    def test_ok_active_with_refresh_token_flag(self):
        # has_refresh_token:true 不得因字段名含 refresh_token 误判
        acc = {
            "status": "active",
            "error_message": "",
            "credentials_status": {"has_refresh_token": True, "has_access_token": True},
        }
        self.assertEqual(mon.classify_pool_account(acc), "ok")

    def test_rt_bad_when_no_refresh_token(self):
        acc = {
            "status": "error",
            "error_message": "something",
            "credentials_status": {"has_refresh_token": False},
        }
        self.assertEqual(mon.classify_pool_account(acc), "rt_bad")

    def test_rt_bad_from_invalid_grant(self):
        acc = {
            "status": "error",
            "error_message": "oauth token refresh failed: invalid_grant",
            "credentials_status": {"has_refresh_token": True},
        }
        self.assertEqual(mon.classify_pool_account(acc), "rt_bad")

    def test_dead_hint_banned(self):
        acc = {
            "status": "error",
            "error_message": "account_deactivated by openai",
        }
        self.assertEqual(mon.classify_pool_account(acc), "dead_hint")

    def test_unauthorized_status(self):
        acc = {"status": "unauthorized", "error_message": ""}
        self.assertEqual(mon.classify_pool_account(acc), "rt_bad")

    def test_unknown_active_with_unrelated_error(self):
        acc = {
            "status": "active",
            "error_message": "rate limited temporarily",
            "credentials_status": {"has_refresh_token": True},
        }
        self.assertEqual(mon.classify_pool_account(acc), "unknown")


class PoolEmailExtractTests(unittest.TestCase):
    def test_email_from_extra_when_name_is_batch_label(self):
        acc = {
            "id": 1,
            "name": "26.8.5.10.38 #19",
            "extra": {"email": "signals.swill_5u@icloud.com", "source": "codex-register"},
            "credentials": {},
        }
        self.assertEqual(mon._pool_email(acc), "signals.swill_5u@icloud.com")

    def test_email_from_credentials_outlook(self):
        acc = {
            "name": "batch #1",
            "credentials": {"outlook_email": "a.b@icloud.com"},
        }
        self.assertEqual(mon._pool_email(acc), "a.b@icloud.com")


class ScanMatchTests(unittest.TestCase):
    def test_foreign_ignored_without_local_match(self):
        pool = [
            {
                "id": 1,
                "name": "foreign@other.com",
                "status": "error",
                "error_message": "invalid_grant",
                "extra": {"import_source": "manual-other"},
            },
            {
                "id": 2,
                "name": "ours@local.com",
                "status": "error",
                "error_message": "invalid_grant",
                "extra": {"import_source": "codex_pool_push"},
            },
        ]
        local_idx = {
            "ours@local.com": {
                "email": "ours@local.com",
                "account_id": 9,
                "codex_status": "success",
                "filenames": ["codex-ours.json"],
                "exported": True,
                "sources": ["codex"],
            }
        }
        mat_ok = {
            "repairable": True, "login_mode": "password_totp",
            "reason": "可用密码+2FA", "has_password": True, "has_totp": True,
        }
        with patch.object(mon, "fetch_pool_accounts", return_value=pool), \
             patch.object(mon, "build_local_index", return_value=local_idx), \
             patch.object(mon, "assess_login_material", return_value=mat_ok):
            result = mon.scan_pool(include_ok=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["ignored"], 1)
        self.assertEqual(result["summary"]["ours"], 1)
        self.assertEqual(result["summary"]["rt_bad"], 1)
        actions = {it["email"]: it["action"] for it in result["items"]}
        self.assertEqual(actions["foreign@other.com"], "ignore")
        self.assertEqual(actions["ours@local.com"], "reauth_repush")

    def test_codex_register_without_local_is_foreign(self):
        """号池 source=codex-register 但本机无记录 → 外渠道排除（无法补跑）。"""
        pool = [{
            "id": 13232,
            "name": "26.8.5.10.38 #19",
            "status": "inactive",
            "error_message": "Token revoked (401): Encountered invalidated oauth token",
            "extra": {
                "email": "signals.swill_5u@icloud.com",
                "source": "codex-register",
            },
            "credentials": {},
            "schedulable": False,
        }]
        with patch.object(mon, "fetch_pool_accounts", return_value=pool), \
             patch.object(mon, "build_local_index", return_value={}):
            result = mon.scan_pool()
        self.assertEqual(result["summary"]["ours"], 0)
        self.assertEqual(result["summary"]["ignored"], 1)
        self.assertEqual(result["items"][0]["action"], "ignore")
        # 邮箱仍应从 extra 抽出（展示用）
        self.assertEqual(result["items"][0]["email"], "signals.swill_5u@icloud.com")

    def test_local_match_with_batch_name_is_ours(self):
        """本机有邮箱记录时，即使号池 name 是批次名，也应算本站。"""
        pool = [{
            "id": 1,
            "name": "26.8.5.10.38 #19",
            "status": "error",
            "error_message": "invalid_grant",
            "extra": {"email": "mine@icloud.com", "source": "codex-register"},
        }]
        local_idx = {
            "mine@icloud.com": {
                "email": "mine@icloud.com",
                "account_id": 1,
                "codex_status": "success",
                "filenames": [],
                "exported": False,
                "sources": ["account"],
            }
        }
        mat_ok = {
            "repairable": True, "login_mode": "password_totp",
            "reason": "可用密码+2FA",
        }
        with patch.object(mon, "fetch_pool_accounts", return_value=pool), \
             patch.object(mon, "build_local_index", return_value=local_idx), \
             patch.object(mon, "assess_login_material", return_value=mat_ok):
            result = mon.scan_pool()
        self.assertEqual(result["summary"]["ours"], 1)
        self.assertEqual(result["summary"]["rt_bad"], 1)
        self.assertEqual(result["items"][0]["action"], "reauth_repush")
        self.assertEqual(result["items"][0]["email"], "mine@icloud.com")

    def test_outlook_pool_local_match(self):
        pool = [{
            "id": 9,
            "name": "only-in-outlook@icloud.com",
            "status": "error",
            "error_message": "invalid_grant",
            "extra": {},
        }]
        local_idx = {
            "only-in-outlook@icloud.com": {
                "email": "only-in-outlook@icloud.com",
                "account_id": None,
                "codex_status": "",
                "filenames": [],
                "exported": False,
                "sources": ["outlook_pool"],
            }
        }
        mat_ok = {
            "repairable": True, "login_mode": "email_otp",
            "reason": "outlook", "mail_source": "outlook",
        }
        with patch.object(mon, "fetch_pool_accounts", return_value=pool), \
             patch.object(mon, "build_local_index", return_value=local_idx), \
             patch.object(mon, "assess_login_material", return_value=mat_ok):
            result = mon.scan_pool()
        self.assertEqual(result["summary"]["rt_bad"], 1)
        self.assertEqual(result["items"][0]["action"], "reauth_repush")

    def test_rt_bad_without_material_is_need_material(self):
        pool = [{
            "id": 1,
            "name": "x@y.com",
            "status": "error",
            "error_message": "invalid_grant",
            "extra": {"import_source": "codex_pool_push"},
        }]
        mat_bad = {
            "repairable": False, "login_mode": "none",
            "reason": "本地无登录素材",
        }
        with patch.object(mon, "fetch_pool_accounts", return_value=pool), \
             patch.object(mon, "build_local_index", return_value={}), \
             patch.object(mon, "assess_login_material", return_value=mat_bad):
            result = mon.scan_pool()
        self.assertEqual(result["summary"]["need_material"], 1)
        self.assertEqual(result["items"][0]["action"], "need_material")

    def test_local_deactivated_skip(self):
        pool = [{
            "id": 3,
            "name": "dead@local.com",
            "status": "error",
            "error_message": "invalid_grant",
            "extra": {},
        }]
        local_idx = {
            "dead@local.com": {
                "email": "dead@local.com",
                "account_id": 1,
                "codex_status": "deactivated",
                "filenames": [],
                "exported": False,
                "sources": ["account"],
            }
        }
        with patch.object(mon, "fetch_pool_accounts", return_value=pool), \
             patch.object(mon, "build_local_index", return_value=local_idx):
            result = mon.scan_pool()
        self.assertEqual(result["summary"]["local_dead_skip"], 1)
        self.assertEqual(result["items"][0]["action"], "skip_dead")

    def test_import_source_marks_ours_without_local(self):
        pool = [{
            "id": 4,
            "name": "pushed@x.com",
            "status": "active",
            "error_message": "",
            "extra": {"import_source": "turb-gpt"},
            "credentials_status": {"has_refresh_token": True},
        }]
        with patch.object(mon, "fetch_pool_accounts", return_value=pool), \
             patch.object(mon, "build_local_index", return_value={}):
            result = mon.scan_pool(include_ok=True)
        self.assertEqual(result["summary"]["ours"], 1)
        self.assertEqual(result["summary"]["ok"], 1)
        self.assertEqual(result["items"][0]["action"], "none")


class MarkDeadTests(unittest.TestCase):
    def test_mark_local_dead_also_deletes_pool(self):
        with patch.object(mon.db, "update_account_codex_status") as upd, \
             patch.object(mon, "find_pool_ids_for_email", return_value=[101, 102]), \
             patch.object(mon, "delete_pool_account", side_effect=lambda pid: {"ok": True, "pool_id": pid, "deleted": True}) as dele:
            r = mon.mark_local_dead("a@b.com", "test", pool_id=101)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "dead")
        self.assertEqual(r["pool_deleted"], [101, 102])
        upd.assert_called_once_with("a@b.com", "deactivated", "test")
        self.assertEqual(dele.call_count, 2)

    def test_mark_local_dead_skip_pool_delete(self):
        with patch.object(mon.db, "update_account_codex_status") as upd, \
             patch.object(mon, "delete_pool_account") as dele:
            r = mon.mark_local_dead("a@b.com", "test", delete_pool=False)
        self.assertTrue(r["ok"])
        dele.assert_not_called()
        upd.assert_called_once()


class LoginMaterialTests(unittest.TestCase):
    def test_password_totp_repairable(self):
        with patch.object(mon.db, "get_account_by_email", return_value={
            "id": 1, "email": "a@b.com", "password": "p", "totp_secret": "ABCDEFGH",
            "email_source": "password_totp",
        }), patch.object(mon.db, "get_generic_api_email_by_email", return_value=None), \
             patch.object(mon.db, "get_outlook_by_email", return_value=None):
            m = mon.assess_login_material("a@b.com")
        self.assertTrue(m["repairable"])
        self.assertEqual(m["login_mode"], "password_totp")

    def test_generic_api_repairable(self):
        with patch.object(mon.db, "get_account_by_email", return_value=None), \
             patch.object(mon.db, "get_generic_api_email_by_email", return_value={"email": "a@b.com", "code_url": "http://x"}), \
             patch.object(mon.db, "get_outlook_by_email", return_value=None):
            m = mon.assess_login_material("a@b.com")
        self.assertTrue(m["repairable"])
        self.assertEqual(m["login_mode"], "email_otp")
        self.assertEqual(m["mail_source"], "generic_api")

    def test_no_material_not_repairable(self):
        with patch.object(mon.db, "get_account_by_email", return_value=None), \
             patch.object(mon.db, "get_generic_api_email_by_email", return_value=None), \
             patch.object(mon.db, "get_outlook_by_email", return_value=None):
            m = mon.assess_login_material("ghost@icloud.com")
        self.assertFalse(m["repairable"])
        self.assertEqual(m["login_mode"], "none")


class ParallelRepairTests(unittest.TestCase):
    def test_repair_many_parallel_default_workers(self):
        targets = [{"email": f"u{i}@x.com", "pool_id": i} for i in range(5)]

        def fake_repair(email, pool_id=None, do_reauth=True):
            return {"ok": True, "email": email, "pool_id": pool_id, "status": "repaired"}

        events = []
        with patch.object(mon, "repair_one", side_effect=fake_repair):
            r = mon.repair_many(targets, do_reauth=True, max_workers=10, progress_cb=events.append)
        self.assertEqual(r["success"], 5)
        self.assertEqual(r["max_workers"], 10)
        self.assertEqual(len(r["results"]), 5)
        # 完成事件里应带上并行 workers 信息
        self.assertTrue(any(e.get("max_workers") == 10 for e in events))


class SchedulableRestoreTests(unittest.TestCase):
    def test_update_pool_credentials_enables_schedulable(self):
        calls = []

        class FakeResp:
            def __init__(self, code=200, body=None):
                self.status_code = code
                self._body = body if body is not None else {"code": 0}

            def json(self):
                return self._body

        def fake_request(method, url, **kwargs):
            calls.append((method.upper(), url, kwargs.get("json")))
            return FakeResp()

        with patch.object(mon, "_api_base", return_value="https://sub.example"), \
             patch.object(mon, "_auth_headers", return_value={"x-api-key": "k"}), \
             patch.object(mon.requests, "put", side_effect=lambda url, **kw: fake_request("PUT", url, **kw)), \
             patch.object(mon.requests, "post", side_effect=lambda url, **kw: fake_request("POST", url, **kw)), \
             patch.object(mon.requests, "delete", side_effect=lambda url, **kw: fake_request("DELETE", url, **kw)):
            mon._update_pool_credentials(123, {
                "credentials": {"refresh_token": "rt", "access_token": "at"},
                "expires_at": 1893456000,
            })

        methods_urls = [(m, u) for m, u, _ in calls]
        self.assertTrue(any(m == "PUT" and u.endswith("/api/v1/admin/accounts/123") for m, u in methods_urls))
        self.assertTrue(any(
            m == "POST" and u.endswith("/api/v1/admin/accounts/123/schedulable")
            for m, u in methods_urls
        ))
        # PUT body 含 schedulable=true
        put_bodies = [body for m, u, body in calls if m == "PUT" and u.endswith("/accounts/123")]
        self.assertTrue(any(isinstance(b, dict) and b.get("schedulable") is True for b in put_bodies))
        post_bodies = [body for m, u, body in calls if m == "POST" and u.endswith("/schedulable")]
        self.assertTrue(any(isinstance(b, dict) and b.get("schedulable") is True for b in post_bodies))


class ExtractListPayloadTests(unittest.TestCase):
    def test_nested_items(self):
        items, total = mon._extract_list_payload({
            "code": 0,
            "data": {"items": [{"id": 1}, {"id": 2}], "total": 2},
        })
        self.assertEqual(len(items), 2)
        self.assertEqual(total, 2)


class AutoMonitorTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self._patches = [
            patch.object(mon, "_AUTO_DIR", root),
            patch.object(mon, "_AUTO_STATE_PATH", root / "auto_state.json"),
            patch.object(mon, "_AUTO_LOG_PATH", root / "auto.log"),
            patch.object(mon, "_AUTO_RUNS_DIR", root / "runs"),
            patch.object(mon, "_AUTO_DAILY_DIR", root / "daily"),
        ]
        for p in self._patches:
            p.start()
        mon._AUTO_RUNNING = False

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def test_collect_auto_targets_caps_reauth(self):
        items = []
        for i in range(15):
            items.append({
                "email": f"r{i}@x.com", "pool_id": 100 + i,
                "action": "reauth_repush",
            })
        items.append({"email": "dead@x.com", "pool_id": 1, "action": "mark_dead", "reason": "banned"})
        items.append({"email": "skip@x.com", "pool_id": 2, "action": "skip_dead"})
        items.append({"email": "mat@x.com", "pool_id": 3, "action": "need_material"})
        picked = mon.collect_auto_targets(items, max_reauth=10, delete_local_dead_in_pool=True)
        self.assertEqual(len(picked["reauth"]), 10)
        self.assertEqual(picked["reauth_total"], 15)
        self.assertEqual(picked["reauth_capped"], 5)
        self.assertEqual(len(picked["dead"]), 1)
        self.assertEqual(len(picked["residual_delete"]), 1)
        self.assertEqual(len(picked["need_material"]), 1)
        # 稳定按 pool_id 排序后取前 10
        self.assertEqual(picked["reauth"][0]["pool_id"], 100)
        self.assertEqual(picked["reauth"][-1]["pool_id"], 109)

    def test_auto_state_persist_enabled(self):
        st0 = mon.load_auto_state()
        self.assertFalse(st0.get("enabled"))
        mon.save_auto_state(enabled=True, interval_sec=900, max_reauth_per_cycle=10)
        st1 = mon.load_auto_state()
        self.assertTrue(st1.get("enabled"))
        self.assertEqual(st1.get("interval_sec"), 900)
        self.assertEqual(st1.get("max_reauth_per_cycle"), 10)

    def test_run_auto_cycle_skips_when_manual_busy(self):
        mon.save_auto_state(enabled=True)
        with patch.object(mon, "get_repair_job", return_value={"status": "running"}), \
             patch.object(mon, "scan_pool") as scan_mock:
            out = mon.run_auto_cycle(force=True)
        self.assertEqual(out.get("status"), "skipped")
        self.assertEqual(out.get("reason"), "busy_manual_repair")
        scan_mock.assert_not_called()

    def test_run_auto_cycle_scan_and_repair(self):
        mon.save_auto_state(enabled=True, max_reauth_per_cycle=10, max_workers=2)
        scan = {
            "ok": True,
            "scanned_at": "t",
            "group_id": 8,
            "summary": {
                "ours": 3, "rt_bad": 2, "dead": 1, "repairable": 2, "need_material": 0,
            },
            "items": [
                {"email": "a@x.com", "pool_id": 1, "action": "mark_dead", "reason": "banned"},
                {"email": "b@x.com", "pool_id": 2, "action": "reauth_repush"},
                {"email": "c@x.com", "pool_id": 3, "action": "reauth_repush"},
                {"email": "d@x.com", "pool_id": 4, "action": "skip_dead"},
            ],
        }

        def fake_repair(targets, **kwargs):
            results = []
            for t in targets:
                if t.get("action") == "mark_dead":
                    results.append({
                        "ok": True, "email": t["email"], "status": "dead",
                        "pool_deleted": [t.get("pool_id")],
                    })
                else:
                    results.append({"ok": True, "email": t["email"], "status": "repaired"})
            return {
                "ok": True, "success": sum(1 for r in results if r.get("status") == "repaired"),
                "dead": sum(1 for r in results if r.get("status") == "dead"),
                "failed": 0, "total": len(results), "results": results,
            }

        with patch.object(mon, "scan_pool", return_value=scan), \
             patch.object(mon, "repair_many", side_effect=fake_repair) as repair_mock, \
             patch.object(mon, "delete_pool_account", return_value={"ok": True, "deleted": True, "pool_id": 4}), \
             patch.object(mon, "get_repair_job", return_value=None):
            out = mon.run_auto_cycle(force=True)

        self.assertEqual(out.get("status"), "done")
        self.assertEqual(out["summary"]["dead_marked"], 1)
        self.assertEqual(out["summary"]["reauth_attempted"], 2)
        self.assertEqual(out["summary"]["reauth_success"], 2)
        self.assertEqual(out["summary"]["residual_pool_deleted"], 1)
        # 先 dead 后 reauth 一起进 repair_many
        args, kwargs = repair_mock.call_args
        targets = args[0]
        self.assertEqual(targets[0]["action"], "mark_dead")
        self.assertEqual(len([t for t in targets if t["action"] == "reauth_repush"]), 2)

        daily = mon.get_auto_daily(days=1)["daily"]
        self.assertEqual(daily["runs"], 1)
        self.assertEqual(daily["reauth_success"], 2)
        self.assertEqual(daily["dead_marked"], 1)

        runs = mon.list_auto_runs(limit=5)["items"]
        self.assertTrue(runs)
        self.assertEqual(runs[0]["status"], "done")


if __name__ == "__main__":
    unittest.main()
