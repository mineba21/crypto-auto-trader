"""Upbit public quotation REST API (https://docs.upbit.com/kr/). Private endpoints live in brokers/upbit_live.py."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from crypto_trader.collectors.http import HttpClient
from crypto_trader.core.models import Aggressor, Bar, BookLevel, OrderBookSnapshot, Trade
from crypto_trader.core.timeutils import INTERVAL_MS, now_ms, parse_utc

log = logging.getLogger(__name__)

BASE_URL = "https://api.upbit.com"
_MINUTE_UNITS = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}


def candle_path(interval: str) -> str:
    if interval in _MINUTE_UNITS:
        return f"/v1/candles/minutes/{_MINUTE_UNITS[interval]}"
    if interval == "1d":
        return "/v1/candles/days"
    if interval == "1s":
        return "/v1/candles/seconds"
    raise ValueError(f"unsupported Upbit interval {interval}")


def parse_candle(row: dict, interval: str) -> Bar:
    open_ts = parse_utc(row["candle_date_time_utc"])
    close_ts = open_ts + INTERVAL_MS[interval]
    # A candle is usable only once closed; in-progress candles are dropped by the caller.
    return Bar(
        exchange="upbit",
        symbol=row["market"],
        interval=interval,
        open_ts=open_ts,
        close_ts=close_ts,
        open=float(row["opening_price"]),
        high=float(row["high_price"]),
        low=float(row["low_price"]),
        close=float(row["trade_price"]),
        volume=float(row["candle_acc_trade_volume"]),
        quote_volume=float(row["candle_acc_trade_price"]),
        available_ts=close_ts,
    )


def parse_trade_tick(row: dict, received_ts: int) -> Trade:
    return Trade(
        exchange="upbit",
        symbol=row.get("market") or row.get("code"),
        event_ts=int(row.get("timestamp") or row.get("trade_timestamp")),
        received_ts=received_ts,
        price=float(row["trade_price"]),
        volume=float(row["trade_volume"]),
        aggressor=Aggressor(row["ask_bid"]),
        seq=int(row["sequential_id"]) if row.get("sequential_id") is not None else None,
    )


def parse_orderbook(row: dict, received_ts: int) -> OrderBookSnapshot:
    levels = tuple(
        BookLevel(float(u["bid_price"]), float(u["bid_size"]), float(u["ask_price"]), float(u["ask_size"]))
        for u in row["orderbook_units"]
    )
    return OrderBookSnapshot(
        exchange="upbit",
        symbol=row.get("market") or row.get("code"),
        event_ts=int(row["timestamp"]),
        received_ts=received_ts,
        levels=levels,
    )


def _to_param(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class UpbitRest:
    exchange = "upbit"

    def __init__(self, client: HttpClient | None = None):
        # Quotation API: 10 req/s per IP (Remaining-Req header). Stay below.
        self.http = client or HttpClient(BASE_URL, rate_per_sec=8)

    async def close(self) -> None:
        await self.http.close()

    async def markets(self) -> list[dict]:
        return await self.http.get("/v1/market/all", params={"isDetails": "true"})

    async def krw_markets(self) -> list[str]:
        return [m["market"] for m in await self.markets() if m["market"].startswith("KRW-")]

    async def instruments(self, symbols: list[str]) -> list[dict]:
        return await self.http.get("/v1/orderbook/instruments", params={"markets": ",".join(symbols)})

    async def ticker(self, symbols: list[str]) -> list[dict]:
        return await self.http.get("/v1/ticker", params={"markets": ",".join(symbols)})

    async def orderbook(self, symbols: list[str]) -> list[OrderBookSnapshot]:
        rows = await self.http.get("/v1/orderbook", params={"markets": ",".join(symbols)})
        rts = now_ms()
        return [parse_orderbook(r, rts) for r in rows]

    async def recent_trades(self, symbol: str, count: int = 200, days_ago: int | None = None,
                            cursor: str | None = None) -> list[Trade]:
        params: dict = {"market": symbol, "count": count}
        if days_ago:
            params["days_ago"] = days_ago
        if cursor:
            params["cursor"] = cursor
        rows = await self.http.get("/v1/trades/ticks", params=params)
        rts = now_ms()
        return [parse_trade_tick(r, rts) for r in rows]

    async def candles_page(self, symbol: str, interval: str, to_ms: int | None, count: int = 200) -> list[dict]:
        params: dict = {"market": symbol, "count": count}
        if to_ms is not None:
            params["to"] = _to_param(to_ms)
        return await self.http.get(candle_path(interval), params=params)

    async def candles(self, symbol: str, interval: str, start_ms: int, end_ms: int,
                      max_pages: int = 100_000) -> list[Bar]:
        """Fetch closed candles with open_ts in [start_ms, end_ms). Pages backwards from end_ms.

        Upbit omits candles for periods without trades; gaps are reported by the quality module, not filled.
        """
        out: dict[int, Bar] = {}
        to = end_ms
        rts = now_ms()
        for _ in range(max_pages):
            rows = await self.candles_page(symbol, interval, to)
            if not rows:
                break
            oldest = None
            for r in rows:
                b = parse_candle(r, interval)
                oldest = b.open_ts if oldest is None else min(oldest, b.open_ts)
                if start_ms <= b.open_ts < end_ms and b.close_ts <= rts:
                    out[b.open_ts] = b
            if oldest is None or oldest <= start_ms:
                break
            to = oldest
        return [out[k] for k in sorted(out)]
