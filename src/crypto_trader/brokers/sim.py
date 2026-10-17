"""Simulated exchange account + matching, shared by BacktestBroker and PaperBroker.

Rules mirrored from Upbit KRW spot: min order 5,000 KRW, buy fee charged in KRW on top, sell fee deducted from
proceeds, market buy specifies KRW amount, market sell specifies volume, no shorting (sell <= free volume).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from crypto_trader.brokers.base import Balance, Broker
from crypto_trader.core.models import Fill, Mode, Order, OrderRequest, OrderState, OrderType, Side

log = logging.getLogger(__name__)


@dataclass
class FillQuote:
    price: float  # average fill price
    volume: float  # base volume fillable
    slippage_krw: float  # cost vs reference price, KRW


class SimBroker(Broker):
    def __init__(self, mode: Mode, cash_krw: float, fee_rate: float, min_order_krw: float = 5_000):
        self.mode = mode
        self.fee_rate = fee_rate
        self.min_order = min_order_krw
        self.cash = float(cash_krw)
        self.locked_cash = 0.0
        self.coins: dict[str, float] = {}
        self.locked_coins: dict[str, float] = {}
        self.orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self.now = 0
        self.total_fees = 0.0
        self.total_slippage = 0.0

    # ------------------------------------------------------------------ API
    def submit(self, req: OrderRequest) -> Order:
        if req.client_order_id in self.orders:
            raise ValueError(f"duplicate client_order_id {req.client_order_id}")
        o = Order(request=req, state=OrderState.OPEN, updated_ts=self.now, exchange_order_id=f"sim-{len(self.orders)}")
        reason = self._validate_and_lock(o)
        if reason:
            o.state, o.reject_reason = OrderState.REJECTED, reason
        self.orders[req.client_order_id] = o
        return o

    def cancel(self, client_order_id: str) -> Order:
        o = self.orders[client_order_id]
        if o.state.is_active:
            self._unlock(o)
            o.state = OrderState.CANCELLED
            o.updated_ts = self.now
        return o

    def get_order(self, client_order_id: str) -> Order | None:
        return self.orders.get(client_order_id)

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.state in (OrderState.OPEN, OrderState.PARTIAL)]

    def balances(self) -> dict[str, Balance]:
        out = {"KRW": Balance("KRW", self.cash, self.locked_cash)}
        for sym, v in self.coins.items():
            cur = sym.split("-", 1)[1]
            out[cur] = Balance(cur, v, self.locked_coins.get(sym, 0.0))
        return out

    def drain_fills(self) -> list[Fill]:
        f, self._fills = self._fills, []
        return f

    def open_order_krw(self) -> float:
        return self.locked_cash

    # -------------------------------------------------------------- internals
    def _validate_and_lock(self, o: Order) -> str:
        r = o.request
        if r.side == Side.BUY:
            if r.order_type == OrderType.MARKET_BUY:
                amount = float(r.price or 0)
            elif r.order_type == OrderType.LIMIT:
                amount = float(r.price or 0) * float(r.volume or 0)
            else:
                return "unsupported buy order type"
            if amount < self.min_order:
                return f"under_min_total_bid: {amount:,.0f} < {self.min_order:,.0f}"
            need = amount * (1 + self.fee_rate)
            if need > self.cash + 1e-6:
                return "insufficient_funds_bid"
            self.cash -= need
            self.locked_cash += need
            o.locked_krw = need
            return ""
        # sell
        if r.order_type not in (OrderType.MARKET_SELL, OrderType.LIMIT):
            return "unsupported sell order type"
        vol = float(r.volume or 0)
        free = self.coins.get(r.symbol, 0.0)
        if vol <= 0 or vol > free + 1e-12:
            return "insufficient_funds_ask"  # long-only: can never sell more than we hold
        self.coins[r.symbol] = free - vol
        self.locked_coins[r.symbol] = self.locked_coins.get(r.symbol, 0.0) + vol
        return ""

    def _unlock(self, o: Order) -> None:
        r = o.request
        if r.side == Side.BUY:
            self.cash += o.locked_krw
            self.locked_cash -= o.locked_krw
            o.locked_krw = 0.0
        else:
            rem = float(r.volume or 0) - o.filled_volume
            self.locked_coins[r.symbol] = self.locked_coins.get(r.symbol, 0.0) - rem
            self.coins[r.symbol] = self.coins.get(r.symbol, 0.0) + rem

    def _execute(self, o: Order, quote: FillQuote, ts: int, trade_id: str = "") -> Fill | None:
        """Apply a (possibly partial) fill. Remainder of a market order is cancelled (IOC-like)."""
        r = o.request
        if quote.volume <= 0:
            if r.order_type != OrderType.LIMIT:
                self._unlock(o)
                o.state = OrderState.CANCELLED if o.filled_volume > 0 else OrderState.REJECTED
                o.reject_reason = o.reject_reason or "no liquidity"
            return None
        notional = quote.price * quote.volume
        fee = notional * self.fee_rate
        fill = Fill(r.client_order_id, r.symbol, r.side, ts, quote.price, quote.volume, fee, quote.slippage_krw,
                    trade_id or f"{r.client_order_id}-{len(o.fills)}")
        if r.side == Side.BUY:
            cost = notional + fee
            o.locked_krw -= cost
            self.locked_cash -= cost
            self.coins[r.symbol] = self.coins.get(r.symbol, 0.0) + quote.volume
        else:
            self.locked_coins[r.symbol] = self.locked_coins.get(r.symbol, 0.0) - quote.volume
            self.cash += notional - fee
        o.apply_fill(fill)
        self.total_fees += fee
        self.total_slippage += quote.slippage_krw
        target = float(r.volume or 0) if r.side == Side.SELL or r.order_type == OrderType.LIMIT else None
        complete = (target is not None and o.filled_volume >= target - 1e-12) or (
            r.order_type == OrderType.MARKET_BUY and o.locked_krw <= float(r.price or 0) * 1e-9 + 1e-6)
        if complete:
            o.state = OrderState.FILLED
            if o.locked_krw > 0:  # rounding leftovers
                self._unlock(o)
        elif r.order_type == OrderType.LIMIT:
            o.state = OrderState.PARTIAL
        else:
            # market order partially filled: rest is cancelled, unused funds released
            self._unlock(o)
            o.state = OrderState.CANCELLED
            o.reject_reason = "partially filled; remainder cancelled"
        o.updated_ts = ts
        self._fills.append(fill)
        return fill
