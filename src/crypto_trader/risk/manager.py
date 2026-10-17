"""Pre-trade risk checks and position sizing (shared by all modes).

Per-trade risk budget  = min(per_trade_risk_cap_krw, allocation_equity * per_trade_risk_pct) * size_scale
Daily loss limit       = min(daily_loss_cap_krw,  allocation_equity_at_day_start * daily_loss_pct)   (Asia/Seoul day)

Position size solves: qty * (entry - stop) + qty * entry * round_trip_cost <= risk budget, then is capped by
available cash, the exposure limit (incl. open orders) and the trading-capital ceiling. Loss limits are *planned*
limits: gaps, failed fills or exchange outages can exceed them, which is why stops are also sized with stress costs.
No martingale, no averaging down, no leverage: a second entry on an open symbol is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crypto_trader.core.config import Settings
from crypto_trader.core.models import Position, Signal
from crypto_trader.core.timeutils import kst_date
from crypto_trader.risk.kill_switch import KillSwitch


@dataclass
class SizeDecision:
    ok: bool
    krw_amount: float = 0.0
    qty: float = 0.0
    risk_krw: float = 0.0
    reason: str = ""


@dataclass
class RiskState:
    peak_equity: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    consecutive_losses: int = 0
    cum_realized: float = 0.0
    order_times: list[int] = field(default_factory=list)


class RiskManager:
    def __init__(self, settings: Settings, kill_switch: KillSwitch, allocation_krw: float | None = None,
                 round_trip_cost: float = 0.003):
        self.s = settings
        self.r = settings.risk
        self.ks = kill_switch
        self.allocation = float(allocation_krw or settings.strategy_allocation_krw)
        self.rt_cost = round_trip_cost
        self.state = RiskState(peak_equity=self.allocation, day_start_equity=self.allocation)
        if self.allocation > settings.capital.max_trading_capital_krw:
            raise ValueError("allocation exceeds max_trading_capital_krw")

    # ---------------------------------------------------------------- limits
    def per_trade_risk(self, equity: float) -> float:
        return min(self.r.per_trade_risk_cap_krw, equity * self.r.per_trade_risk_pct) * self.size_scale(equity)

    def daily_loss_limit(self) -> float:
        return min(self.r.daily_loss_cap_krw, self.state.day_start_equity * self.r.daily_loss_pct)

    def size_scale(self, equity: float) -> float:
        scale = 1.0
        if self.state.consecutive_losses >= self.r.consecutive_loss_scale_after:
            scale *= self.r.consecutive_loss_scale
        dd = 1 - equity / self.state.peak_equity if self.state.peak_equity > 0 else 0
        if dd > self.r.drawdown_scale_start_pct:
            # linear shrink: at 2x the start drawdown, size is halved, floored at min_size_scale
            scale *= max(self.r.min_size_scale, 1 - (dd - self.r.drawdown_scale_start_pct) / (2 * self.r.drawdown_scale_start_pct))
        return max(self.r.min_size_scale, min(1.0, scale))

    # --------------------------------------------------------------- updates
    def on_equity(self, ts: int, equity: float) -> None:
        d = kst_date(ts).isoformat()
        if d != self.state.day:  # KST midnight: reset only the daily counters
            self.state.day = d
            self.state.day_start_equity = equity
        self.state.peak_equity = max(self.state.peak_equity, equity)
        cum_loss = self.allocation - equity
        if cum_loss >= self.s.capital.emergency_cum_loss_krw:
            self.ks.trigger("CUM_LOSS", f"cumulative loss {cum_loss:,.0f} KRW >= {self.s.capital.emergency_cum_loss_krw:,.0f}")
        if cum_loss >= self.s.capital.max_total_loss_krw:
            self.ks.trigger("TOTAL_LOSS", f"total loss {cum_loss:,.0f} KRW")

    def on_trade_closed(self, pnl_krw: float, by_stop: bool) -> None:
        self.state.cum_realized += pnl_krw
        if pnl_krw < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

    def daily_pnl(self, equity: float) -> float:
        return equity - self.state.day_start_equity

    # ---------------------------------------------------------------- checks
    def check_entry(self, sig: Signal, ts: int, entry_price: float, equity: float, cash: float,
                    positions: dict[str, Position], open_order_krw: float) -> SizeDecision:
        if self.ks.active:
            return SizeDecision(False, reason="kill switch active")
        if sig.stop_price is None or sig.stop_price >= entry_price:
            return SizeDecision(False, reason="no valid stop")
        if sig.symbol in positions and positions[sig.symbol].is_open:
            return SizeDecision(False, reason="already in position (no averaging down)")
        if sum(1 for p in positions.values() if p.is_open) >= self.r.max_positions:
            return SizeDecision(False, reason="max positions")
        if self.daily_pnl(equity) <= -self.daily_loss_limit():
            return SizeDecision(False, reason=f"daily loss limit reached ({self.daily_pnl(equity):,.0f} KRW)")
        recent = [t for t in self.state.order_times if ts - t < 60_000]
        if len(recent) >= self.r.max_orders_per_minute:
            return SizeDecision(False, reason="order rate limit")
        budget = self.per_trade_risk(equity)
        per_unit = (entry_price - sig.stop_price) + entry_price * self.rt_cost
        qty = budget / per_unit
        notional = qty * entry_price
        exposure_now = sum(p.volume * p.avg_price for p in positions.values() if p.is_open) + open_order_krw
        cap_exposure = self.r.max_exposure_pct * equity - exposure_now
        cap_capital = self.s.capital.max_trading_capital_krw - exposure_now
        fee_buf = 1 + self.s.costs.upbit_fee_rate * 2
        notional = min(notional, cash / fee_buf, cap_exposure, cap_capital)
        if notional < self.r.min_order_krw:
            return SizeDecision(False, reason=f"size {notional:,.0f} KRW below exchange minimum")
        qty = notional / entry_price
        return SizeDecision(True, krw_amount=notional, qty=qty, risk_krw=qty * per_unit)

    def record_order(self, ts: int) -> None:
        self.state.order_times = [t for t in self.state.order_times if ts - t < 60_000] + [ts]
        if len(self.state.order_times) > 3 * self.r.max_orders_per_minute:
            self.ks.trigger("ABNORMAL_ORDERS", "order burst detected")


def market_data_guard(now: int, last_tick_ts: int | None, spread_bps: float | None, settings: Settings,
                      derivatives_ts: int | None = None, needs_derivatives: bool = False,
                      clock_skew_ms: int | None = None) -> str | None:
    """Return a reason to block new orders based on live data health, else None."""
    r = settings.risk
    if last_tick_ts is None or now - last_tick_ts > r.market_data_stale_ms:
        return "STALE_MARKET_DATA"
    if spread_bps is not None and spread_bps > r.max_spread_bps:
        return "ABNORMAL_SPREAD"
    if needs_derivatives and (derivatives_ts is None or now - derivatives_ts > r.derivatives_stale_ms):
        return "STALE_DERIVATIVES"
    if clock_skew_ms is not None and abs(clock_skew_ms) > r.max_clock_skew_ms:
        return "CLOCK_SKEW"
    return None
