# -*- coding: utf-8 -*-
"""统一业务时间：默认使用北京时间（Asia/Shanghai / UTC+8）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _beijing_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo("Asia/Shanghai")
        except Exception:
            pass
    # Windows 未装 tzdata 时回退固定 UTC+8（北京时间无夏令时）
    return timezone(timedelta(hours=8))


BEIJING_TZ = _beijing_tz()


def beijing_now() -> datetime:
    """返回不带 tzinfo 的北京时间（墙钟），便于写入既有 naive ISO 字段。"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def beijing_now_iso() -> str:
    """北京时间 ISO 字符串，秒级精度，如 2026-08-06T02:15:30。"""
    return beijing_now().isoformat(timespec="seconds")
