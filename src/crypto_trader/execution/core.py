"""TradingCore: the one decision/execution path shared by BACKTEST, PAPER and LIVE.

Per closed bar:  strategy.on_bar -> risk checks/sizing -> broker.submit -> ledger update from fills.
Per price update (bar high/low in backtest, trade ticks live): client-side stop / take-profit monitoring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from crypto_trader.brokers.base import Broker, OrderRejected
from crypto_trader.core.models import Fill, Order, OrderRequest, OrderType, Position, Side, Signal
from crypto_trader.risk.manager import RiskManager
from crypto_trader.storage.ops_db import OpsDB
from crypto_trader.strategies.base import Features, Strategy

log = logging.getLogger(__name__)


@dataclass
class ClosedTrade:
    symbol: str
    strategy: str
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    volume: float
    pnl_krw: float  # net of fees and slippage
    fees_krw: float
    slippage_krw: float
    r_multiple: float
    mae: float
    mfe: float
    exit_reason: str
    entry_reason: str
    bars_held: int


@dataclass
class Ledger:
    positions: dict[str, Position] = field(default_factory=dict)
    realized: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    trades: list[ClosedTrade] = field(default_factory=list)
    entry_bar: dict[str, int] = field(default_factory=dict)
    entry_reason: dict[str, str] = field(default_factory=dict)
    entry_slip: dict[str, float] = field(default_factory=dict)
    partial: dict[str, dict] = field(default_factory=dict)  # realised pieces of a position being closed in parts


class TradingCore:
    def __init__(self, strategy: Strategy, broker: Broker, risk: RiskManager, db: OpsDB | None = None,
                 log_signals: bool = True):
        self.strategy, self.broker, self.risk, self.db = strategy, broker, risk, db
        self.ledger = Ledger()
        self.pending_exit_reason: dict[str, str] = {}
        self.log_signals = log_signals and db is not None
        self.blocked: list[tuple[int, str]] = []
        self.bar_index = 0

    # ---------------------------------------------------------------- equity
    def equity(self, marks: dict[str, float]) -> float:
        bal = self.broker.balances()
        krw = bal["KRW"].total if "KRW" in bal else 0.0
        coins = sum(p.volume * marks.get(sym, p.avg_price) for sym, p in self.ledger.positions.items() if p.is_open)
        return krw + coins

    def cash(self) -> float:
        b = self.broker.balances().get("KRW")
        return b.free if b else 0.0

    # ----------------------------------------------------------------- fills
    def apply_fills(self, fills: list[Fill], bar_index: int | None = None, exit_reason: str = "") -> None:
        for f in fills:
            pos = self.ledger.positions.get(f.symbol)
            self.ledger.fees += f.fee
            self.ledger.slippage += f.slippage_krw
            if self.db is not None:
                self.db.add_fill(f)
            if f.side == Side.BUY:
                if pos is None or not pos.is_open:
                    # reuse the placeholder created at signal time (it carries stop / target)
                    pos = pos or Position(f.symbol, self.strategy.label)
                    pos.volume, pos.avg_price, pos.entry_fee, pos.mae, pos.mfe = 0.0, 0.0, 0.0, 0.0, 0.0
                    pos.entry_ts = f.ts
                    self.ledger.positions[f.symbol] = pos
                    self.ledger.entry_bar[f.symbol] = bar_index if bar_index is not None else self.bar_index
                    self.ledger.entry_slip[f.symbol] = 0.0
                new_vol = pos.volume + f.volume
                pos.avg_price = (pos.avg_price * pos.volume + f.price * f.volume) / new_vol
                pos.volume = new_vol
                pos.entry_fee += f.fee
                self.ledger.entry_slip[f.symbol] += f.slippage_krw
                if pos.stop_price:
                    pos.initial_risk_krw = pos.volume * (pos.avg_price - pos.stop_price)
            else:
                if pos is None or not pos.is_open:
                    log.error("sell fill without position %s", f)
                    continue
                vol = min(f.volume, pos.volume)
                frac = vol / pos.volume
                entry_fee_part = pos.entry_fee * frac
                gross = (f.price - pos.avg_price) * vol
                pnl = gross - entry_fee_part - f.fee
                self.ledger.realized += pnl
                pos.entry_fee -= entry_fee_part
                pos.volume -= vol
                acc = self.ledger.partial.setdefault(f.symbol, {"pnl": 0.0, "fees": 0.0, "slip": 0.0, "vol": 0.0, "notional": 0.0})
                acc["pnl"] += pnl
                acc["fees"] += entry_fee_part + f.fee
                acc["slip"] += f.slippage_krw
                acc["vol"] += vol
                acc["notional"] += vol * f.price
                if pos.volume * pos.avg_price < 1.0:  # sub-1-KRW dust is unsellable on Upbit; treat as closed
                    pos.volume = 0.0
                if not pos.is_open:
                    acc = self.ledger.partial.pop(f.symbol)
                    r = acc["pnl"] / pos.initial_risk_krw if pos.initial_risk_krw > 0 else 0.0
                    eb = self.ledger.entry_bar.get(f.symbol, self.bar_index)
                    reason = exit_reason or self.pending_exit_reason.get(f.symbol, "exit")
                    self.pending_exit_reason.pop(f.symbol, None)
                    self.ledger.trades.append(ClosedTrade(
                        f.symbol, pos.strategy, pos.entry_ts, f.ts, pos.avg_price, acc["notional"] / acc["vol"], acc["vol"],
                        acc["pnl"], acc["fees"], self.ledger.entry_slip.pop(f.symbol, 0.0) + acc["slip"], r,
                        pos.mae, pos.mfe, reason, self.ledger.entry_reason.pop(f.symbol, ""),
                        (bar_index if bar_index is not None else self.bar_index) - eb))
                    self.risk.on_trade_closed(pnl, by_stop=reason == "stop")
                    pos.volume = 0.0
            if self.db is not None:
                self.db.save_position(pos)

    # -------------------------------------------------------------- decisions
    def on_bar_close(self, i: int, f: Features, symbol: str, ts: int, ref_price: float,
                     abs_index: int | None = None) -> list[Order]:
        """Evaluate the strategy at the close of bar i and submit resulting orders.

        ``abs_index`` is a monotonically increasing bar counter (live windows re-index every bar)."""
        self.bar_index = abs_index if abs_index is not None else i
        pos = self.ledger.positions.get(symbol)
        if pos is not None and pos.is_open:
            pos.mae = min(pos.mae, f.low[i] / pos.avg_price - 1)
            pos.mfe = max(pos.mfe, f.high[i] / pos.avg_price - 1)
        held = self.bar_index - self.ledger.entry_bar.get(symbol, self.bar_index) if pos is not None and pos.is_open else 0
        out: list[Order] = []
        for sig in self.strategy.on_bar(i, f, symbol, pos, held):
            o = self.handle_signal(sig, ts, ref_price, int(f.ts[i]))
            if o is not None:
                out.append(o)
        return out

    def handle_signal(self, sig: Signal, ts: int, ref_price: float, bar_ts: int | None = None) -> Order | None:
        pos = self.ledger.positions.get(sig.symbol)
        accepted, reason, order = False, sig.reason, None
        if sig.action == "BLOCK":
            self.blocked.append((ts, sig.reason))
        elif sig.action == "EXIT" and pos is not None and pos.is_open:
            order = self.exit_position(sig.symbol, ts, sig.reason or "strategy exit")
            accepted = order is not None
        elif sig.action == "ENTER":
            eq = self.equity({sig.symbol: ref_price})
            d = self.risk.check_entry(sig, ts, ref_price, eq, self.cash(), self.ledger.positions,
                                      getattr(self.broker, "open_order_krw", lambda: 0.0)())
            if d.ok:
                req = OrderRequest(sig.symbol, Side.BUY, OrderType.MARKET_BUY, self.strategy.label, price=round(d.krw_amount),
                                   reason=sig.reason, stop_price=sig.stop_price, take_profit=sig.take_profit, created_ts=ts)
                order = self._submit(req, ts)
                if order is not None and order.state.name != "REJECTED":
                    accepted = True
                    p = self.ledger.positions.setdefault(sig.symbol, Position(sig.symbol, self.strategy.label))
                    p.stop_price, p.take_profit = sig.stop_price, sig.take_profit
                    self.ledger.entry_reason[sig.symbol] = sig.reason
                    self.apply_fills(self.broker.drain_fills())
                else:
                    reason = f"rejected: {order.reject_reason if order else 'submit error'}"
            else:
                reason = d.reason
                self.blocked.append((ts, d.reason))
        if self.log_signals:
            self.db.log_signal(ts, bar_ts, sig.symbol, sig.strategy, sig.action, accepted, reason,
                               {"stop": sig.stop_price, "tp": sig.take_profit})
        return order

    def exit_position(self, symbol: str, ts: int, reason: str) -> Order | None:
        pos = self.ledger.positions.get(symbol)
        if pos is None or not pos.is_open:
            return None
        bal = self.broker.balances().get(symbol.split("-", 1)[1])
        vol = min(pos.volume, bal.free if bal else 0.0)
        if vol <= 0:
            return None
        self.pending_exit_reason[symbol] = reason
        return self._submit(OrderRequest(symbol, Side.SELL, OrderType.MARKET_SELL, self.strategy.label, volume=vol,
                                         reason=reason, created_ts=ts), ts)

    def _submit(self, req: OrderRequest, ts: int) -> Order | None:
        try:
            self.risk.record_order(ts)
            o = self.broker.submit(req)
        except OrderRejected as e:
            if self.db is not None:
                self.db.log_event("WARNING", "core", f"order rejected before send: {e}")
            return None
        except PermissionError as e:
            if self.db is not None:
                self.db.log_event("CRITICAL", "core", f"live guard refused order: {e}")
            return None
        if self.db is not None:
            self.db.upsert_order(o)
        return o

    # ----------------------------------------------------------------- stops
    def stop_or_target_hit(self, symbol: str, price: float) -> str | None:
        pos = self.ledger.positions.get(symbol)
        if pos is None or not pos.is_open:
            return None
        if pos.stop_price is not None and price <= pos.stop_price:
            return "stop"
        if pos.take_profit is not None and price >= pos.take_profit:
            return "target"
        return None
