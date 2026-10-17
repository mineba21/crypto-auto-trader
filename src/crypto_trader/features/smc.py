"""Smart-Money-Concept primitives with explicit confirmation timing.

Swing definition (fractal): bar j is a swing high if ``high[j] > max(high[j-left..j-1])`` and
``high[j] >= max(high[j+1..j+right])``. It needs ``right`` future bars, so it becomes *known* only at the close of bar
``j + right``. Every array returned here is indexed by the bar at which the information is available, never by j.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SwingState:
    last_high: np.ndarray  # price of most recent confirmed swing high as of bar i
    last_high_idx: np.ndarray  # bar index j of that swing (-1 if none)
    prev_high: np.ndarray  # the swing high before it
    last_low: np.ndarray
    last_low_idx: np.ndarray
    prev_low: np.ndarray
    new_high_confirmed: np.ndarray  # bool: a swing high got confirmed at bar i
    new_low_confirmed: np.ndarray


def swings(high: np.ndarray, low: np.ndarray, left: int = 3, right: int = 3) -> SwingState:
    n = len(high)
    lh = np.full(n, np.nan)
    lhi = np.full(n, -1, dtype=np.int64)
    ph = np.full(n, np.nan)
    ll = np.full(n, np.nan)
    lli = np.full(n, -1, dtype=np.int64)
    pl = np.full(n, np.nan)
    nh = np.zeros(n, dtype=bool)
    nl = np.zeros(n, dtype=bool)
    cur_h = cur_l = prev_h = prev_l = np.nan
    cur_hi = cur_li = -1
    for i in range(n):
        j = i - right  # candidate whose right window just completed at bar i
        if j - left >= 0:
            if high[j] > high[j - left:j].max() and high[j] >= high[j + 1:i + 1].max():
                prev_h, cur_h, cur_hi = cur_h, high[j], j
                nh[i] = True
            if low[j] < low[j - left:j].min() and low[j] <= low[j + 1:i + 1].min():
                prev_l, cur_l, cur_li = cur_l, low[j], j
                nl[i] = True
        lh[i], lhi[i], ph[i] = cur_h, cur_hi, prev_h
        ll[i], lli[i], pl[i] = cur_l, cur_li, prev_l
    return SwingState(lh, lhi, ph, ll, lli, pl, nh, nl)


def bullish_sweep(low: np.ndarray, close: np.ndarray, sw: SwingState, atr: np.ndarray, min_pierce_atr: float = 0.05,
                  max_pierce_atr: float = 2.0) -> np.ndarray:
    """Bar i trades below the last confirmed swing low (known at i-1) and closes back above it.

    Returns the sweep extreme (low[i]) where a sweep happened, else NaN.
    """
    n = len(low)
    out = np.full(n, np.nan)
    for i in range(1, n):
        lvl, a = sw.last_low[i - 1], atr[i - 1]
        if np.isnan(lvl) or np.isnan(a) or a <= 0:
            continue
        pierce = lvl - low[i]
        if min_pierce_atr * a <= pierce <= max_pierce_atr * a and close[i] > lvl:
            out[i] = low[i]
    return out


def bullish_fvg(high: np.ndarray, low: np.ndarray, atr: np.ndarray, min_gap_atr: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    """3-candle bullish fair value gap completed at bar i: low[i] > high[i-2]. Zone = (high[i-2], low[i])."""
    n = len(high)
    bottom = np.full(n, np.nan)
    top = np.full(n, np.nan)
    for i in range(2, n):
        gap = low[i] - high[i - 2]
        a = atr[i]
        if gap > 0 and not np.isnan(a) and gap >= min_gap_atr * a:
            bottom[i], top[i] = high[i - 2], low[i]
    return bottom, top


def bullish_choch(close: np.ndarray, sw: SwingState) -> np.ndarray:
    """Change of character up: close breaks the last confirmed swing high while structure was making lower highs."""
    n = len(close)
    out = np.zeros(n, dtype=bool)
    for i in range(1, n):
        lvl, prev = sw.last_high[i - 1], sw.prev_high[i - 1]
        if np.isnan(lvl):
            continue
        bearish_structure = np.isnan(prev) or lvl < prev
        if bearish_structure and close[i] > lvl and close[i - 1] <= lvl:
            out[i] = True
    return out


def break_of_structure_up(close: np.ndarray, sw: SwingState) -> np.ndarray:
    n = len(close)
    out = np.zeros(n, dtype=bool)
    for i in range(1, n):
        lvl = sw.last_high[i - 1]
        if not np.isnan(lvl) and close[i] > lvl and close[i - 1] <= lvl:
            out[i] = True
    return out
