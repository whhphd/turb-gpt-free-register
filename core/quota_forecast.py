# -*- coding: utf-8 -*-
"""Generic OAuth quota-window sampling and exhaustion forecasting.

The Sub2API admin response has evolved from a fixed set of Codex windows to
window-specific fields.  This module deliberately discovers windows from the
response instead of hard-coding 5h/7d/30d.  It stores only account hashes and
quota percentages, never credentials or mailbox data.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from datetime import datetime
from typing import Any

_USED_RE = re.compile(r"^codex_(?P<label>[a-z0-9]+)_used_percent$", re.IGNORECASE)
_DURATION_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>m|h|d|w)$", re.IGNORECASE)
_ALIAS_LABELS = {"primary", "secondary"}
_RESET_USED_PERCENT_MAX = 10.0
_RESET_MIN_DROP_PERCENT = 5.0


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> float | None:
    number = _number(value)
    if number is not None:
        return number / 1000.0 if number > 10_000_000_000 else number
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _duration_minutes(label: str) -> float | None:
    match = _DURATION_RE.fullmatch(str(label or "").strip())
    if not match:
        return None
    value = float(match.group("value"))
    factor = {"m": 1.0, "h": 60.0, "d": 1440.0, "w": 10080.0}[match.group("unit").lower()]
    return value * factor


def _source_maps(account: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for candidate in (
        account,
        account.get("extra"),
        account.get("quota"),
        account.get("usage"),
    ):
        if isinstance(candidate, dict):
            sources.append(candidate)
    return sources


def _lookup(sources: list[dict[str, Any]], *keys: str) -> Any:
    for source in sources:
        for key in keys:
            if key in source and source[key] not in (None, ""):
                return source[key]
    return None


def _canonical_window(label: str, window_minutes: float | None) -> tuple[str, int]:
    duration = _duration_minutes(label)
    if duration is not None:
        return f"{duration:g}m", 2
    if window_minutes is not None and window_minutes > 0:
        return f"{window_minutes:g}m", 1
    return label.lower(), 0


def _explicit_windows(account: dict[str, Any]) -> list[dict[str, Any]]:
    """Read a future list-shaped quota API without requiring a new release."""
    rows: list[dict[str, Any]] = []
    for source in _source_maps(account):
        for key in ("quota_windows", "windows"):
            value = source.get(key)
            if isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, dict))
    return rows


def extract_quota_windows(account: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return active quota windows keyed by canonical duration/label.

    A zero-length window is disabled, even when its used percentage is zero.
    Alias fields such as ``primary`` and ``secondary`` are ignored unless the
    API also provides an explicit window length, preventing double counting of
    the same quota represented by a duration field.
    """
    if not isinstance(account, dict):
        return {}
    sources = _source_maps(account)
    common_usage_updated_at = _timestamp(_lookup(
        sources,
        "codex_usage_updated_at",
        "codexUsageUpdatedAt",
        "usage_updated_at",
        "usageUpdatedAt",
    ))
    candidates: list[tuple[int, str, dict[str, Any]]] = []

    for item in _explicit_windows(account):
        label = str(item.get("name") or item.get("label") or item.get("id") or "").strip().lower()
        used = _number(item.get("used_percent", item.get("usedPercent")))
        minutes_raw = item.get("window_minutes", item.get("windowMinutes"))
        minutes = _number(minutes_raw)
        reset_after = _number(item.get("reset_after_seconds", item.get("resetAfterSeconds")))
        if minutes_raw not in (None, "") and (minutes or 0) <= 0:
            continue
        if not label or used is None or (minutes or 0) <= 0 and (reset_after or 0) <= 0:
            continue
        usage_updated_at = _timestamp(item.get(
            "usage_updated_at",
            item.get("usageUpdatedAt", item.get("updated_at", item.get("updatedAt"))),
        )) or common_usage_updated_at
        key, specificity = _canonical_window(label, minutes)
        candidates.append((specificity + 3, key, {
            "label": label,
            "used_percent": max(0.0, min(100.0, used)),
            "window_minutes": max(0.0, minutes or 0.0),
            "reset_after_seconds": max(0.0, reset_after or 0.0),
            "capacity_units": max(0.0, _number(item.get("capacity_units", item.get("capacity"))) or 1.0),
            "usage_updated_at": usage_updated_at,
            "usage_updated_at_observed": usage_updated_at is not None,
        }))

    for source in sources:
        for raw_key, raw_used in source.items():
            match = _USED_RE.fullmatch(str(raw_key))
            if not match:
                continue
            label = match.group("label").lower()
            used = _number(raw_used)
            if used is None:
                continue
            minutes_raw = _lookup(sources,
                                  f"codex_{label}_window_minutes",
                                  f"codex_{label}_windowMinutes")
            minutes = _number(minutes_raw)
            reset_after = _number(_lookup(sources,
                                          f"codex_{label}_reset_after_seconds",
                                          f"codex_{label}_resetAfterSeconds"))
            reset_at = _lookup(sources, f"codex_{label}_reset_at", f"codex_{label}_resetAt")
            reset_at_active = reset_at not in (None, "", 0, 0.0, "0")
            if minutes_raw not in (None, "") and (minutes or 0) <= 0:
                continue
            # primary/secondary are aliases in current Sub2API payloads.  They
            # are accepted only when the payload describes their duration.
            if label in _ALIAS_LABELS and (minutes or 0) <= 0:
                continue
            if (minutes or 0) <= 0 and (reset_after or 0) <= 0 and not reset_at_active:
                continue
            key, specificity = _canonical_window(label, minutes)
            candidates.append((specificity, key, {
                "label": label,
                "used_percent": max(0.0, min(100.0, used)),
                "window_minutes": max(0.0, minutes or 0.0),
                "reset_after_seconds": max(0.0, reset_after or 0.0),
                "reset_at": reset_at,
                "capacity_units": max(0.0, _number(_lookup(
                    sources,
                    f"codex_{label}_capacity_units",
                    f"codex_{label}_capacity",
                )) or 1.0),
                "usage_updated_at": common_usage_updated_at,
                "usage_updated_at_observed": common_usage_updated_at is not None,
            }))

    selected: dict[str, tuple[int, dict[str, Any]]] = {}
    for specificity, key, window in candidates:
        current = selected.get(key)
        if current is None or specificity > current[0]:
            selected[key] = (specificity, window)
    result: dict[str, dict[str, Any]] = {}
    for key, (_, window) in selected.items():
        remaining = max(0.0, 1.0 - window["used_percent"] / 100.0)
        window["remaining_fraction"] = remaining
        window["remaining_units"] = remaining * window["capacity_units"]
        result[key] = window
    return result


