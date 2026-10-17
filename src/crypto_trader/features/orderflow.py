"""Order-flow features from Upbit *spot* trades and orderbook snapshots.

These are computed only where raw trades / book snapshots exist. Windows overlapping a recorded data gap are
flagged ``valid=False`` and must not produce signals. Binance futures taker volume is a different quantity and is
handled in ``features/derivatives.py``.
"""

from __future__ import annotations

import numpy as np
import polars as pl


def trades_to_bars(trades: pl.DataFrame, interval_ms: int, gaps: pl.DataFrame | None = None) -> pl.DataFrame:
    """Aggregate trades (event_ts, price, volume, aggressor) into bars with buy/sell aggressor volume and delta.

    ``available_ts`` = bar close (a bar is only usable after it closes). Bars overlapping a gap get ``valid=False``.
    """
    if trades.is_empty():
        return pl.DataFrame()
    df = trades.sort("event_ts").with_columns(
        (pl.col("event_ts") // interval_ms * interval_ms).alias("open_ts"),
        pl.when(pl.col("aggressor") == "BID").then(pl.col("volume")).otherwise(0.0).alias("buy_vol"),
        pl.when(pl.col("aggressor") == "ASK").then(pl.col("volume")).otherwise(0.0).alias("sell_vol"),
        (pl.col("price") * pl.col("volume")).alias("notional"),
    )
    bars = df.group_by("open_ts", maintain_order=True).agg(
        pl.col("price").first().alias("open"),
        pl.col("price").max().alias("high"),
        pl.col("price").min().alias("low"),
        pl.col("price").last().alias("close"),
        pl.col("volume").sum().alias("volume"),
        pl.col("notional").sum().alias("quote_volume"),
        pl.col("buy_vol").sum(),
        pl.col("sell_vol").sum(),
        pl.len().alias("n_trades"),
    ).sort("open_ts")
    # Re-index to a full grid so that "no trades" bars are explicit (zero volume, carried price).
    grid = pl.DataFrame({"open_ts": np.arange(bars["open_ts"].min(), bars["open_ts"].max() + 1, interval_ms)})
    bars = grid.join(bars, on="open_ts", how="left").with_columns(
        pl.col("close").forward_fill(),
    ).with_columns(
        pl.col("open").fill_null(pl.col("close")),
        pl.col("high").fill_null(pl.col("close")),
        pl.col("low").fill_null(pl.col("close")),
        *[pl.col(c).fill_null(0) for c in ("volume", "quote_volume", "buy_vol", "sell_vol", "n_trades")],
    ).with_columns(
        (pl.col("open_ts") + interval_ms).alias("close_ts"),
        (pl.col("buy_vol") - pl.col("sell_vol")).alias("delta"),
    ).with_columns(
        pl.col("close_ts").alias("available_ts"),
        pl.col("delta").cum_sum().alias("cvd"),
        pl.lit(True).alias("valid"),
    )
    if gaps is not None and not gaps.is_empty():
        g = gaps.filter(pl.col("stream") == "trades") if "stream" in gaps.columns else gaps
        for s, e in zip(g["event_ts"].to_list(), g["end_ts"].to_list(), strict=True):
            bars = bars.with_columns(
                pl.when((pl.col("close_ts") > s) & (pl.col("open_ts") < e)).then(False).otherwise(pl.col("valid")).alias("valid")
            )
    return bars


def rolling_delta(delta: np.ndarray, valid: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Sum of delta over last n bars and whether the whole window is valid."""
    out = np.full(len(delta), np.nan)
    ok = np.zeros(len(delta), dtype=bool)
    c = np.cumsum(np.insert(delta.astype(float), 0, 0.0))
    bad = np.cumsum(np.insert((~valid.astype(bool)).astype(int), 0, 0))
    for i in range(n - 1, len(delta)):
        out[i] = c[i + 1] - c[i + 1 - n]
        ok[i] = bad[i + 1] - bad[i + 1 - n] == 0
    return out, ok


def normalized_delta(delta: np.ndarray, volume: np.ndarray, n: int) -> np.ndarray:
    """Rolling delta / rolling volume in [-1, 1]."""
    d = np.convolve(delta, np.ones(n), mode="full")[: len(delta)]
    v = np.convolve(volume, np.ones(n), mode="full")[: len(volume)]
    out = np.where(v > 0, d / np.where(v > 0, v, 1), 0.0)
    out[: n - 1] = np.nan
    return out


def cvd_bullish_divergence(low: np.ndarray, cvd: np.ndarray, lookback: int) -> np.ndarray:
    """Price makes a new `lookback`-bar low while CVD stays above its own low over the same window."""
    n = len(low)
    out = np.zeros(n, dtype=bool)
    for i in range(lookback, n):
        win_low = low[i - lookback:i].min()
        win_cvd = cvd[i - lookback:i].min()
        if low[i] < win_low and cvd[i] > win_cvd:
            out[i] = True
    return out


def order_book_imbalance(bid_sizes: np.ndarray, ask_sizes: np.ndarray, depth: int,
                         weights: np.ndarray | None = None) -> np.ndarray:
    """OBI = (Bid - Ask) / (Bid + Ask) over the top `depth` levels. Inputs are (n_snapshots, n_levels)."""
    b = bid_sizes[:, :depth].astype(float)
    a = ask_sizes[:, :depth].astype(float)
    if weights is not None:
        w = np.asarray(weights[:depth], dtype=float)
        b, a = b * w, a * w
    bs, as_ = b.sum(axis=1), a.sum(axis=1)
    tot = bs + as_
    return np.where(tot > 0, (bs - as_) / np.where(tot > 0, tot, 1), np.nan)


def orderbook_frame_obi(ob: pl.DataFrame, depth: int = 5, decay: float | None = None) -> pl.DataFrame:
    """OBI per stored snapshot row (columns bs0..bsN / as0..asN). Snapshots out of order are dropped as invalid."""
    if ob.is_empty():
        return pl.DataFrame()
    ob = ob.sort("received_ts")
    levels = min(depth, sum(1 for c in ob.columns if c.startswith("bs")))
    bids = ob.select([f"bs{i}" for i in range(levels)]).to_numpy()
    asks = ob.select([f"as{i}" for i in range(levels)]).to_numpy()
    w = np.array([decay ** i for i in range(levels)]) if decay else None
    obi = order_book_imbalance(bids, asks, levels, w)
    ev = ob["event_ts"].to_numpy()
    in_order = np.concatenate([[True], np.diff(ev) >= 0])
    spread_bps = ((ob["ap0"] - ob["bp0"]) / ((ob["ap0"] + ob["bp0"]) / 2) * 1e4).to_numpy()
    return pl.DataFrame({"event_ts": ev, "available_ts": ob["received_ts"].to_numpy(), "obi": obi,
                         "spread_bps": spread_bps, "valid": in_order & ~np.isnan(obi)})
