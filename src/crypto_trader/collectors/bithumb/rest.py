"""Bithumb public REST (v1, Upbit-compatible schema). https://apidocs.bithumb.com/

Extension target: public market data only. Live trading on Bithumb is NOT IMPLEMENTED (Upbit only for now).
"""

from __future__ import annotations

from crypto_trader.collectors.http import HttpClient
from crypto_trader.collectors.upbit.rest import UpbitRest, parse_candle

BASE_URL = "https://api.bithumb.com"


class BithumbRest(UpbitRest):
    exchange = "bithumb"

    def __init__(self, client: HttpClient | None = None):
        super().__init__(client or HttpClient(BASE_URL, rate_per_sec=8))

    async def markets(self) -> list[dict]:
        return await self.http.get("/v1/market/all", params={"isDetails": "false"})

    async def candles(self, symbol, interval, start_ms, end_ms, max_pages=100_000):
        bars = await super().candles(symbol, interval, start_ms, end_ms, max_pages)
        return [b.__class__(**{**{s: getattr(b, s) for s in b.__slots__}, "exchange": "bithumb"}) for b in bars]


__all__ = ["BithumbRest", "parse_candle"]
