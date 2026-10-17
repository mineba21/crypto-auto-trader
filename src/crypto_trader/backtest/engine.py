"""Event-driven single-market backtest that drives the same TradingCore / RiskManager as PAPER and LIVE."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl

from crypto_trader.brokers.backtest import BacktestBroker
from crypto_trader.core.config import Settings
from crypto_trader.core.models import Bar
from crypto_trader.execution.core import ClosedTrade, TradingCore
from crypto_trader.risk.kill_switch import KillSwitch
from crypto_trader.risk.manager import RiskManager
from crypto_trader.storage.ops_db import OpsDB
from crypto_trader.strategies.base import Features, Strategy


@dataclass
class BacktestResult:
    strategy: str
    family: str
    symbol: str
    interval: str
    cost_rt: float
    allocation: float
    ts: np.ndarray
    equity: np.ndarray
    trades: list[ClosedTrade]
    fees: float
    slippage: float
    rejections: list[str] = field(default_factory=list)
    blocked: int = 0
    kill_switch: dict = field(default_factory=dict)
    start_ts: int = 0
    end_ts: int = 0
    missing_inputs: list[str] = field(default_factory=list)
    status: str = "OK"  # OK | NO_DATA


def frame_to_bars(df: pl.DataFrame, symbol: str, interval: str) -> list[Bar]:
    cols = [df[c].to_numpy() for c in ("open_ts", "close_ts", "open", "high", "low", "close", "volume", "quote_volume")]
    return [Bar("upbit", symbol, interval, int(a), int(b), float(o), float(h), float(lo), float(c), float(v), float(q), int(b))
            for a, b, o, h, lo, c, v, q in zip(*cols, strict=True)]


def run_backtest(strategy: Strategy, df: pl.DataFrame, symbol: str, interval: str, settings: Settings,
                 cost_rt: float, allocation: float | None = None, features: Features | None = None) -> BacktestResult:
    allocation = float(allocation or settings.strategy_allocation_krw)
    fee = settings.costs.upbit_fee_rate
    slip = max(0.0, cost_rt / 2 - fee)
    f = features or Features.from_frame(df)
    missing = strategy.missing_inputs(f)
    if missing or df.height < strategy.warmup + 2:
        return BacktestResult(strategy.label, strategy.family, symbol, interval, cost_rt, allocation, np.array([]),
                              np.array([]), [], 0.0, 0.0, missing_inputs=missing, status="NO_DATA")
    strategy.prepare(f)
    bars = frame_to_bars(df, symbol, interval)
    broker = BacktestBroker(allocation, fee, slip, settings.costs.max_bar_participation, settings.risk.min_order_krw)
    ks = KillSwitch(OpsDB(":memory:", "BACKTEST"))
    risk = RiskManager(settings, ks, allocation, round_trip_cost=cost_rt)
    core = TradingCore(strategy, broker, risk, db=None)
    n = len(bars)
    eq = np.empty(n)
    for i, bar in enumerate(bars):
        pos = core.ledger.positions.get(symbol)
        if pos is not None and pos.is_open and symbol in core.pending_exit_reason and not broker.open_orders():
            # an exit only partially filled (liquidity cap): keep selling the remainder at the next open
            core.exit_position(symbol, bar.open_ts, core.pending_exit_reason[symbol])
        broker.fill_at_open(bar)
        core.apply_fills(broker.drain_fills(), i)
        pos = core.ledger.positions.get(symbol)
        if pos is not None and pos.is_open:
            trig = None
            stop, tp = pos.stop_price, pos.take_profit
            if stop is not None and bar.open <= stop:
                trig = ("stop", bar.open, bar.open_ts)  # gapped through the stop
            elif tp is not None and bar.open >= tp:
                trig = ("target", bar.open, bar.open_ts)
            elif stop is not None and bar.low <= stop:
                trig = ("stop", stop, bar.close_ts - 1)  # both inside the bar -> assume the stop came first
            elif tp is not None and bar.high >= tp:
                trig = ("target", tp, bar.close_ts - 1)
            if trig and symbol not in core.pending_exit_reason:
                o = core.exit_position(symbol, trig[2], trig[0])
                if o is not None:
                    broker.fill_now(o, trig[1], bar, trig[2])
                    core.apply_fills(broker.drain_fills(), i, exit_reason=trig[0])
        e = core.equity({symbol: bar.close})
        eq[i] = e
        risk.on_equity(bar.close_ts, e)
        if not ks.active:
            core.on_bar_close(i, f, symbol, bar.close_ts, bar.close)
    # mark-to-market the open position at the end as an "end of data" exit (no extra fill simulated beyond cost)
    pos = core.ledger.positions.get(symbol)
    if pos is not None and pos.is_open and bars:
        last = bars[-1]
        o = core.exit_position(symbol, last.close_ts, "end_of_data")
        if o is not None:
            broker.fill_now(o, last.close, last, last.close_ts)
            core.apply_fills(broker.drain_fills(), n - 1, exit_reason="end_of_data")
            eq[-1] = core.equity({symbol: last.close})
    return BacktestResult(strategy.label, strategy.family, symbol, interval, cost_rt, allocation,
                          np.array([b.close_ts for b in bars]), eq, core.ledger.trades, broker.total_fees,
                          broker.total_slippage, broker.rejections, len(core.blocked), ks.status(),
                          bars[0].open_ts, bars[-1].close_ts)


def buy_and_hold(df: pl.DataFrame, allocation: float, cost_rt: float) -> tuple[np.ndarray, np.ndarray]:
    c = df["close"].to_numpy().astype(float)
    o0 = df["open"].to_numpy()[0]
    eq = allocation * (1 - cost_rt / 2) * c / o0
    eq[-1] *= 1 - cost_rt / 2
    return df["close_ts"].to_numpy(), eq


def random_entry_baseline(df: pl.DataFrame, trades: list[ClosedTrade], cost_rt: float, n_sims: int = 500,
                          seed: int = 7) -> dict:
    """Same number of trades & holding periods as the strategy, entered at random bars (no signal).

    Returns the distribution of mean trade return and the strategy's empirical p-value (one-sided).
    """
    if not trades:
        return {"n": 0}
    rng = np.random.default_rng(seed)
    o = df["open"].to_numpy().astype(float)
    c = df["close"].to_numpy().astype(float)
    n = len(o)
    holds = np.array([max(1, t.bars_held) for t in trades])
    strat_ret = np.array([t.exit_price / t.entry_price - 1 - cost_rt for t in trades])
    sims = np.empty(n_sims)
    for k in range(n_sims):
        starts = rng.integers(1, max(2, n - holds.max() - 1), size=len(holds))
        ends = np.minimum(starts + holds, n - 1)
        sims[k] = np.mean(c[ends] / o[starts] - 1 - cost_rt)
    m = strat_ret.mean()
    return {"n": len(trades), "strategy_mean": float(m), "random_mean": float(sims.mean()),
            "random_p95": float(np.percentile(sims, 95)), "p_value": float((np.sum(sims >= m) + 1) / (n_sims + 1))}