def _account_key(account: dict[str, Any], index: int) -> str:
    raw = account.get("id") or account.get("account_id") or account.get("uuid")
    if raw not in (None, ""):
        return str(raw)
    # Do not persist a name/email fallback; only its short hash is retained.
    stable = "|".join(str(account.get(key) or "") for key in ("name", "created_at", "updated_at"))
    if not stable:
        stable = f"anonymous:{index}"
    return "hash:" + hashlib.sha256(stable.encode("utf-8", "ignore")).hexdigest()[:16]


def _quota_rows(accounts: list[dict[str, Any]], snapshot_at: float) -> dict[str, dict[str, dict[str, Any]]]:
    rows: dict[str, dict[str, dict[str, Any]]] = {}
    for index, account in enumerate(accounts or []):
        windows = extract_quota_windows(account)
        if windows:
            for window in windows.values():
                if _timestamp(window.get("usage_updated_at")) is None:
                    window["usage_updated_at"] = snapshot_at
                    window["usage_updated_at_observed"] = False
            rows[_account_key(account, index)] = windows
    return rows


def collect_quota_snapshot(
    accounts: list[dict[str, Any]],
    *,
    sampled_at: float | None = None,
    rate_accounts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a credential-free snapshot suitable for persistence.

    ``accounts`` supplies the currently usable balance. ``rate_accounts`` may
    include unhealthy accounts from the same monitored pool so account churn
    does not erase observed demand from the sliding consumption rate.
    """
    snapshot_at = float(sampled_at if sampled_at is not None else time.time())
    rows = _quota_rows(accounts, snapshot_at)
    rate_rows = rows if rate_accounts is None else _quota_rows(rate_accounts, snapshot_at)
    return {
        "sampled_at": snapshot_at,
        "accounts": rows,
        "rate_accounts": rate_rows,
        "account_count": len(accounts or []),
        "quota_account_count": len(rows),
        "rate_account_count": len(rate_rows),
    }


def _aggregate(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    aggregate: dict[str, dict[str, Any]] = {}
    for account_windows in (snapshot.get("accounts") or {}).values():
        if not isinstance(account_windows, dict):
            continue
        for key, window in account_windows.items():
            if not isinstance(window, dict):
                continue
            item = aggregate.setdefault(key, {
                "accounts": 0,
                "remaining_units": 0.0,
                "capacity_units_sum": 0.0,
                "used_percent_sum": 0.0,
                "window_minutes": window.get("window_minutes") or 0.0,
                "reset_after_seconds": [],
            })
            item["accounts"] += 1
            item["remaining_units"] += float(window.get("remaining_units") or 0.0)
            item["capacity_units_sum"] += float(window.get("capacity_units") or 1.0)
            item["used_percent_sum"] += float(window.get("used_percent") or 0.0)
            item["reset_after_seconds"].append(float(window.get("reset_after_seconds") or 0.0))
    return aggregate


def _consumed_since(previous: dict[str, Any], current: dict[str, Any]) -> tuple[float, bool]:
    previous_used = float(previous.get("used_percent") or 0.0)
    current_used = float(current.get("used_percent") or 0.0)
    delta = current_used - previous_used
    if delta >= -0.01:
        return max(0.0, delta) / 100.0 * float(current.get("capacity_units") or 1.0), False
    # A real window reset clears usage.  A timer jump alone is too noisy: the
    # upstream payload can move the timer while the rounded usage changes by
    # only one percentage point.  Treat the reset sample as a new baseline so
    # its post-reset balance is not mistaken for instantaneous consumption.
    used_drop = previous_used - current_used
    timer_jump = float(current.get("reset_after_seconds") or 0.0) > float(previous.get("reset_after_seconds") or 0.0) + 30.0
    reset = current_used <= _RESET_USED_PERCENT_MAX and used_drop >= _RESET_MIN_DROP_PERCENT and (timer_jump or used_drop >= 10.0)
    if reset:
        return 0.0, True
    return 0.0, False


def _snapshot_time(snapshot: dict[str, Any]) -> float | None:
    value = snapshot.get("sampled_at") if isinstance(snapshot, dict) else None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _history_from_state(previous_state: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """Load a bounded, de-duplicated snapshot history from old or new state."""
    candidates: list[dict[str, Any]] = []
    samples = previous_state.get("samples")
    if isinstance(samples, list):
        candidates.extend(item for item in samples if isinstance(item, dict))
    previous = previous_state.get("previous_snapshot")
    if isinstance(previous, dict) and not any(item is previous for item in candidates):
        candidates.append(previous)
    candidates.append(current)
    unique: dict[float, dict[str, Any]] = {}
    for item in candidates:
        timestamp = _snapshot_time(item)
        if timestamp is not None:
            unique[timestamp] = item
    return [unique[key] for key in sorted(unique)]


def _snapshot_rate_accounts(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    rate_accounts = snapshot.get("rate_accounts")
    if isinstance(rate_accounts, dict):
        return rate_accounts
    accounts = snapshot.get("accounts")
    return accounts if isinstance(accounts, dict) else {}


def _consumption_over_history(
    account_key: str,
    window_key: str,
    history: list[dict[str, Any]],
) -> tuple[float, float, int, int, bool]:
    raw_observations: list[tuple[float, bool, dict[str, Any]]] = []
    for snapshot in history:
        accounts = _snapshot_rate_accounts(snapshot)
        window = accounts.get(account_key, {}).get(window_key) if isinstance(accounts, dict) else None
        if not isinstance(window, dict):
            continue
        observed_at = _timestamp(window.get("usage_updated_at"))
        is_admin_timestamp = bool(window.get("usage_updated_at_observed")) and observed_at is not None
        fallback_at = _snapshot_time(snapshot)
        timestamp = observed_at if is_admin_timestamp else fallback_at
        if timestamp is not None:
            raw_observations.append((timestamp, is_admin_timestamp, window))

    if not raw_observations:
        return 0.0, 0.0, 0, 0, False
    has_admin_timestamps = any(item[1] for item in raw_observations)
    observations: list[tuple[float, dict[str, Any]]] = []
    for timestamp, is_admin_timestamp, window in raw_observations:
        if has_admin_timestamps and not is_admin_timestamp:
            continue
        if observations and timestamp < observations[-1][0] - 0.001:
            continue
        if observations and abs(timestamp - observations[-1][0]) <= 0.001:
            observations[-1] = (timestamp, window)
        else:
            observations.append((timestamp, window))
    if len(observations) < 2:
        return 0.0, 0.0, 0, 0, False

    consumed = 0.0
    measured_seconds = 0.0
    resets = 0
    segment_at, segment_window = observations[0]
    previous_at, previous_window = observations[0]

    def close_segment(end_at: float, end_window: dict[str, Any]) -> tuple[float, float]:
        duration = max(0.0, end_at - segment_at)
        used_delta = max(
            0.0,
            float(end_window.get("used_percent") or 0.0) - float(segment_window.get("used_percent") or 0.0),
        )
        capacity = float(end_window.get("capacity_units") or 1.0)
        return used_delta / 100.0 * capacity, duration

    for current_at, current_window in observations[1:]:
        _, reset = _consumed_since(previous_window, current_window)
        if reset:
            value, duration = close_segment(previous_at, previous_window)
            consumed += value
            measured_seconds += duration
            resets += 1
            segment_at, segment_window = current_at, current_window
        previous_at, previous_window = current_at, current_window

    value, duration = close_segment(previous_at, previous_window)
    consumed += value
    measured_seconds += duration
    rate = consumed / (measured_seconds / 60.0) if measured_seconds > 0 else 0.0
    return consumed, rate, resets, len(observations) - 1, True


def _reliable_rates_from_state(previous_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Load reliable rates, migrating a ready pre-fallback state when needed."""
    rates = previous_state.get("reliable_rates")
    if isinstance(rates, dict) and rates:
        return {
            str(key): dict(value)
            for key, value in rates.items()
            if isinstance(value, dict)
        }
    forecast = previous_state.get("forecast")
    legacy_windows = forecast.get("windows") if isinstance(forecast, dict) else None
    if not isinstance(forecast, dict) or forecast.get("status") != "ready" or not isinstance(legacy_windows, dict):
        return {}
    sampled_at = _snapshot_time({"sampled_at": forecast.get("sampled_at")})
    migrated: dict[str, dict[str, Any]] = {}
    for key, value in legacy_windows.items():
        if not isinstance(value, dict) or _number(value.get("rate_units_per_min")) is None:
            continue
        migrated[str(key)] = {
            "rate_units_per_min": max(0.0, float(value.get("rate_units_per_min") or 0.0)),
            "sampled_at": sampled_at,
            "rate_samples": int(value.get("rate_samples") or 0),
            "rate_coverage": float(value.get("rate_coverage") or 0.0),
        }
    return migrated


def update_forecast(
    previous_state: dict[str, Any] | None,
    snapshot: dict[str, Any],
    *,
    min_samples: int = 3,
    safety_factor: float = 1.2,
    rate_window_minutes: int = 10,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Update a per-window sliding-rate forecast.

    The current snapshot's ``accounts`` supplies remaining capacity for every
    healthy account. Consumption uses ``rate_accounts`` from the full monitored
    pool so a health transition cannot make observed demand disappear. New
    accounts still need observations at both ends of the sliding window.
    Consecutive samples preserve usage across quota resets; ETA is the minimum
    of all active quota windows.
    """
    previous_state = previous_state if isinstance(previous_state, dict) else {}
    current = snapshot if isinstance(snapshot, dict) else {"sampled_at": time.time(), "accounts": {}, "account_count": 0}
    current_at = _snapshot_time(current)
    if current_at is None:
        current_at = time.time()
        current = dict(current)
        current["sampled_at"] = current_at
    history = _history_from_state(previous_state, current)
    window_seconds = max(60.0, float(rate_window_minutes or 10) * 60.0)
    cutoff = current_at - window_seconds
    history = [
        item for item in history
        if ((_snapshot_time(item) if _snapshot_time(item) is not None else current_at) >= cutoff)
    ]
    if not history or _snapshot_time(history[-1]) != current_at:
        history.append(current)
    oldest = history[0]
    oldest_value = _snapshot_time(oldest)
    oldest_at = oldest_value if oldest_value is not None else current_at
    elapsed_minutes = max(0.0, (current_at - oldest_at) / 60.0)
    sample_count = int(previous_state.get("sample_count") or 0) + 1
    current_accounts = current.get("accounts") if isinstance(current.get("accounts"), dict) else {}
    oldest_accounts = oldest.get("accounts") if isinstance(oldest.get("accounts"), dict) else {}
    remaining_cohort_keys = set(current_accounts).intersection(oldest_accounts)
    new_account_count = max(0, len(current_accounts) - len(remaining_cohort_keys))
    removed_account_count = max(0, len(oldest_accounts) - len(remaining_cohort_keys))
    current_rate_accounts = _snapshot_rate_accounts(current)
    oldest_rate_accounts = _snapshot_rate_accounts(oldest)
    rate_cohort_keys = set(current_rate_accounts).intersection(oldest_rate_accounts)
    aggregate = _aggregate(current)
    total_current_accounts = max(len(current_accounts), int(current.get("account_count") or 0))
    quota_account_count = len(current_accounts)
    estimated_full_accounts = max(0, total_current_accounts - quota_account_count)
    # A healthy account can briefly appear in the pool before Sub2API returns
    # its quota fields. Reuse the last known window schema so that this account
    # contributes a full balance immediately, while rate calculations continue
    # to use only real quota observations.
    if estimated_full_accounts and not aggregate:
        aggregate = _aggregate(oldest)
        if not aggregate:
            prior_forecast = previous_state.get("forecast") if isinstance(previous_state, dict) else None
            prior_windows = prior_forecast.get("windows") if isinstance(prior_forecast, dict) else None
            if isinstance(prior_windows, dict):
                for window_key, value in prior_windows.items():
                    if not isinstance(value, dict):
                        continue
                    capacity = max(1e-12, float(value.get("capacity_units_per_account") or 1.0))
                    aggregate[str(window_key)] = {
                        "accounts": 0,
                        "remaining_units": 0.0,
                        "capacity_units_sum": 0.0,
                        "used_percent_sum": 0.0,
                        "window_minutes": value.get("window_minutes") or 0.0,
                        "reset_after_seconds": [],
                        "_fallback_capacity": capacity,
                    }
    if estimated_full_accounts:
        for item in aggregate.values():
            known_accounts = int(item.get("accounts") or 0)
            capacity = (
                float(item.get("capacity_units_sum") or 0.0) / known_accounts
                if known_accounts > 0
                else float(item.get("_fallback_capacity") or 1.0)
            )
            capacity = max(1e-12, capacity)
            item["accounts"] = known_accounts + estimated_full_accounts
            item["remaining_units"] = float(item.get("remaining_units") or 0.0) + estimated_full_accounts * capacity
            item["capacity_units_sum"] = float(item.get("capacity_units_sum") or 0.0) + estimated_full_accounts * capacity
            item["estimated_full_accounts"] = estimated_full_accounts
    windows: dict[str, dict[str, Any]] = {}
    next_rates: dict[str, dict[str, Any]] = {}
    valid_etas: list[tuple[float, str]] = []
    safe_factor = max(1.0, float(safety_factor or 1.0))
    min_required = max(2, int(min_samples or 3))
    previous_reliable_rates = _reliable_rates_from_state(previous_state)
    reliable_rates = dict(previous_reliable_rates)
    # Rate history length and reliable-snapshot lifetime solve different problems.
    # Keep a recent reliable rate through short-lived account churn even when the
    # operator chooses a small sliding window for responsiveness.
    reliable_rate_ttl_minutes = max(60.0, float(rate_window_minutes or 10))

    for window_key, item in aggregate.items():
        consumed = 0.0
        actual_rate = 0.0
        matched_accounts = 0
        coverage_matched_accounts = 0
        reset_count = 0
        rate_samples = 0
        cohort_window_accounts = sum(
            1
            for account_key in remaining_cohort_keys
            if isinstance(current_accounts.get(account_key, {}).get(window_key), dict)
        )
        rate_pool_window_accounts = sum(
            1
            for account_key in rate_cohort_keys
            if isinstance(current_rate_accounts.get(account_key, {}).get(window_key), dict)
        )
        for account_key in rate_cohort_keys:
            value, account_rate, resets, account_samples, observed = _consumption_over_history(
                account_key,
                window_key,
                history,
            )
            if observed:
                consumed += value
                actual_rate += account_rate
                reset_count += resets
                rate_samples = max(rate_samples, account_samples)
                matched_accounts += 1
                if account_key in remaining_cohort_keys:
                    coverage_matched_accounts += 1
        actual_rate = max(0.0, actual_rate)
        raw_rate = actual_rate
        remaining = max(0.0, float(item.get("remaining_units") or 0.0))
        coverage = float(item.get("accounts") or 0) / max(1, total_current_accounts)
        rate_coverage = coverage_matched_accounts / max(1, cohort_window_accounts)
        required_rate_samples = max(1, min_required - 1)
        coverage_insufficient = rate_samples < required_rate_samples or rate_coverage < 0.8
        rate_source = "current"
        rate_snapshot_age = None
        previous_reliable = previous_reliable_rates.get(window_key)
        if coverage_insufficient and isinstance(previous_reliable, dict):
            previous_rate = max(0.0, float(previous_reliable.get("rate_units_per_min") or 0.0))
            previous_at = _timestamp(previous_reliable.get("sampled_at"))
            if previous_rate > 1e-12 and previous_at is not None:
                rate_snapshot_age = max(0.0, (current_at - previous_at) / 60.0)
                if rate_snapshot_age <= reliable_rate_ttl_minutes:
                    actual_rate = previous_rate
                    rate_source = "last_reliable"
        planned_rate = actual_rate * safe_factor
        eta = remaining / planned_rate if planned_rate > 1e-12 else None
        if rate_source == "last_reliable":
            status = "ready"
        elif coverage_insufficient:
            status = "insufficient"
        elif actual_rate <= 1e-12:
            status = "no_rate"
        else:
            status = "ready"
        if status == "ready" and eta is not None:
            valid_etas.append((eta, window_key))
        windows[window_key] = {
            "accounts": int(item.get("accounts") or 0),
            "coverage": round(coverage, 4),
            "rate_coverage": round(rate_coverage, 4),
            "remaining_units": round(remaining, 6),
            "capacity_units_per_account": round(
                float(item.get("capacity_units_sum") or 0.0) / max(1, int(item.get("accounts") or 0)),
                6,
            ),
            "used_percent_avg": round(float(item.get("used_percent_sum") or 0.0) / max(1, int(item.get("accounts") or 0)), 4),
            "rate_units_per_min": round(actual_rate, 8),
            "raw_rate_units_per_min": round(raw_rate, 8),
            "planned_rate_units_per_min": round(planned_rate, 8),
            "eta_minutes": round(eta, 3) if eta is not None else None,
            "status": status,
            "rate_source": rate_source,
            "rate_snapshot_age_minutes": round(rate_snapshot_age, 4) if rate_snapshot_age is not None else None,
            "elapsed_minutes": round(elapsed_minutes, 4) if elapsed_minutes else None,
            "matched_accounts": matched_accounts,
            "rate_coverage_matched_accounts": coverage_matched_accounts,
            "rate_account_population": cohort_window_accounts,
            "rate_pool_account_population": rate_pool_window_accounts,
            "rate_samples": rate_samples,
            "new_accounts": new_account_count,
            "removed_accounts": removed_account_count,
            "estimated_full_accounts": int(item.get("estimated_full_accounts") or 0),
            "last_delta_units": round(consumed, 6),
            "reset_count": reset_count,
        }
        next_rates[window_key] = {
            "rate_units_per_min": actual_rate,
            "rate_samples": rate_samples,
            "last_delta_units": consumed,
            "matched_accounts": matched_accounts,
            "reset_count": reset_count,
            "rate_source": rate_source,
        }
        if not coverage_insufficient and raw_rate > 1e-12:
            reliable_rates[window_key] = {
                "rate_units_per_min": raw_rate,
                "sampled_at": current_at,
                "rate_samples": rate_samples,
                "rate_coverage": rate_coverage,
            }

    if valid_etas:
        eta, window_key = min(valid_etas)
        overall_status = "ready"
        confidence = "low" if windows[window_key]["rate_source"] == "last_reliable" else (
            "ready" if windows[window_key]["coverage"] >= 0.8 else "low"
        )
    elif windows:
        eta, window_key = None, None
        overall_status = "insufficient" if any(
            item.get("status") == "insufficient" for item in windows.values()
        ) else "no_rate"
        confidence = "insufficient" if overall_status == "insufficient" else "low"
    else:
        eta, window_key = None, None
        overall_status = "insufficient"
        confidence = "insufficient"

    forecast = {
        "sampled_at": current_at,
        "sample_count": sample_count,
        "rate_window_minutes": int(max(1, int(rate_window_minutes or 10))),
        "elapsed_minutes": round(elapsed_minutes, 4) if elapsed_minutes else None,
        "status": overall_status,
        "confidence": confidence,
        "eta_minutes": round(eta, 3) if eta is not None else None,
        "bottleneck_window": window_key,
        "windows": windows,
        "new_accounts": new_account_count,
        "removed_accounts": removed_account_count,
        "estimated_full_accounts": estimated_full_accounts,
        "quota_account_count": quota_account_count,
        "account_count": total_current_accounts,
    }
    if window_key is not None and isinstance(windows.get(window_key), dict):
        forecast["rate_source"] = windows[window_key].get("rate_source")
        forecast["rate_snapshot_age_minutes"] = windows[window_key].get("rate_snapshot_age_minutes")
    next_state = {
        "sample_count": sample_count,
        "last_sampled_at": current_at,
        "previous_snapshot": current,
        "samples": history,
        # Keep this summary for operators and old state readers.
        "windows": next_rates,
        "reliable_rates": reliable_rates,
        "forecast": forecast,
    }
    return next_state, forecast
