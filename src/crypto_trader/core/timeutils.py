"""Time helpers. Storage is UTC epoch-ms everywhere; Asia/Seoul is used only for display and daily risk resets."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

INTERVAL_MS = {
    "1s": 1_000,
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def ms_to_kst(ms: int) -> datetime:
    return ms_to_utc(ms).astimezone(KST)


def kst_date(ms: int) -> date:
    return ms_to_kst(ms).date()


def utc_date(ms: int) -> date:
    return ms_to_utc(ms).date()


def parse_utc(s: str) -> int:
    """Parse ISO-ish string as UTC (naive strings are treated as UTC)."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def fmt_kst(ms: int | None) -> str:
    if not ms:
        return "-"
    return ms_to_kst(ms).strftime("%Y-%m-%d %H:%M:%S KST")


def floor_ts(ms: int, interval: str) -> int:
    step = INTERVAL_MS[interval]
    return ms - (ms % step)
