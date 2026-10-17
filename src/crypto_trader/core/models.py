"""Shared domain models used by collectors, strategies, backtest and brokers.

All timestamps are integer milliseconds since epoch, UTC.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field


class Mode(enum.StrEnum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Side(enum.StrEnum):
    BUY = "bid"  # Upbit naming
    SELL = "ask"


class OrderType(enum.StrEnum):
    LIMIT = "limit"
    MARKET_BUY = "price"  # Upbit: market buy by total KRW amount
    MARKET_SELL = "market"  # Upbit: market sell by volume
    BEST = "best"


class OrderState(enum.StrEnum):
    CREATED = "created"  # locally created, not yet sent
    SUBMITTING = "submitting"  # request in flight
    UNKNOWN = "unknown"  # send result unclear (timeout etc.) -> must reconcile
    OPEN = "wait"  # accepted by exchange, resting
    WATCH = "watch"
    PARTIAL = "partial"
    FILLED = "done"
    CANCELLED = "cancel"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED)

    @property
    def is_active(self) -> bool:
        return not self.is_terminal


class Aggressor(enum.StrEnum):
    BUY = "BID"  # Upbit ask_bid=BID means a taker buy hit the ask
    SELL = "ASK"


@dataclass(slots=True, frozen=True)
class Bar:
    exchange: str
    symbol: str
    interval: str
    open_ts: int  # bar start (event time)
    close_ts: int  # bar end exclusive; bar is usable at available_ts >= close_ts
    open: float
    high: float
    low: float
    close: float
    volume: float  # base volume
    quote_volume: float
    available_ts: int  # earliest time a strategy may consume this bar

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0


@dataclass(slots=True, frozen=True)
class Trade:
    exchange: str
    symbol: str
    event_ts: int
    received_ts: int
    price: float
    volume: float
    aggressor: Aggressor
    seq: int | None = None


@dataclass(slots=True, frozen=True)
class BookLevel:
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float


@dataclass(slots=True, frozen=True)
class OrderBookSnapshot:
    exchange: str
    symbol: str
    event_ts: int
    received_ts: int
    levels: tuple[BookLevel, ...]

    @property
    def best_bid(self) -> float:
        return self.levels[0].bid_price

    @property
    def best_ask(self) -> float:
        return self.levels[0].ask_price

    @property
    def spread_bps(self) -> float:
        mid = (self.best_bid + self.best_ask) / 2
        return (self.best_ask - self.best_bid) / mid * 1e4 if mid > 0 else float("inf")


def new_client_order_id(prefix: str = "cat") -> str:
    # Upbit identifier: <= 64 chars, must never be reused.
    return f"{prefix}-{uuid.uuid4().hex}"


@dataclass(slots=True)
class OrderRequest:
    symbol: str
    side: Side
    order_type: OrderType
    strategy: str
    volume: float | None = None  # base qty (limit / market sell)
    price: float | None = None  # limit price, or KRW amount for MARKET_BUY
    client_order_id: str = field(default_factory=new_client_order_id)
    reason: str = ""
    stop_price: float | None = None  # client-side stop, managed by our engine (Upbit has no server stop)
    take_profit: float | None = None
    created_ts: int = 0

    def notional_krw(self, ref_price: float) -> float:
        if self.order_type == OrderType.MARKET_BUY:
            return float(self.price or 0.0)
        px = self.price if self.price is not None else ref_price
        return float(self.volume or 0.0) * float(px)


@dataclass(slots=True)
class Fill:
    client_order_id: str
    symbol: str
    side: Side
    ts: int
    price: float
    volume: float
    fee: float  # KRW
    slippage_krw: float = 0.0
    trade_id: str = ""


@dataclass(slots=True)
class Order:
    request: OrderRequest
    state: OrderState = OrderState.CREATED
    exchange_order_id: str | None = None
    filled_volume: float = 0.0
    avg_price: float = 0.0
    paid_fee: float = 0.0
    locked_krw: float = 0.0
    reject_reason: str = ""
    updated_ts: int = 0
    fills: list[Fill] = field(default_factory=list)

    @property
    def client_order_id(self) -> str:
        return self.request.client_order_id

    def apply_fill(self, fill: Fill) -> None:
        total = self.filled_volume + fill.volume
        if total > 0:
            self.avg_price = (self.avg_price * self.filled_volume + fill.price * fill.volume) / total
        self.filled_volume = total
        self.paid_fee += fill.fee
        self.fills.append(fill)
        self.updated_ts = fill.ts


@dataclass(slots=True)
class Position:
    symbol: str
    strategy: str
    volume: float = 0.0
    avg_price: float = 0.0
    entry_ts: int = 0
    stop_price: float | None = None
    take_profit: float | None = None
    entry_fee: float = 0.0
    initial_risk_krw: float = 0.0  # 1R in KRW at entry
    mae: float = 0.0  # max adverse excursion (price fraction)
    mfe: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.volume > 1e-12


@dataclass(slots=True, frozen=True)
class Signal:
    """Strategy output. Long-only: ENTER opens/extends a long, EXIT closes, BLOCK vetoes new buys."""

    ts: int
    symbol: str
    strategy: str
    action: str  # "ENTER" | "EXIT" | "BLOCK"
    stop_price: float | None = None
    take_profit: float | None = None
    reason: str = ""
    score: float = 1.0
    meta: dict | None = None
