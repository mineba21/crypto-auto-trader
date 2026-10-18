"""Live Binance USD-M derivatives frames for B-family strategies in PAPER/LIVE.

History comes from the official archive already in the store (published with ~1-2 day delay); the most recent
window is filled from the public REST API. Output frames use exactly the archive schema, so
``features.derivatives.add_derivative_features`` is the same code path as in the backtest.

If the REST API is unreachable (e.g. HTTP 451 outside supported regions) the feed reports ``BLOCKED`` and only the
stale archive is returned; the as-of join then nulls stale values and B strategies emit no signals.
"""

from __future__ import annotations

import logging

import polars as pl

from crypto_trader.collectors.binance.futures_rest import BinanceFuturesRest
from crypto_trader.collectors.http import AccessBlocked
from crypto_trader.core.timeutils import now_ms
from crypto_trader.storage.parquet_store import ParquetStore

log = logging.getLogger(__name__)
PERIOD_MS = 300_000  # 5m, same grain as the archive metrics


def _metrics_from_rest(oi: list[dict], taker: list[dict], gls: list[dict]) -> pl.DataFrame:
    if not oi:
        return pl.DataFrame()
    df = pl.DataFrame({"event_ts": [int(r["timestamp"]) for r in oi],
                       "oi_base": [float(r["sumOpenInterest"]) for r in oi],
                       "oi_usd": [float(r["sumOpenInterestValue"]) for r in oi]})
    if taker:
        df = df.join(pl.DataFrame({"event_ts": [int(r["timestamp"]) for r in taker],
                                   "taker_buy_sell_ratio": [float(r["buySellRatio"]) for r in taker]}), on="event_ts", how="left")
    if gls:
        df = df.join(pl.DataFrame({"event_ts": [int(r["timestamp"]) for r in gls],
                                   "global_ls_ratio": [float(r["longShortRatio"]) for r in gls]}), on="event_ts", how="left")
    # a 5m statistic stamped at t is treated as usable only after its period ends (same rule as the archive)
    return df.with_columns((pl.col("event_ts") + PERIOD_MS).alias("available_ts"))


def _klines_from_rest(rows: list[list]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame({
        "open_ts": [int(r[0]) for r in rows], "close_ts": [int(r[6]) + 1 for r in rows],
        "open": [float(r[1]) for r in rows], "high": [float(r[2]) for r in rows], "low": [float(r[3]) for r in rows],
        "close": [float(r[4]) for r in rows], "volume": [float(r[5]) for r in rows],
        "taker_buy_volume": [float(r[9]) for r in rows],
    }).with_columns(pl.col("close_ts").alias("available_ts"))


def _concat(a: pl.DataFrame, b: pl.DataFrame, key: str) -> pl.DataFrame:
    if a.is_empty():
        return b
    if b.is_empty():
        return a
    cols = [c for c in b.columns if c in a.columns]
    return pl.concat([a.select(cols), b.select(cols)], how="diagonal_relaxed").unique(key, keep="last").sort(key)


class BinanceLiveDerivatives:
    def __init__(self, store: ParquetStore | None, symbol: str, rest: BinanceFuturesRest | None = None,
                 history_days: int = 100):
        self.store, self.symbol = store, symbol
        self.rest = rest or BinanceFuturesRest()
        self.history_ms = history_days * 86_400_000
        self.status = "INIT"
        self.last_ok_ms: int | None = None
        self.metrics = self.funding = self.premium = self.fut_klines = pl.DataFrame()

    def _archive(self, kind: str, exchange: str, start: int) -> pl.DataFrame:
        if self.store is None:
            return pl.DataFrame()
        df = self.store.read(kind, exchange, self.symbol, start_ms=start)
        if not df.is_empty() and "interval" in df.columns:
            df = df.filter(pl.col("interval") == "1h").drop("interval")
        return df

    async def refresh(self) -> str:
        start = now_ms() - self.history_ms
        metrics = self._archive("metrics", "binance", start)
        funding = self._archive("funding", "binance", start)
        premium = self._archive("premium_index", "binance", start)
        fk = self._archive("ohlcv", "binance_futures", start)
        try:
            h = self.rest.http
            oi = await h.get("/futures/data/openInterestHist", params={"symbol": self.symbol, "period": "5m", "limit": 500})
            taker = await h.get("/futures/data/takerlongshortRatio", params={"symbol": self.symbol, "period": "5m", "limit": 500})
            gls = await h.get("/futures/data/globalLongShortAccountRatio",
                              params={"symbol": self.symbol, "period": "5m", "limit": 500})
            fr = await h.get("/fapi/v1/fundingRate", params={"symbol": self.symbol, "limit": 100})
            pk = await h.get("/fapi/v1/premiumIndexKlines", params={"symbol": self.symbol, "interval": "1h", "limit": 500})
            kl = await h.get("/fapi/v1/klines", params={"symbol": self.symbol, "interval": "1h", "limit": 500})
            now = now_ms()
            metrics = _concat(metrics, _metrics_from_rest(oi, taker, gls), "event_ts")
            fr_df = pl.DataFrame({"event_ts": [int(r["fundingTime"]) for r in fr],
                                  "available_ts": [int(r["fundingTime"]) for r in fr],
                                  "funding_rate": [float(r["fundingRate"]) for r in fr],
                                  "is_settled": [True] * len(fr)}) if fr else pl.DataFrame()
            funding = _concat(funding, fr_df, "event_ts")
            pkd = _klines_from_rest(pk)
            if not pkd.is_empty():
                pkd = pkd.select(pl.col("open_ts").alias("event_ts"), "close_ts", "available_ts",
                                 pl.col("close").alias("premium_close"))
            premium = _concat(premium, pkd, "event_ts")
            # only closed klines (the last REST row is the running bar)
            fk = _concat(fk, _klines_from_rest(kl).filter(pl.col("close_ts") <= now), "open_ts")
            self.status, self.last_ok_ms = "OK", now
        except AccessBlocked as e:
            self.status = f"BLOCKED (HTTP {e.status})"
        except Exception as e:  # noqa: BLE001
            self.status = f"ERROR {type(e).__name__}"
            log.warning("binance live feed %s: %s", self.symbol, e)
        self.metrics, self.funding, self.premium, self.fut_klines = metrics, funding, premium, fk
        return self.status

    async def close(self) -> None:
        await self.rest.close()
