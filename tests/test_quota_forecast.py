import unittest
from datetime import datetime

from core.quota_forecast import collect_quota_snapshot, extract_quota_windows, update_forecast


class QuotaForecastTests(unittest.TestCase):
    def test_snapshot_records_admin_usage_update_timestamp(self):
        snapshot = collect_quota_snapshot([{
            "id": 100,
            "extra": {
                "codex_7d_used_percent": 12,
                "codex_7d_window_minutes": 10080,
                "codex_7d_reset_after_seconds": 600000,
                "codex_usage_updated_at": "2026-08-16T09:33:41+08:00",
            },
        }], sampled_at=10)
        window = snapshot["accounts"]["100"]["10080m"]
        self.assertTrue(window["usage_updated_at_observed"])
        self.assertEqual(
            window["usage_updated_at"],
            datetime.fromisoformat("2026-08-16T09:33:41+08:00").timestamp(),
        )

    def test_disabled_zero_window_is_ignored_but_full_active_window_is_kept(self):
        account = {
            "id": 1,
            "extra": {
                "codex_5h_used_percent": 0,
                "codex_5h_window_minutes": 0,
                "codex_5h_reset_after_seconds": 0,
                "codex_5h_reset_at": "2026-08-16T01:50:28+08:00",
                "codex_7d_used_percent": 0,
                "codex_7d_window_minutes": 10080,
                "codex_7d_reset_after_seconds": 600000,
            },
        }
        windows = extract_quota_windows(account)
        self.assertNotIn("300m", windows)
        self.assertEqual(windows["10080m"]["remaining_units"], 1.0)

    def test_discovers_monthly_and_future_duration_windows_without_alias_double_count(self):
        account = {
            "id": 2,
            "extra": {
                "codex_primary_used_percent": 10,
                "codex_primary_reset_after_seconds": 100,
                "codex_30d_used_percent": 25,
                "codex_30d_window_minutes": 43200,
                "codex_30d_reset_after_seconds": 100000,
                "codex_14d_used_percent": 40,
                "codex_14d_window_minutes": 20160,
                "codex_14d_reset_after_seconds": 100000,
            },
        }
        windows = extract_quota_windows(account)
        self.assertEqual(set(windows), {"43200m", "20160m"})
        self.assertAlmostEqual(windows["43200m"]["remaining_units"], 0.75)
        self.assertAlmostEqual(windows["20160m"]["remaining_units"], 0.60)

    def test_two_samples_choose_earliest_window_eta(self):
        first = collect_quota_snapshot([{
            "id": 3,
            "extra": {
                "codex_7d_used_percent": 20,
                "codex_7d_window_minutes": 10080,
                "codex_7d_reset_after_seconds": 600000,
                "codex_30d_used_percent": 50,
                "codex_30d_window_minutes": 43200,
                "codex_30d_reset_after_seconds": 200000,
            },
        }], sampled_at=0)
        state, first_forecast = update_forecast(None, first, min_samples=2, safety_factor=1.0)
        self.assertEqual(first_forecast["status"], "insufficient")

        second = collect_quota_snapshot([{
            "id": 3,
            "extra": {
                "codex_7d_used_percent": 30,
                "codex_7d_window_minutes": 10080,
                "codex_7d_reset_after_seconds": 599400,
                "codex_30d_used_percent": 60,
                "codex_30d_window_minutes": 43200,
                "codex_30d_reset_after_seconds": 199400,
            },
        }], sampled_at=600)
        state, forecast = update_forecast(state, second, min_samples=2, safety_factor=1.0)
        self.assertEqual(forecast["status"], "ready")
        self.assertEqual(forecast["bottleneck_window"], "43200m")
        self.assertAlmostEqual(forecast["eta_minutes"], 40.0, places=3)
        self.assertEqual(state["sample_count"], 2)

    def test_window_reset_uses_new_baseline_without_rate_spike(self):
        first = collect_quota_snapshot([{
            "id": 4,
            "extra": {
                "codex_7d_used_percent": 90,
                "codex_7d_window_minutes": 10080,
                "codex_7d_reset_after_seconds": 30,
            },
        }], sampled_at=0)
        state, _ = update_forecast(None, first, min_samples=2, safety_factor=1.0)
        second = collect_quota_snapshot([{
            "id": 4,
            "extra": {
                "codex_7d_used_percent": 10,
                "codex_7d_window_minutes": 10080,
                "codex_7d_reset_after_seconds": 100000,
            },
        }], sampled_at=600)
        _, forecast = update_forecast(state, second, min_samples=2, safety_factor=1.0)
        window = forecast["windows"]["10080m"]
        self.assertEqual(window["reset_count"], 1)
        self.assertEqual(window["last_delta_units"], 0.0)
        self.assertEqual(window["rate_units_per_min"], 0.0)

    def test_small_usage_drop_with_timer_jump_is_not_a_reset(self):
        first = collect_quota_snapshot([{
            "id": 5,
            "extra": {
                "codex_7d_used_percent": 44,
                "codex_7d_window_minutes": 10080,
                "codex_7d_reset_after_seconds": 602255,
            },
        }], sampled_at=0)
        state, _ = update_forecast(None, first, min_samples=2, safety_factor=1.0)
        second = collect_quota_snapshot([{
            "id": 5,
            "extra": {
                "codex_7d_used_percent": 43,
                "codex_7d_window_minutes": 10080,
                "codex_7d_reset_after_seconds": 602287,
            },
        }], sampled_at=600)
        _, forecast = update_forecast(state, second, min_samples=2, safety_factor=1.0)
        window = forecast["windows"]["10080m"]
        self.assertEqual(window["reset_count"], 0)
        self.assertEqual(window["last_delta_units"], 0.0)
        self.assertEqual(window["rate_units_per_min"], 0.0)

    def test_rate_uses_admin_update_elapsed_instead_of_patrol_elapsed(self):
        def sample(used, sampled_at, updated_at):
            return collect_quota_snapshot([{
                "id": 12,
                "extra": {
                    "codex_7d_used_percent": used,
                    "codex_7d_window_minutes": 10080,
                    "codex_7d_reset_after_seconds": 600000,
                    "codex_usage_updated_at": updated_at,
                },
            }], sampled_at=sampled_at)

        state, _ = update_forecast(
            None,
            sample(5, 0, "2026-08-16T09:33:10+08:00"),
            min_samples=2,
            safety_factor=1.0,
        )
        state, _ = update_forecast(
            state,
            sample(5, 10, "2026-08-16T09:33:10+08:00"),
            min_samples=2,
            safety_factor=1.0,
        )
        _, forecast = update_forecast(
            state,
            sample(7, 20, "2026-08-16T09:33:40+08:00"),
            min_samples=2,
            safety_factor=1.0,
        )
        window = forecast["windows"]["10080m"]
        self.assertEqual(window["rate_samples"], 1)
        self.assertAlmostEqual(window["last_delta_units"], 0.02, places=6)
        self.assertAlmostEqual(window["rate_units_per_min"], 0.04, places=6)

    def test_forecast_waits_for_rate_coverage_before_becoming_ready(self):
        def sample(first_used, second_used, sampled_at, first_updated, second_updated):
            return collect_quota_snapshot([
                {
                    "id": 21,
                    "extra": {
                        "codex_7d_used_percent": first_used,
                        "codex_7d_window_minutes": 10080,
                        "codex_7d_reset_after_seconds": 600000,
                        "codex_usage_updated_at": first_updated,
                    },
                },
                {
                    "id": 22,
                    "extra": {
                        "codex_7d_used_percent": second_used,
                        "codex_7d_window_minutes": 10080,
                        "codex_7d_reset_after_seconds": 600000,
                        "codex_usage_updated_at": second_updated,
                    },
                },
            ], sampled_at=sampled_at)

        first_time = "2026-08-16T09:33:00+08:00"
        state, _ = update_forecast(
            None,
            sample(5, 5, 0, first_time, first_time),
            min_samples=2,
            safety_factor=1.0,
        )
        state, forecast = update_forecast(
            state,
            sample(7, 5, 10, "2026-08-16T09:33:30+08:00", first_time),
            min_samples=2,
            safety_factor=1.0,
        )
        window = forecast["windows"]["10080m"]
        self.assertEqual(forecast["status"], "insufficient")
        self.assertEqual(window["rate_coverage"], 0.5)

        _, forecast = update_forecast(
            state,
            sample(8, 7, 20, "2026-08-16T09:34:00+08:00", "2026-08-16T09:33:30+08:00"),
            min_samples=2,
            safety_factor=1.0,
        )
        window = forecast["windows"]["10080m"]
        self.assertEqual(forecast["status"], "ready")
        self.assertEqual(window["rate_coverage"], 1.0)

    def test_existing_reliable_rate_survives_new_account_coverage_dip(self):
        def sample(rows, sampled_at):
            return collect_quota_snapshot(rows, sampled_at=sampled_at)

        def account(account_id, used, updated_at):
            return {
                "id": account_id,
                "extra": {
                    "codex_7d_used_percent": used,
                    "codex_7d_window_minutes": 10080,
                    "codex_7d_reset_after_seconds": 600000,
                    "codex_usage_updated_at": updated_at,
                },
            }

        first_time = "2026-08-16T09:33:00+08:00"
        second_time = "2026-08-16T09:34:00+08:00"
        state, _ = update_forecast(
            None,
            sample([
                account(31, 0, first_time),
                account(32, 0, first_time),
            ], 0),
            min_samples=2,
            safety_factor=1.0,
            rate_window_minutes=5,
        )
        state, reliable = update_forecast(
            state,
            sample([
                account(31, 10, second_time),
                account(32, 10, second_time),
            ], 60),
            min_samples=2,
            safety_factor=1.0,
            rate_window_minutes=5,
        )
        self.assertEqual(reliable["status"], "ready")
        self.assertIn("10080m", state["reliable_rates"])

        # Account 32 has no new admin usage timestamp and account 33 is new.
        # The latest balance still includes both accounts, but the prior rate
        # remains usable because a reliable snapshot already exists.
        _, forecast = update_forecast(
            state,
            sample([
                account(31, 20, "2026-08-16T09:35:00+08:00"),
                account(32, 10, second_time),
                account(33, 0, "2026-08-16T09:35:00+08:00"),
            ], 301),
            min_samples=2,
            safety_factor=1.0,
            rate_window_minutes=5,
        )
        window = forecast["windows"]["10080m"]
        self.assertEqual(forecast["status"], "ready")
        self.assertEqual(window["rate_source"], "last_reliable")
        self.assertEqual(window["new_accounts"], 1)
        self.assertEqual(window["rate_coverage"], 0.5)
        self.assertAlmostEqual(window["remaining_units"], 2.7, places=6)
        self.assertAlmostEqual(window["rate_units_per_min"], 0.2, places=6)
        self.assertAlmostEqual(window["raw_rate_units_per_min"], 0.1, places=6)
        self.assertAlmostEqual(window["eta_minutes"], 13.5, places=3)

    def test_negative_jitter_and_recovery_have_zero_net_consumption(self):
        def sample(used, sampled_at, updated_at):
            return collect_quota_snapshot([{
                "id": 13,
                "extra": {
                    "codex_7d_used_percent": used,
                    "codex_7d_window_minutes": 10080,
                    "codex_7d_reset_after_seconds": 600000,
                    "codex_usage_updated_at": updated_at,
                },
            }], sampled_at=sampled_at)

        state, _ = update_forecast(None, sample(3, 0, "2026-08-16T09:33:00+08:00"), min_samples=2, safety_factor=1.0)
        state, _ = update_forecast(state, sample(1, 10, "2026-08-16T09:33:30+08:00"), min_samples=2, safety_factor=1.0)
        _, forecast = update_forecast(state, sample(3, 20, "2026-08-16T09:34:00+08:00"), min_samples=2, safety_factor=1.0)
        window = forecast["windows"]["10080m"]
        self.assertEqual(window["reset_count"], 0)
        self.assertEqual(window["last_delta_units"], 0.0)
        self.assertEqual(window["rate_units_per_min"], 0.0)

    def test_post_reset_rate_starts_after_new_baseline(self):
        def sample(used, sampled_at, updated_at, reset_after):
            return collect_quota_snapshot([{
                "id": 14,
                "extra": {
                    "codex_7d_used_percent": used,
                    "codex_7d_window_minutes": 10080,
                    "codex_7d_reset_after_seconds": reset_after,
                    "codex_usage_updated_at": updated_at,
                },
            }], sampled_at=sampled_at)

        state, _ = update_forecast(None, sample(90, 0, "2026-08-16T09:33:00+08:00", 30), min_samples=2, safety_factor=1.0)
        state, _ = update_forecast(state, sample(10, 10, "2026-08-16T09:33:30+08:00", 604800), min_samples=2, safety_factor=1.0)
        _, forecast = update_forecast(state, sample(12, 20, "2026-08-16T09:34:00+08:00", 604770), min_samples=2, safety_factor=1.0)
        window = forecast["windows"]["10080m"]
        self.assertEqual(window["reset_count"], 1)
        self.assertAlmostEqual(window["last_delta_units"], 0.02, places=6)
        self.assertAlmostEqual(window["rate_units_per_min"], 0.04, places=6)

    def test_new_account_addition_changes_capacity_not_consumption_rate(self):
        first = collect_quota_snapshot([{
            "id": 6,
            "extra": {
                "codex_7d_used_percent": 20,
                "codex_7d_window_minutes": 10080,
                "codex_7d_reset_after_seconds": 600000,
            },
        }], sampled_at=0)
        state, _ = update_forecast(None, first, min_samples=2, safety_factor=1.0)
        second = collect_quota_snapshot([
            {
                "id": 6,
                "extra": {
                    "codex_7d_used_percent": 30,
                    "codex_7d_window_minutes": 10080,
                    "codex_7d_reset_after_seconds": 599400,
                },
            },
            {
                "id": 7,
                "extra": {
                    "codex_7d_used_percent": 0,
                    "codex_7d_window_minutes": 10080,
                    "codex_7d_reset_after_seconds": 599400,
                },
            },
        ], sampled_at=600)
        _, forecast = update_forecast(state, second, min_samples=2, safety_factor=1.0)
        window = forecast["windows"]["10080m"]
        self.assertEqual(forecast["status"], "ready")
        self.assertEqual(window["matched_accounts"], 1)
        self.assertEqual(window["rate_account_population"], 1)
        self.assertEqual(window["rate_coverage"], 1.0)
        self.assertEqual(window["new_accounts"], 1)
        self.assertAlmostEqual(window["rate_units_per_min"], 0.01, places=6)
        self.assertAlmostEqual(window["remaining_units"], 1.7, places=6)

    def test_unhealthy_account_keeps_contributing_to_pool_demand_rate(self):
        def account(account_id, used):
            return {
                "id": account_id,
                "extra": {
                    "codex_7d_used_percent": used,
                    "codex_7d_window_minutes": 10080,
                    "codex_7d_reset_after_seconds": 600000,
                },
            }

        first_rows = [account(1, 0), account(2, 0)]
        first = collect_quota_snapshot(first_rows, rate_accounts=first_rows, sampled_at=0)
        state, _ = update_forecast(None, first, min_samples=2, safety_factor=1.0)

        healthy_rows = [account(1, 10)]
        all_rows = [account(1, 10), account(2, 10)]
        second = collect_quota_snapshot(
            healthy_rows,
            rate_accounts=all_rows,
            sampled_at=600,
        )
        _, forecast = update_forecast(state, second, min_samples=2, safety_factor=1.0)
        window = forecast["windows"]["10080m"]

        self.assertEqual(window["accounts"], 1)
        self.assertEqual(window["matched_accounts"], 2)
        self.assertEqual(window["rate_coverage_matched_accounts"], 1)
        self.assertEqual(window["rate_account_population"], 1)
        self.assertEqual(window["rate_pool_account_population"], 2)
        self.assertEqual(window["rate_coverage"], 1.0)
        self.assertEqual(window["removed_accounts"], 1)
        self.assertAlmostEqual(window["remaining_units"], 0.9, places=6)
        self.assertAlmostEqual(window["rate_units_per_min"], 0.02, places=6)
        self.assertAlmostEqual(window["eta_minutes"], 45.0, places=3)

    def test_rate_uses_only_the_configured_sliding_window(self):
        def sample(used, at):
            return collect_quota_snapshot([{
                "id": 8,
                "extra": {
                    "codex_7d_used_percent": used,
                    "codex_7d_window_minutes": 10080,
                    "codex_7d_reset_after_seconds": 600000 - at,
                },
            }], sampled_at=at)

        state, _ = update_forecast(None, sample(0, 0), min_samples=3, safety_factor=1.0, rate_window_minutes=5)
        state, _ = update_forecast(state, sample(20, 300), min_samples=3, safety_factor=1.0, rate_window_minutes=5)
        _, forecast = update_forecast(state, sample(20, 600), min_samples=3, safety_factor=1.0, rate_window_minutes=5)
        window = forecast["windows"]["10080m"]
        self.assertEqual(forecast["status"], "insufficient")
        self.assertEqual(window["rate_samples"], 1)
        self.assertAlmostEqual(window["last_delta_units"], 0.0, places=6)

    def test_sliding_window_keeps_latest_balance_after_account_churn(self):
        def sample(accounts, at):
            return collect_quota_snapshot([
                {
                    "id": account_id,
                    "extra": {
                        "codex_7d_used_percent": used,
                        "codex_7d_window_minutes": 10080,
                        "codex_7d_reset_after_seconds": 600000 - at,
                    },
                }
                for account_id, used in accounts
            ], sampled_at=at)

        state, _ = update_forecast(None, sample([(9, 10), (10, 10)], 0), min_samples=2, safety_factor=1.0)
        _, forecast = update_forecast(state, sample([(9, 20), (11, 0)], 600), min_samples=2, safety_factor=1.0)
        window = forecast["windows"]["10080m"]
        self.assertEqual(window["new_accounts"], 1)
        self.assertEqual(window["removed_accounts"], 1)
        self.assertEqual(window["matched_accounts"], 1)
        self.assertAlmostEqual(window["remaining_units"], 1.8, places=6)
        self.assertAlmostEqual(window["rate_units_per_min"], 0.01, places=6)

if __name__ == "__main__":
    unittest.main()
