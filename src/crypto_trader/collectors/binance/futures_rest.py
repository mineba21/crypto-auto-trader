"""Binance USD-M futures public REST (read-only). https://developers.binance.com/docs/derivatives/usds-margined-futures

From some regions this API answers HTTP 451 ("restricted location"); the client raises ``AccessBlocked`` and the
system falls back to the official archive (data.binance.vision) and to other providers. No bypass is attempted.
"""

from __future__ import annotations

from crypto_trader.collectors.http import HttpClient

BASE_URL = "https://fapi.binance.com"


class BinanceFuturesRest:
    exchange = "binance"

    def __init__(self, client: HttpClient | None = None):
        # IP weight limit 2400/min; we stay far below.
        self.http = client or HttpClient(BASE_URL, rate_per_sec=5)

    async def close(self) -> None:
        await self.http.close()

    async def price(self, symbol: str) -> dict:
        return await self.http.get("/fapi/v1/ticker/price", params={"symbol": symbol})

    async def premium_index(self, symbol: str) -> dict:
        """markPrice, indexPrice, lastFundingRate (an *estimate* for the running period), nextFundingTime."""
        return await self.http.get("/fapi/v1/premiumIndex", params={"symbol": symbol})

    async def open_interest(self, symbol: str) -> dict:
        return await self.http.get("/fapi/v1/openInterest", params={"symbol": symbol})

    async def open_interest_hist(self, symbol: str, period: str, start_ms: int, end_ms: int) -> list[dict]:
        # Only the most recent 30 days are served by this endpoint.
        out, cur = [], start_ms
        while cur < end_ms:
            rows = await self.http.get("/futures/data/openInterestHist",
                                       params={"symbol": symbol, "period": period, "startTime": cur, "endTime": end_ms, "limit": 500})
            if not rows:
                break
            out += rows
            nxt = int(rows[-1]["timestamp"]) + 1
            if nxt <= cur:
                break
            cur = nxt
        return out

    async def taker_volume(self, symbol: str, period: str, limit: int = 500) -> list[dict]:
        return await self.http.get("/futures/data/takerlongshortRatio", params={"symbol": symbol, "period": period, "limit": limit})

    async def funding_history(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        out, cur = [], start_ms
        while cur < end_ms:
            rows = await self.http.get("/fapi/v1/fundingRate",
                                       params={"symbol": symbol, "startTime": cur, "endTime": end_ms, "limit": 1000})
            if not rows:
                break
            out += rows
            cur = int(rows[-1]["fundingTime"]) + 1
        return out

    async def klines(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
        out, cur = [], start_ms
        while cur < end_ms:
            rows = await self.http.get("/fapi/v1/klines", params={"symbol": symbol, "interval": interval,
                                                                   "startTime": cur, "endTime": end_ms, "limit": 1500})
            if not rows:
                break
            out += rows
            cur = int(rows[-1][6]) + 1
        return out
