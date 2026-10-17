import numpy as np
import polars as pl

from crypto_trader.features.derivatives import add_derivative_features, asof_align, usdt_adjusted_spread


def test_asof_uses_available_ts_not_event_ts():
    bars = pl.DataFrame({"close_ts": [100, 200, 300]})
    ext = pl.DataFrame({"event_ts": [90, 190], "available_ts": [150, 290], "x": [1.0, 2.0]})
    out = asof_align(bars, ext, ["x"], "d_")
    # bar closing at 100 cannot see the value observed at 90 because it only became available at 150
    assert out["d_x"].to_list() == [None, 1.0, 2.0]


def test_asof_stale_values_are_nulled():
    bars = pl.DataFrame({"close_ts": [100, 10_000]})
    ext = pl.DataFrame({"available_ts": [50], "x": [1.0]})
    assert asof_align(bars, ext, ["x"], "", max_age_ms=1000)["x"].to_list() == [1.0, None]


def test_unsorted_inputs_are_aligned_by_time():
    bars = pl.DataFrame({"close_ts": [300, 100, 200]})
    ext = pl.DataFrame({"available_ts": [250, 50], "x": [2.0, 1.0]})
    out = asof_align(bars, ext, ["x"], "")
    assert out["close_ts"].to_list() == [100, 200, 300] and out["x"].to_list() == [1.0, 1.0, 2.0]


def test_derivative_features_are_causal(bars):
    n = bars.height
    ts = bars["close_ts"].to_numpy()
    rng = np.random.default_rng(3)
    metrics = pl.DataFrame({"event_ts": ts - 300_000, "available_ts": ts, "oi_usd": 1e9 * np.exp(np.cumsum(rng.normal(0, .01, n))),
                            "taker_buy_sell_ratio": rng.uniform(.8, 1.2, n), "global_ls_ratio": rng.uniform(1, 2, n)})
    funding = pl.DataFrame({"event_ts": ts[::8], "available_ts": ts[::8], "funding_rate": rng.normal(1e-4, 5e-5, len(ts[::8]))})
    full = add_derivative_features(bars, metrics, funding, pl.DataFrame(), pl.DataFrame(), 3_600_000)
    k = 2000
    part = add_derivative_features(bars.head(k), metrics, funding, pl.DataFrame(), pl.DataFrame(), 3_600_000)
    for col in ("oi_chg_4h", "oi_chg_24h", "oi_pctile_30d", "funding_z"):
        a, b = full[col].to_numpy()[:k], part[col].to_numpy()
        assert np.allclose(a, b, equal_nan=True), col


def test_usdt_spread_is_relative_price_gap():
    assert abs(usdt_adjusted_spread(np.array([1400.0]), np.array([1.0]), np.array([1350.0]))[0] - (1400 / 1350 - 1)) < 1e-12
