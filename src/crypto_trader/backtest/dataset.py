"""Assemble research frames: Upbit bars (execution prices) + external derivatives features + order-flow features."""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from crypto_trader.core.timeutils import INTERVAL_MS
from crypto_trader.features.derivatives import add_derivative_features, asof_align, liquidation_bars
from crypto_trader.features.orderflow import orderbook_frame_obi, trades_to_bars
from crypto_trader.storage.parquet_store import ParquetStore


@dataclass
class DataAvailability:
    market: str
    interval: str
    upbit_bars: int = 0
    upbit_first: int | None = None
    upbit_last: int | None = None
    derivatives_first: int | None = None
    derivatives_last: int | None = None
    trades_first: int | None = None
    trades_last: int | None = None
    trade_bars: int = 0
    orderbook_snapshots: int = 0
    notes: list[str] = field(default_factory=list)


def load_upbit_bars(store: ParquetStore, market: str, interval: str) -> pl.DataFrame:
    df = store.read("ohlcv", "upbit", market)
    if df.is_empty():
        return df
    return df.filter(pl.col("interval") == interval).drop("interval").sort("open_ts")


def load_research_frame(store: ParquetStore, market: str, interval: str, futures_symbol: str | None,
                        ) -> tuple[pl.DataFrame, DataAvailability]:
    av = DataAvailability(market, interval)
    bars = load_upbit_bars(store, market, interval)
    if bars.is_empty():
        av.notes.append("no Upbit bars")
        return bars, av
    av.upbit_bars, av.upbit_first, av.upbit_last = bars.height, int(bars["open_ts"].min()), int(bars["open_ts"].max())
    bar_ms = INTERVAL_MS[interval]
    if futures_symbol:
        metrics = store.read("metrics", "binance", futures_symbol)
        funding = store.read("funding", "binance", futures_symbol)
        premium = store.read("premium_index", "binance", futures_symbol)
        fk = store.read("ohlcv", "binance_futures", futures_symbol)
        if not fk.is_empty():
            fk = fk.filter(pl.col("interval") == "1h")
        if not premium.is_empty():
            premium = premium.filter(pl.col("interval") == "1h")
        if metrics.is_empty():
            av.notes.append(f"no Binance metrics for {futures_symbol}")
        else:
            av.derivatives_first, av.derivatives_last = int(metrics["event_ts"].min()), int(metrics["event_ts"].max())
        bars = add_derivative_features(bars, metrics, funding, premium, fk, bar_ms)
        bars = attach_liquidations(store, bars, futures_symbol, bar_ms, av)
    # USDT-adjusted spread vs Binance spot x Upbit KRW-USDT (NOT an FX kimchi premium)
    usdt = load_upbit_bars(store, "KRW-USDT", interval)
    spot = store.read("ohlcv", "binance_spot", futures_symbol) if futures_symbol else pl.DataFrame()
    if not usdt.is_empty() and not spot.is_empty():
        spot = spot.filter(pl.col("interval") == "1h")
        bars = asof_align(bars, usdt.select("available_ts", pl.col("close").alias("krwusdt")), ["krwusdt"], "", 2 * bar_ms)
        bars = asof_align(bars, spot.select("available_ts", pl.col("close").alias("gspot")), ["gspot"], "", 2 * bar_ms)
        bars = bars.with_columns((pl.col("close") / (pl.col("gspot") * pl.col("krwusdt")) - 1).alias("usdt_adjusted_spread"))
    return bars, av


def attach_liquidations(store: ParquetStore, bars: pl.DataFrame, futures_symbol: str, bar_ms: int,
                        av: DataAvailability | None = None) -> pl.DataFrame:
    """Observed liquidations from every captured source (OKX / Binance / Bybit WS) -> long_liq_usd, short_liq_usd."""
    inst = futures_symbol.removesuffix("USDT") + "-USDT-SWAP"
    frames, gaps = [], []
    for ex, sym in (("okx", inst), ("binance", futures_symbol), ("bybit", futures_symbol)):
        df = store.read("liquidations", ex, sym, dedupe=False)
        if not df.is_empty():
            frames.append(df.select("event_ts", "side", pl.col("notional_usd").cast(pl.Float64).fill_null(0.0),
                                    pl.lit(ex).alias("source")))
        g = store.read("gaps", ex, sym)
        if not g.is_empty() and "stream" in g.columns:
            g = g.filter(pl.col("stream") == "liquidations")
            gaps += list(zip(g["event_ts"].to_list(), g["end_ts"].to_list(), strict=True))
    lb = liquidation_bars(pl.concat(frames, how="diagonal_relaxed"), bar_ms, gaps) if frames else pl.DataFrame()
    if lb.is_empty():
        if av is not None:
            av.notes.append("no liquidation coverage yet (live capture only)")
        return bars.with_columns(pl.lit(None, dtype=pl.Float64).alias("long_liq_usd"),
                                 pl.lit(None, dtype=pl.Float64).alias("short_liq_usd"))
    if av is not None:
        av.notes.append(f"liquidations covered {lb.height} bars from {len(frames)} source(s)")
    return asof_align(bars, lb, ["long_liq_usd", "short_liq_usd"], "", max_age_ms=bar_ms)


def load_trade_bars(store: ParquetStore, market: str, interval: str = "5m") -> tuple[pl.DataFrame, DataAvailability]:
    """Order-flow bars from raw Upbit trades (only where trades exist), plus OBI where orderbook snapshots exist."""
    av = DataAvailability(market, interval)
    trades = store.read("trades", "upbit", market)
    if trades.is_empty():
        av.notes.append("no Upbit trades recorded")
        return pl.DataFrame(), av
    trades = trades.unique(subset=["seq"], keep="first") if "seq" in trades.columns else trades
    gaps = store.read("gaps", "upbit", market)
    tb = trades_to_bars(trades, INTERVAL_MS[interval], gaps if not gaps.is_empty() else None)
    av.trades_first, av.trades_last = int(trades["event_ts"].min()), int(trades["event_ts"].max())
    av.trade_bars = tb.height
    ob = store.read("orderbook", "upbit", market)
    av.orderbook_snapshots = ob.height
    if not ob.is_empty():
        obi = orderbook_frame_obi(ob, depth=5)
        tb = asof_align(tb, obi.rename({"valid": "obi_ok"}), ["obi", "spread_bps", "obi_ok"], "", INTERVAL_MS[interval])
        tb = tb.with_columns(pl.col("obi_ok").fill_null(False).cast(pl.Boolean).alias("obi_valid"))
    else:
        av.notes.append("no orderbook snapshots: OBI strategies cannot be backtested")
    return tb, av
