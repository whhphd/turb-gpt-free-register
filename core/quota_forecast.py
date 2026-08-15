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
from typing import Any

_USED_RE = re.compile(r"^codex_(?P<label>[a-z0-9]+)_used_percent$", re.IGNORECASE)
_DURATION_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>m|h|d|w)$", re.IGNORECASE)
_ALIAS_LABELS = {"primary", "secondary"}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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
        key, specificity = _canonical_window(label, minutes)
        candidates.append((specificity + 3, key, {
            "label": label,
            "used_percent": max(0.0, min(100.0, used)),
            "window_minutes": max(0.0, minutes or 0.0),
            "reset_after_seconds": max(0.0, reset_after or 0.0),
            "capacity_units": max(0.0, _number(item.get("capacity_units", item.get("capacity"))) or 1.0),
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


def collect_quota_snapshot(accounts: list[dict[str, Any]], *, sampled_at: float | None = None) -> dict[str, Any]:
    """Create a credential-free snapshot suitable for persistence."""
    rows: dict[str, dict[str, dict[str, Any]]] = {}
    for index, account in enumerate(accounts or []):
        windows = extract_quota_windows(account)
        if windows:
            rows[_account_key(account, index)] = windows
    return {
        "sampled_at": float(sampled_at if sampled_at is not None else time.time()),
        "accounts": rows,
        "account_count": len(accounts or []),
        "quota_account_count": len(rows),
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
                "used_percent_sum": 0.0,
                "window_minutes": window.get("window_minutes") or 0.0,
                "reset_after_seconds": [],
            })
            item["accounts"] += 1
            item["remaining_units"] += float(window.get("remaining_units") or 0.0)
            item["used_percent_sum"] += float(window.get("used_percent") or 0.0)
            item["reset_after_seconds"].append(float(window.get("reset_after_seconds") or 0.0))
    return aggregate


def _consumed_since(previous: dict[str, Any], current: dict[str, Any]) -> tuple[float, bool]:
    previous_used = float(previous.get("used_percent") or 0.0)
    current_used = float(current.get("used_percent") or 0.0)
    delta = current_used - previous_used
    if delta >= -0.01:
        return max(0.0, delta) / 100.0 * float(current.get("capacity_units") or 1.0), False
    # A large drop accompanied by a reset timer jump is a new quota window.
    reset = float(current.get("reset_after_seconds") or 0.0) > float(previous.get("reset_after_seconds") or 0.0) + 30.0
    if reset or delta < -5.0:
        return max(0.0, current_used) / 100.0 * float(current.get("capacity_units") or 1.0), True
    return 0.0, False


def update_forecast(
    previous_state: dict[str, Any] | None,
    snapshot: dict[str, Any],
    *,
    min_samples: int = 3,
    safety_factor: float = 1.2,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Update a cohort-matched EWMA rate and return ``(state, forecast)``.

    Account churn is common during replenishment.  New accounts contribute to
    current remaining capacity but never to the consumption delta until they
    have a matching sample; removed accounts likewise cannot create a fake
    consumption spike.  The EWMA smooths delayed percentage refreshes without
    mixing different pool sizes in a fixed sliding window.
    """
    previous_state = previous_state if isinstance(previous_state, dict) else {}
    previous_snapshot = previous_state.get("previous_snapshot")
    if not isinstance(previous_snapshot, dict):
        samples = previous_state.get("samples")
        if isinstance(samples, list) and samples and isinstance(samples[-1], dict):
            previous_snapshot = samples[-1]
    current_accounts = snapshot.get("accounts") if isinstance(snapshot, dict) else {}
    current_accounts = current_accounts if isinstance(current_accounts, dict) else {}
    previous_accounts = previous_snapshot.get("accounts") if isinstance(previous_snapshot, dict) else {}
    previous_accounts = previous_accounts if isinstance(previous_accounts, dict) else {}
    sampled_at = snapshot.get("sampled_at")
    current_at = float(time.time() if sampled_at is None else sampled_at)
    previous_value = previous_snapshot.get("sampled_at") if isinstance(previous_snapshot, dict) else None
    previous_at = float(previous_value) if previous_value is not None else None
    elapsed_minutes = max(0.0, (current_at - previous_at) / 60.0) if previous_at is not None else 0.0
    sample_count = int(previous_state.get("sample_count") or 0) + 1
    rate_state = previous_state.get("windows") if isinstance(previous_state.get("windows"), dict) else {}
    next_rates: dict[str, dict[str, Any]] = {}
    deltas: dict[str, dict[str, Any]] = {}

    if previous_snapshot and elapsed_minutes > 0:
        for account_key, current_windows in current_accounts.items():
            old_windows = previous_accounts.get(account_key)
            if not isinstance(current_windows, dict) or not isinstance(old_windows, dict):
                continue
            for window_key, current_window in current_windows.items():
                previous_window = old_windows.get(window_key)
                if not isinstance(previous_window, dict) or not isinstance(current_window, dict):
                    continue
                consumed, reset = _consumed_since(previous_window, current_window)
                item = deltas.setdefault(window_key, {
                    "consumed_units": 0.0,
                    "matched_accounts": 0,
                    "resets": 0,
                })
                item["consumed_units"] += consumed
                item["matched_accounts"] += 1
                item["resets"] += int(reset)

    aggregate = _aggregate(snapshot)
    windows: dict[str, dict[str, Any]] = {}
    valid_etas: list[tuple[float, str]] = []
    safe_factor = max(1.0, float(safety_factor or 1.0))
    total_current_accounts = max(0, int(snapshot.get("account_count") or 0))
    matched_account_keys = {key for key in current_accounts if key in previous_accounts}
    new_account_count = max(0, len(current_accounts) - len(matched_account_keys))
    removed_account_count = max(0, len(previous_accounts) - len(matched_account_keys))

    for window_key, item in aggregate.items():
        old_rate = rate_state.get(window_key) if isinstance(rate_state.get(window_key), dict) else {}
        delta = deltas.get(window_key, {})
        rate_now = float(delta.get("consumed_units") or 0.0) / elapsed_minutes if elapsed_minutes > 0 else None
        old_rate_value = float(old_rate.get("rate_units_per_min") or 0.0)
        if rate_now is None:
            actual_rate = old_rate_value
        elif int(old_rate.get("rate_samples") or 0) <= 0:
            actual_rate = rate_now
        else:
            actual_rate = 0.35 * rate_now + 0.65 * old_rate_value
        planned_rate = actual_rate * safe_factor
        remaining = max(0.0, float(item.get("remaining_units") or 0.0))
        eta = remaining / planned_rate if planned_rate > 1e-12 else None
        coverage = float(item.get("accounts") or 0) / max(1, total_current_accounts)
        rate_samples = int(old_rate.get("rate_samples") or 0) + (1 if rate_now is not None else 0)
        if sample_count < max(2, int(min_samples or 3)) or not rate_samples:
            status = "insufficient"
        elif actual_rate <= 1e-12:
            status = "no_rate"
        else:
            status = "ready"
            if eta is not None:
                valid_etas.append((eta, window_key))
        windows[window_key] = {
            "accounts": int(item.get("accounts") or 0),
            "coverage": round(coverage, 4),
            "remaining_units": round(remaining, 6),
            "used_percent_avg": round(float(item.get("used_percent_sum") or 0.0) / max(1, int(item.get("accounts") or 0)), 4),
            "rate_units_per_min": round(actual_rate, 8),
            "planned_rate_units_per_min": round(planned_rate, 8),
            "eta_minutes": round(eta, 3) if eta is not None else None,
            "status": status,
            "matched_accounts": int(delta.get("matched_accounts") or 0),
            "rate_samples": rate_samples,
            "new_accounts": new_account_count,
            "removed_accounts": removed_account_count,
            "last_delta_units": round(float(delta.get("consumed_units") or 0.0), 6),
            "reset_count": int(old_rate.get("reset_count") or 0) + int(delta.get("resets") or 0),
        }
        next_rates[window_key] = {
            "rate_units_per_min": actual_rate,
            "rate_samples": rate_samples,
            "last_delta_units": float(delta.get("consumed_units") or 0.0),
            "matched_accounts": int(delta.get("matched_accounts") or 0),
            "reset_count": int(old_rate.get("reset_count") or 0) + int(delta.get("resets") or 0),
        }

    if valid_etas:
        eta, window_key = min(valid_etas)
        overall_status = "ready"
        confidence = "ready" if windows[window_key]["coverage"] >= 0.8 else "low"
    elif windows:
        eta, window_key = None, None
        overall_status = "insufficient" if sample_count < max(2, int(min_samples or 3)) else "no_rate"
        confidence = "insufficient" if overall_status == "insufficient" else "low"
    else:
        eta, window_key = None, None
        overall_status = "insufficient"
        confidence = "insufficient"

    forecast = {
        "sampled_at": current_at,
        "sample_count": sample_count,
        "elapsed_minutes": round(elapsed_minutes, 4) if elapsed_minutes else None,
        "status": overall_status,
        "confidence": confidence,
        "eta_minutes": round(eta, 3) if eta is not None else None,
        "bottleneck_window": window_key,
        "windows": windows,
    }
    next_state = {
        "sample_count": sample_count,
        "last_sampled_at": current_at,
        "previous_snapshot": snapshot,
        "windows": next_rates,
        "forecast": forecast,
    }
    return next_state, forecast
