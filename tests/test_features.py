import numpy as np
import polars as pl

from crypto_trader.features import smc
from crypto_trader.features.common import atr, ema, rolling_max, rolling_percentile, rolling_zscore, sma
from crypto_trader.features.orderflow import (
    cvd_bullish_divergence,
    normalized_delta,
    order_book_imbalance,
    orderbook_frame_obi,
    rolling_delta,
    trades_to_bars,
)


def test_swing_confirmed_only_after_right_bars():
    high = np.array([1, 2, 3, 10, 3, 2, 1, 1, 1], dtype=float)
    low = high - 0.5
    sw = smc.swings(high, low, left=3, right=3)
    # swing high at index 3 needs bars 4..6 -> first known at index 6
    assert np.isnan(sw.last_high[5]) and sw.last_high[6] == 10 and sw.new_high_confirmed[6]
    assert sw.last_high_idx[6] == 3


def test_indicators_are_causal(bars):
    h, lo, c = (bars[k].to_numpy() for k in ("high", "low", "close"))
    k = 1500
    for fn in (lambda a, b, cc: atr(a, b, cc, 14), lambda a, b, cc: ema(cc, 20), lambda a, b, cc: sma(cc, 50),
               lambda a, b, cc: rolling_max(a, 24), lambda a, b, cc: rolling_zscore(cc, 100),
               lambda a, b, cc: rolling_percentile(cc, 100), lambda a, b, cc: smc.swings(a, b, 3, 3).last_low,
               lambda a, b, cc: smc.bullish_fvg(a, b, atr(a, b, cc), 0.1)[0],
               lambda a, b, cc: smc.bullish_choch(cc, smc.swings(a, b, 3, 3)).astype(float),
               lambda a, b, cc: smc.bullish_sweep(b, cc, smc.swings(a, b, 3, 3), atr(a, b, cc))):
        full, part = fn(h, lo, c), fn(h[:k], lo[:k], c[:k])
        assert np.allclose(full[:k], part, equal_nan=True)


def test_sweep_detection():
    high = np.array([10, 9, 8, 7, 8, 9, 10, 9.5, 9.4, 9.3, 8.9], dtype=float)
    low = np.array([9, 8, 7, 5, 7, 8, 9, 8.5, 8.4, 8.3, 4.8], dtype=float)
    close = np.array([9.5, 8.5, 7.5, 6, 7.5, 8.5, 9.5, 9, 9, 9, 6.0])
    sw = smc.swings(high, low, 3, 3)
    a = np.full(len(high), 1.0)
    s = smc.bullish_sweep(low, close, sw, a, 0.05, 2.0)
    assert sw.last_low[9] == 5 and s[10] == 4.8  # pierced 5 by 0.2 and closed back above


def test_cvd_and_deltas():
    trades = pl.DataFrame({"event_ts": [0, 10, 70_000, 130_000], "price": [1.0, 1.1, 1.2, 1.0],
                           "volume": [2.0, 1.0, 3.0, 1.0], "aggressor": ["BID", "ASK", "BID", "ASK"]})
    b = trades_to_bars(trades, 60_000)
    assert b["delta"].to_list() == [1.0, 3.0, -1.0]
    assert b["cvd"].to_list() == [1.0, 4.0, 3.0]
    assert b["available_ts"].to_list() == [60_000, 120_000, 180_000]
    rd, ok = rolling_delta(b["delta"].to_numpy(), b["valid"].to_numpy(), 2)
    assert rd[1] == 4.0 and rd[2] == 2.0 and ok[1]
    nd = normalized_delta(b["delta"].to_numpy(), b["volume"].to_numpy(), 2)
    assert abs(nd[1] - 4 / 6) < 1e-12


def test_gap_invalidates_flow_bars():
    trades = pl.DataFrame({"event_ts": [0, 70_000, 130_000], "price": [1.0] * 3, "volume": [1.0] * 3,
                           "aggressor": ["BID"] * 3})
    gaps = pl.DataFrame({"event_ts": [65_000], "end_ts": [100_000], "stream": ["trades"]})
    b = trades_to_bars(trades, 60_000, gaps)
    assert b["valid"].to_list() == [True, False, True]


def test_cvd_divergence():
    low = np.array([10, 9, 8, 7, 6.5])
    cvd = np.array([0, -5, -8, -6, -4.0])
    assert cvd_bullish_divergence(low, cvd, 3)[4]


def test_obi_formula_depth_and_weights():
    bids = np.array([[3.0, 1.0, 100.0]])
    asks = np.array([[1.0, 1.0, 0.0]])
    assert abs(order_book_imbalance(bids, asks, 2)[0] - (4 - 2) / 6) < 1e-12
    w = np.array([1.0, 0.5, 0.25])
    assert abs(order_book_imbalance(bids, asks, 2, w)[0] - (3.5 - 1.5) / 5) < 1e-12


def test_orderbook_out_of_order_flagged():
    ob = pl.DataFrame({"event_ts": [2, 1], "received_ts": [10, 11], "bp0": [99.0, 99.0], "bs0": [1.0, 1.0],
                       "ap0": [101.0, 101.0], "as0": [1.0, 2.0]})
    o = orderbook_frame_obi(ob, depth=1)
    assert o["valid"].to_list() == [True, False]
