"""Common exchange interfaces so Upbit/Bithumb (spot) and Binance/Bybit/OKX (derivatives, read-only) are swappable."""

from __future__ import annotations

from typing import Protocol

from crypto_trader.core.models import Bar, OrderBookSnapshot, Trade


class SpotMarketData(Protocol):
    exchange: str

    async def markets(self) -> list[dict]: ...

    async def candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Bar]: ...

    async def orderbook(self, symbols: list[str]) -> list[OrderBookSnapshot]: ...

    async def recent_trades(self, symbol: str, count: int = 200) -> list[Trade]: ...


class DerivativesMarketData(Protocol):
    """Read-only futures data. Implementations MUST NOT expose order placement."""

    exchange: str

    async def klines(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]: ...

    async def open_interest_hist(self, symbol: str, period: str, start_ms: int, end_ms: int) -> list[dict]: ...

    async def funding_history(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]: ...
