"""Market data quality checks: gaps, duplicates, OHLC sanity, outliers, latency."""

from __future__ import annotations

import numpy as np
import polars as pl

from crypto_trader.core.timeutils import INTERVAL_MS


def ohlcv_quality(df: pl.DataFrame, interval: str, ts_col: str = "open_ts") -> dict:
    if df.is_empty():
        return {"rows": 0}
    step = INTERVAL_MS[interval]
    ts = df[ts_col].to_numpy()
    uniq = np.unique(ts)
    dup = len(ts) - len(uniq)
    expected = int((uniq[-1] - uniq[0]) // step) + 1
    diffs = np.diff(uniq)
    gaps = diffs[diffs > step]
    misaligned = int(np.sum(uniq % step != 0)) if step < 86_400_000 else 0
    o, h, low, c = (df[k].to_numpy().astype(float) for k in ("open", "high", "low", "close"))
    bad_ohlc = int(np.sum((h < np.maximum(o, c) - 1e-9) | (low > np.minimum(o, c) + 1e-9) | (low <= 0)))
    r = np.diff(np.log(c))
    sd = np.nanstd(r) if len(r) > 10 else np.nan
    outliers = int(np.sum(np.abs(r) > 8 * sd)) if sd and sd > 0 else 0
    vol = df["volume"].to_numpy().astype(float)
    largest = sorted(((int(uniq[i]), int(diffs[i] // step) - 1) for i in np.where(diffs > step)[0]), key=lambda x: -x[1])[:5]
    return {
        "rows": len(ts), "first_ts": int(uniq[0]), "last_ts": int(uniq[-1]), "expected": expected,
        "missing": expected - len(uniq), "missing_pct": (expected - len(uniq)) / expected if expected else 0.0,
        "gap_events": int(len(gaps)), "largest_gaps": largest, "duplicates": dup, "misaligned": misaligned,
        "bad_ohlc": bad_ohlc, "zero_volume": int(np.sum(vol <= 0)), "return_outliers_8sd": outliers,
    }


def regular_series_quality(df: pl.DataFrame, step_ms: int, ts_col: str = "event_ts") -> dict:
    if df.is_empty():
        return {"rows": 0}
    ts = np.unique(df[ts_col].to_numpy())
    expected = int((ts[-1] - ts[0]) // step_ms) + 1
    diffs = np.diff(ts)
    irregular = int(np.sum(diffs % step_ms != 0))
    return {"rows": len(ts), "first_ts": int(ts[0]), "last_ts": int(ts[-1]), "expected": expected,
            "missing": expected - len(ts), "missing_pct": (expected - len(ts)) / expected if expected else 0.0,
            "gap_events": int(np.sum(diffs > step_ms)), "irregular_steps": irregular}


def latency_quality(df: pl.DataFrame) -> dict:
    """received_ts - event_ts for live-captured rows."""
    if df.is_empty() or "received_ts" not in df.columns:
        return {"rows": 0}
    live = df.filter(pl.col("source") == "ws") if "source" in df.columns else df
    if live.is_empty():
        return {"rows": 0}
    lat = (live["received_ts"] - live["event_ts"]).to_numpy().astype(float)
    ev = live["event_ts"].to_numpy()
    return {"rows": len(lat), "first_ts": int(ev.min()), "last_ts": int(ev.max()), "lat_p50_ms": float(np.percentile(lat, 50)),
            "lat_p95_ms": float(np.percentile(lat, 95)), "lat_p99_ms": float(np.percentile(lat, 99)),
            "lat_max_ms": float(lat.max()), "negative_latency": int(np.sum(lat < 0)),
            "out_of_order": int(np.sum(np.diff(ev) < 0))}
