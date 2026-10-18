"""External futures features, aligned to Upbit bars by *availability time* (as-of backward join).

Rule: a derivatives value may be attached to an Upbit bar decided at ``decision_ts = bar.close_ts`` only if the
value's ``available_ts <= decision_ts``. Settled funding is only known at settlement; the running estimate is a
different series (``is_settled=False``) and is never back-filled into history.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from crypto_trader.features.common import rolling_percentile, rolling_zscore


def asof_align(bars: pl.DataFrame, ext: pl.DataFrame, cols: list[str], prefix: str,
               max_age_ms: int | None = None) -> pl.DataFrame:
    """Attach the latest ``ext`` row with available_ts <= bar.close_ts. Optionally null-out stale values."""
    if ext.is_empty():
        return bars.with_columns([pl.lit(None, dtype=pl.Float64).alias(f"{prefix}{c}") for c in cols])
    right = ext.select(pl.col("available_ts").alias("__avail"), *[pl.col(c).alias(f"{prefix}{c}") for c in cols]).sort("__avail")
    left = bars.sort("close_ts")
    out = left.join_asof(right, left_on="close_ts", right_on="__avail", strategy="backward")
    if max_age_ms is not None:
        stale = (pl.col("close_ts") - pl.col("__avail")) > max_age_ms
        out = out.with_columns([pl.when(stale).then(None).otherwise(pl.col(f"{prefix}{c}")).alias(f"{prefix}{c}") for c in cols])
    return out.drop("__avail")


def add_derivative_features(bars: pl.DataFrame, metrics: pl.DataFrame, funding: pl.DataFrame,
                            premium: pl.DataFrame, fut_klines: pl.DataFrame, bar_ms: int) -> pl.DataFrame:
    """Return bars + OI change / percentile, funding z-score, premium, futures taker imbalance.

    Inputs come from Binance archive (metrics: oi_usd, taker_buy_sell_ratio; funding: funding_rate;
    premium: premium_close; fut_klines: taker_buy_volume/volume).
    """
    out = asof_align(bars, metrics, ["oi_usd", "taker_buy_sell_ratio", "global_ls_ratio"], "d_", max_age_ms=2 * 3_600_000)
    out = asof_align(out, funding, ["funding_rate"], "d_", max_age_ms=9 * 3_600_000)
    if not premium.is_empty():
        out = asof_align(out, premium, ["premium_close"], "d_", max_age_ms=2 * bar_ms)
    else:
        out = out.with_columns(pl.lit(None, dtype=pl.Float64).alias("d_premium_close"))
    if not fut_klines.is_empty():
        fk = fut_klines.with_columns((pl.col("taker_buy_volume") / pl.col("volume")).alias("taker_buy_frac"))
        out = asof_align(out, fk, ["taker_buy_frac", "close"], "fut_", max_age_ms=2 * bar_ms)
    else:
        out = out.with_columns(pl.lit(None, dtype=pl.Float64).alias("fut_taker_buy_frac"),
                               pl.lit(None, dtype=pl.Float64).alias("fut_close"))
    per_day = max(1, 86_400_000 // bar_ms)
    oi = out["d_oi_usd"].to_numpy().astype(float)
    fr = out["d_funding_rate"].to_numpy().astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        oi_chg_4h = oi / np.concatenate([np.full(max(1, 4 * 3_600_000 // bar_ms), np.nan), oi])[: len(oi)] - 1
        oi_chg_24h = oi / np.concatenate([np.full(per_day, np.nan), oi])[: len(oi)] - 1
    return out.with_columns(
        pl.Series("oi_chg_4h", oi_chg_4h),
        pl.Series("oi_chg_24h", oi_chg_24h),
        pl.Series("oi_pctile_30d", rolling_percentile(oi, 30 * per_day, min_periods=7 * per_day)),
        pl.Series("oi_chg_24h_pctile", rolling_percentile(oi_chg_24h, 90 * per_day, min_periods=14 * per_day)),
        pl.Series("funding_z", rolling_zscore(fr, 30 * per_day, min_periods=7 * per_day)),
        pl.Series("premium_z", rolling_zscore(out["d_premium_close"].to_numpy().astype(float), 30 * per_day,
                                              min_periods=7 * per_day)),
    )


def usdt_adjusted_spread(krw_price: np.ndarray, usdt_price_global: np.ndarray, krw_usdt: np.ndarray) -> np.ndarray:
    """KRW price vs (global USDT price x Upbit KRW-USDT). Not an FX kimchi premium (KRW-USDT != USD/KRW)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        return krw_price / (usdt_price_global * krw_usdt) - 1.0


def fx_adjusted_premium(krw_price: np.ndarray, usd_price_global: np.ndarray, usdkrw: np.ndarray) -> np.ndarray:
    """True kimchi premium; requires a real USD/KRW FX series (not available by default -> do not label otherwise)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        return krw_price / (usd_price_global * usdkrw) - 1.0


def futures_basis(fut_price: np.ndarray, spot_price: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        return fut_price / spot_price - 1.0


def liquidation_bars(liq: pl.DataFrame, bar_ms: int, gaps: list[tuple[int, int]] | None = None) -> pl.DataFrame:
    """Hourly (bar_ms) observed liquidation notional per side, usable only after the bucket closes.

    ``liq`` holds rows from one or more sources (columns: event_ts, side, notional_usd, source). Coverage is the
    window every present source was recording (latest first event .. earliest last event); buckets outside it, or
    overlapping a recorded stream gap, are null (unknown) — never zero. Totals are lower bounds (throttled feeds).
    """
    if liq.is_empty():
        return pl.DataFrame()
    cov = liq.group_by("source").agg(pl.col("event_ts").min().alias("lo"), pl.col("event_ts").max().alias("hi"))
    start = int(cov["lo"].max()) // bar_ms * bar_ms + bar_ms  # first full bucket after every source is live
    end = int(cov["hi"].min()) // bar_ms * bar_ms  # last full bucket before any source stopped
    if end <= start:
        return pl.DataFrame()
    grid = pl.DataFrame({"event_ts": np.arange(start, end, bar_ms, dtype=np.int64)})
    agg = (liq.filter((pl.col("event_ts") >= start) & (pl.col("event_ts") < end))
           .with_columns((pl.col("event_ts") // bar_ms * bar_ms).alias("bucket"))
           .group_by("bucket").agg(
               pl.col("notional_usd").filter(pl.col("side") == "long_liq").sum().alias("long_liq_usd"),
               pl.col("notional_usd").filter(pl.col("side") == "short_liq").sum().alias("short_liq_usd")))
    out = grid.join(agg, left_on="event_ts", right_on="bucket", how="left").with_columns(
        pl.col("long_liq_usd").fill_null(0.0), pl.col("short_liq_usd").fill_null(0.0),
        (pl.col("event_ts") + bar_ms).alias("available_ts"))
    for s, e in gaps or []:
        hit = (pl.col("event_ts") < e) & (pl.col("event_ts") + bar_ms > s)
        out = out.with_columns([pl.when(hit).then(None).otherwise(pl.col(c)).alias(c) for c in ("long_liq_usd", "short_liq_usd")])
    return out.sort("event_ts")
