"""PaperBroker: virtual orders matched against the LIVE Upbit orderbook / trades. Never calls a private API.

Market orders walk the latest orderbook snapshot level by level (so the spread and depth are already in the price,
and no extra slippage is added). If the snapshot is stale or missing the order is rejected rather than guessed.
"""

from __future__ import annotations

from crypto_trader.brokers.sim import FillQuote, SimBroker
from crypto_trader.core.models import Mode, Order, OrderBookSnapshot, OrderRequest, OrderType, Side, Trade
from crypto_trader.core.timeutils import now_ms


def walk_book(ob: OrderBookSnapshot, side: Side, krw_amount: float | None = None, volume: float | None = None) -> FillQuote:
    filled_vol = spent = 0.0
    ref = ob.best_ask if side == Side.BUY else ob.best_bid
    for lv in ob.levels:
        px, sz = (lv.ask_price, lv.ask_size) if side == Side.BUY else (lv.bid_price, lv.bid_size)
        if krw_amount is not None:
            take = min(sz, (krw_amount - spent) / px)
        else:
            take = min(sz, float(volume) - filled_vol)
        if take <= 0:
            break
        filled_vol += take
        spent += take * px
    avg = spent / filled_vol if filled_vol > 0 else ref
    mid = (ob.best_bid + ob.best_ask) / 2
    return FillQuote(avg, filled_vol, abs(avg - mid) * filled_vol)


class PaperBroker(SimBroker):
    def __init__(self, cash_krw: float, fee_rate: float, max_book_age_ms: int = 5_000, min_order_krw: float = 5_000):
        super().__init__(Mode.PAPER, cash_krw, fee_rate, min_order_krw)
        self.books: dict[str, OrderBookSnapshot] = {}
        self.max_book_age_ms = max_book_age_ms

    def on_orderbook(self, ob: OrderBookSnapshot) -> None:
        self.books[ob.symbol] = ob

    def submit(self, req: OrderRequest) -> Order:
        self.now = now_ms()
        o = super().submit(req)
        if o.state.is_active and req.order_type != OrderType.LIMIT:
            self._match_market(o)
        return o

    def _match_market(self, o: Order) -> None:
        r = o.request
        ob = self.books.get(r.symbol)
        if ob is None or self.now - ob.received_ts > self.max_book_age_ms:
            self._unlock(o)
            o.state, o.reject_reason = o.state.REJECTED, "paper: no fresh orderbook"
            return
        if r.side == Side.BUY:
            q = walk_book(ob, Side.BUY, krw_amount=o.locked_krw / (1 + self.fee_rate))
        else:
            q = walk_book(ob, Side.SELL, volume=float(r.volume))
        self._execute(o, q, self.now)

    def on_trade(self, t: Trade) -> None:
        """Resting limit orders fill when the tape trades through their price."""
        self.now = t.received_ts
        for o in list(self.open_orders()):
            r = o.request
            if r.symbol != t.symbol or r.order_type != OrderType.LIMIT:
                continue
            if (r.side == Side.BUY and t.price < r.price) or (r.side == Side.SELL and t.price > r.price):
                vol = min(float(r.volume) - o.filled_volume, t.volume)
                self._execute(o, FillQuote(float(r.price), vol, 0.0), t.received_ts, trade_id=f"{t.seq}")
