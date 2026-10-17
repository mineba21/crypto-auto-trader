"""Bybit V5 public market REST (read-only). https://bybit-exchange.github.io/docs/v5/intro

api.bybit.com is fronted by CloudFront with country blocking (HTTP 403 from e.g. US). ``AccessBlocked`` is raised;
we do not route around it.
"""

from __future__ import annotations

from crypto_trader.collectors.http import ApiError, HttpClient

BASE_URL = "https://api.bybit.com"


class BybitRest:
    exchange = "bybit"

    def __init__(self, client: HttpClient | None = None):
        self.http = client or HttpClient(BASE_URL, rate_per_sec=5)

    async def close(self) -> None:
        await self.http.close()

    async def _get(self, path: str, params: dict) -> dict:
        body = await self.http.get(path, params=params)
        if body.get("retCode") != 0:
            raise ApiError(200, str(body), path)
        return body["result"]

    async def tickers(self, symbol: str) -> dict:
        return await self._get("/v5/market/tickers", {"category": "linear", "symbol": symbol})

    async def klines(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
        # interval: 1 3 5 15 30 60 120 240 360 720 D W M ; results newest-first, max 1000
        res = await self._get("/v5/market/kline", {"category": "linear", "symbol": symbol, "interval": interval,
                                                    "start": start_ms, "end": end_ms, "limit": 1000})
        return res["list"]

    async def open_interest_hist(self, symbol: str, period: str, start_ms: int, end_ms: int) -> list[dict]:
        # intervalTime: 5min 15min 30min 1h 4h 1d ; cursor pagination
        out, cursor = [], None
        while True:
            params = {"category": "linear", "symbol": symbol, "intervalTime": period, "startTime": start_ms,
                      "endTime": end_ms, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            res = await self._get("/v5/market/open-interest", params)
            out += res.get("list", [])
            cursor = res.get("nextPageCursor")
            if not cursor or not res.get("list"):
                return out

    async def funding_history(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        res = await self._get("/v5/market/funding/history", {"category": "linear", "symbol": symbol,
                                                              "startTime": start_ms, "endTime": end_ms, "limit": 200})
        return res["list"]
