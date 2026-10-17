"""BacktestBroker: fills queued orders against historical bars.

* Market orders submitted at a bar close fill at the NEXT bar's open (the earliest executable time).
* Slippage per side = cost_scenario/2 - fee (so fee + slippage of a round trip equals the scenario cost).
* Partial fills: a fill is capped at ``max_participation`` x bar quote volume; the rest of a market order is cancelled.
* Client-side stop/target exits fill at the trigger price (or the open if the bar gapped through), minus slippage.
"""

from __future__ import annotations

from crypto_trader.brokers.sim import FillQuote, SimBroker
from crypto_trader.core.models import Bar, Mode, Order, OrderType, Side


class BacktestBroker(SimBroker):
    def __init__(self, cash_krw: float, fee_rate: float, slippage_rate: float, max_participation: float = 0.05,
                 min_order_krw: float = 5_000):
        super().__init__(Mode.BACKTEST, cash_krw, fee_rate, min_order_krw)
        self.slip = slippage_rate
        self.max_participation = max_participation
        self.rejections: list[str] = []

    def _quote(self, o: Order, ref_price: float, bar: Bar) -> FillQuote:
        r = o.request
        cap_krw = self.max_participation * bar.quote_volume if bar.quote_volume > 0 else 0.0
        if r.side == Side.BUY:
            px = ref_price * (1 + self.slip)
            budget = (o.locked_krw / (1 + self.fee_rate)) if r.order_type == OrderType.MARKET_BUY else px * float(r.volume or 0)
            krw = min(budget, cap_krw)
            vol = krw / px
        else:
            px = ref_price * (1 - self.slip)
            vol = min(float(r.volume or 0) - o.filled_volume, cap_krw / px if px > 0 else 0.0)
        return FillQuote(px, max(0.0, vol), abs(px - ref_price) * max(0.0, vol))

    def fill_at_open(self, bar: Bar) -> None:
        self.now = bar.open_ts
        for o in list(self.open_orders()):
            if o.request.symbol != bar.symbol or o.request.order_type == OrderType.LIMIT:
                continue
            self._execute(o, self._quote(o, bar.open, bar), bar.open_ts)
            if o.state.name == "REJECTED":
                self.rejections.append(o.reject_reason)

    def fill_now(self, o: Order, ref_price: float, bar: Bar, ts: int) -> None:
        """Immediate execution (client-side stop / take-profit market sell during the bar)."""
        self.now = ts
        if o.state.is_active:
            self._execute(o, self._quote(o, ref_price, bar), ts)

    def fill_limits(self, bar: Bar) -> None:
        for o in list(self.open_orders()):
            r = o.request
            if r.symbol != bar.symbol or r.order_type != OrderType.LIMIT:
                continue
            if (r.side == Side.BUY and bar.low < r.price) or (r.side == Side.SELL and bar.high > r.price):
                # strict cross required (touch is not a guaranteed fill); no extra slippage on a limit price
                cap = self.max_participation * bar.quote_volume / r.price
                vol = min(float(r.volume) - o.filled_volume, cap)
                self._execute(o, FillQuote(float(r.price), vol, 0.0), bar.close_ts)
