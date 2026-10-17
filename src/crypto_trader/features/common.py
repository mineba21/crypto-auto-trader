"""Causal indicator primitives on numpy arrays.

Contract for every function here: output[i] depends only on inputs[0..i]. ``tests/test_lookahead.py`` checks this by
recomputing on truncated prefixes.
"""

from __future__ import annotations

import numpy as np


def ema(x: np.ndarray, span: int) -> np.ndarray:
    a = 2.0 / (span + 1)
    out = np.empty_like(x, dtype=float)
    acc = np.nan
    for i, v in enumerate(x):
        if np.isnan(v):
            out[i] = acc
            continue
        acc = v if np.isnan(acc) else a * v + (1 - a) * acc
        out[i] = acc
    return out


def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    c = np.cumsum(np.insert(x.astype(float), 0, 0.0))
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev = np.concatenate([[np.nan], close[:-1]])
    tr = np.nanmax(np.vstack([high - low, np.abs(high - prev), np.abs(low - prev)]), axis=0)
    tr[0] = high[0] - low[0]
    return tr


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder ATR; NaN until n bars exist."""
    tr = true_range(high, low, close)
    out = np.full(len(tr), np.nan)
    if len(tr) < n:
        return out
    out[n - 1] = tr[:n].mean()
    for i in range(n, len(tr)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    """max(x[i-n+1..i])"""
    out = np.full(len(x), np.nan)
    from numpy.lib.stride_tricks import sliding_window_view

    if len(x) >= n:
        out[n - 1:] = sliding_window_view(x, n).max(axis=1)
    return out


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    from numpy.lib.stride_tricks import sliding_window_view

    if len(x) >= n:
        out[n - 1:] = sliding_window_view(x, n).min(axis=1)
    return out


def shift(x: np.ndarray, k: int = 1) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[:-k] if k > 0 else x
    return out


def rolling_zscore(x: np.ndarray, n: int, min_periods: int | None = None) -> np.ndarray:
    import pandas as pd

    s = pd.Series(x, dtype=float)
    mp = min_periods or n
    m = s.rolling(n, min_periods=mp).mean()
    sd = s.rolling(n, min_periods=mp).std()
    return ((s - m) / sd.replace(0, np.nan)).to_numpy()


def rolling_percentile(x: np.ndarray, n: int, min_periods: int | None = None) -> np.ndarray:
    """Percentile rank (0..1) of x[i] within x[i-n+1..i]."""
    import pandas as pd

    s = pd.Series(x, dtype=float)
    return s.rolling(n, min_periods=min_periods or n).rank(pct=True).to_numpy()


def session_vwap(ts_ms: np.ndarray, typical: np.ndarray, volume: np.ndarray, session_ms: int = 86_400_000,
                 offset_ms: int = 0) -> np.ndarray:
    """VWAP reset each session (default UTC day; offset_ms=-9h*3600e3 for KST day)."""
    sess = (ts_ms + offset_ms) // session_ms
    out = np.empty(len(ts_ms))
    pv = vv = 0.0
    cur = None
    for i in range(len(ts_ms)):
        if sess[i] != cur:
            cur, pv, vv = sess[i], 0.0, 0.0
        pv += typical[i] * volume[i]
        vv += volume[i]
        out[i] = pv / vv if vv > 0 else typical[i]
    return out
