"""Real-time runner for PAPER and LIVE modes.

Data path: Upbit WS (trades/orderbook) for stops, health and paper fills; Upbit REST closed candles for the strategy
bars (the very same candle source the backtest uses). Optional derivatives feed for B-family strategies.
The runner never enables LIVE by itself: ``run_live`` refuses to start unless LiveGuard verifies every gate.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

import polars as pl

from crypto_trader.brokers.paper import PaperBroker
from crypto_trader.collectors.http import AccessBlocked
from crypto_trader.collectors.jobs import bars_to_df
from crypto_trader.collectors.upbit.rest import UpbitRest
from crypto_trader.collectors.upbit.ws import UpbitWsCollector
from crypto_trader.core.config import Settings, upbit_keys
from crypto_trader.core.models import Mode, OrderBookSnapshot, Trade
from crypto_trader.core.timeutils import INTERVAL_MS, now_ms
from crypto_trader.execution.core import TradingCore
from crypto_trader.risk.kill_switch import KillSwitch
from crypto_trader.risk.manager import RiskManager, market_data_guard
from crypto_trader.storage.ops_db import OpsDB
from crypto_trader.storage.parquet_store import ParquetStore
from crypto_trader.strategies.base import Features
from crypto_trader.strategies.library import build

log = logging.getLogger(__name__)
TRANSIENT = ("STALE_MARKET_DATA", "ABNORMAL_SPREAD", "STALE_DERIVATIVES", "CLOCK_SKEW", "API_ERROR")


class LiveSession:
    def __init__(self, settings: Settings, core: TradingCore, db: OpsDB, ks: KillSwitch, symbol: str, interval: str,
                 rest: UpbitRest, store: ParquetStore | None, paper: PaperBroker | None):
        self.s, self.core, self.db, self.ks = settings, core, db, ks
        self.symbol, self.interval, self.rest, self.store, self.paper = symbol, interval, rest, store, paper
        self.window: pl.DataFrame = pl.DataFrame()
        self.last_trade: Trade | None = None
        self.last_book: OrderBookSnapshot | None = None
        self.last_bar_open: int | None = None
        self.exiting = False

    # ------------------------------------------------------------ callbacks
    def on_trade(self, t: Trade) -> None:
        if t.symbol != self.symbol:
            return
        self.last_trade = t
        if self.paper is not None:
            self.paper.on_trade(t)
        reason = self.core.stop_or_target_hit(self.symbol, t.price)
        if reason and not self.exiting:
            self.exiting = True
            try:
                o = self.core.exit_position(self.symbol, t.received_ts, reason)
                self.core.apply_fills(self.core.broker.drain_fills(), exit_reason=reason)
                self.db.log_event("WARNING", "runner", f"{reason} exit {self.symbol} @~{t.price}",
                                  {"order": o.client_order_id if o else None, "state": str(o.state) if o else None})
            finally:
                self.exiting = False
            self._persist_paper()

    def on_orderbook(self, ob: OrderBookSnapshot) -> None:
        if ob.symbol != self.symbol:
            return
        self.last_book = ob
        if self.paper is not None:
            self.paper.on_orderbook(ob)

    # --------------------------------------------------------------- state
    def _persist_paper(self) -> None:
        if self.paper is not None:
            self.db.set_kv("paper_account", {"cash": self.paper.cash + self.paper.locked_cash, "coins": self.paper.coins})

    async def load_window(self, bars: int = 600) -> None:
        step = INTERVAL_MS[self.interval]
        end = now_ms()
        got = await self.rest.candles(self.symbol, self.interval, end - bars * step, end)
        self.window = bars_to_df(got)
        if not self.window.is_empty():
            self.last_bar_open = int(self.window["open_ts"].max())

    async def poll_new_bar(self) -> bool:
        step = INTERVAL_MS[self.interval]
        latest_closed_open = (now_ms() // step - 1) * step
        if self.last_bar_open is not None and latest_closed_open <= self.last_bar_open:
            return False
        got = await self.rest.candles(self.symbol, self.interval, (self.last_bar_open or latest_closed_open - step) + step,
                                      latest_closed_open + step)
        if not got:
            return False
        new = bars_to_df(got)
        self.window = pl.concat([self.window, new], how="diagonal_relaxed").unique("open_ts", keep="last").sort("open_ts").tail(1000)
        self.last_bar_open = int(self.window["open_ts"].max())
        return True

    def decide(self) -> None:
        f = Features.from_frame(self.window)
        if self.core.strategy.missing_inputs(f):
            self.db.log_event("WARNING", "runner", f"strategy inputs missing: {self.core.strategy.missing_inputs(f)}")
            return
        self.core.strategy.prepare(f)
        i = len(f) - 1
        abs_i = int(self.window["open_ts"][-1]) // INTERVAL_MS[self.interval]
        ref = self.last_trade.price if self.last_trade else float(f.close[i])
        self.core.on_bar_close(i, f, self.symbol, now_ms(), ref, abs_index=abs_i)
        self.core.apply_fills(self.core.broker.drain_fills(), abs_i)
        self._persist_paper()

    def health(self) -> None:
        now = now_ms()
        reason = market_data_guard(now, self.last_trade.received_ts if self.last_trade else None,
                                   self.last_book.spread_bps if self.last_book else None, self.s)
        # low-liquidity markets can legitimately be quiet; use the book as liveness too
        if reason == "STALE_MARKET_DATA" and self.last_book and now - self.last_book.received_ts < self.s.risk.market_data_stale_ms:
            reason = None
        for code in TRANSIENT:
            if code != reason and any(r["code"] == code for r in self.ks.status().get("reasons", [])):
                self.ks.clear_transient(code)
        if reason:
            self.ks.trigger(reason, f"detected at {now}")
        mark = self.last_trade.price if self.last_trade else None
        if mark:
            eq = self.core.equity({self.symbol: mark})
            self.core.risk.on_equity(now, eq)
            pos = self.core.ledger.positions.get(self.symbol)
            unreal = (mark - pos.avg_price) * pos.volume if pos and pos.is_open else 0.0
            self.db.record_equity(now // 60_000 * 60_000, self.core.cash(), eq, self.core.ledger.realized, unreal)
        self.db.heartbeat(f"runner:{self.db.mode}", "KILLED" if self.ks.active else "OK",
                          f"{self.symbol} {self.core.strategy.label}")
        self.db.set_kv("risk_state", {"day": self.core.risk.state.day, "day_start_equity": self.core.risk.state.day_start_equity,
                                      "peak_equity": self.core.risk.state.peak_equity,
                                      "consecutive_losses": self.core.risk.state.consecutive_losses,
                                      "daily_loss_limit": self.core.risk.daily_loss_limit(),
                                      "per_trade_risk": self.core.risk.per_trade_risk(self.core.risk.state.peak_equity)})


async def _session_loop(sess: LiveSession, ws: UpbitWsCollector, duration_s: float | None) -> None:
    ws_task = asyncio.create_task(ws.run(duration_s))
    deadline = time.time() + duration_s if duration_s else None
    try:
        while deadline is None or time.time() < deadline:
            try:
                if await sess.poll_new_bar():
                    sess.decide()
                sess.health()
            except sqlite3.Error as e:
                sess.ks.trigger("DB_WRITE_FAILURE", str(e))  # in-memory activation even if DB is broken
            except AccessBlocked as e:
                sess.ks.trigger("API_ERROR", f"access blocked: {e.status}")
            except Exception as e:  # noqa: BLE001
                log.exception("runner loop error")
                sess.ks.trigger("API_ERROR", f"{type(e).__name__}: {str(e)[:200]}")
            sess.db.set_kv("ws_health", {"connected": ws.health.connected, "reconnects": ws.health.reconnects,
                                         "last_msg_ms": ws.health.last_msg_ms, "gaps": ws.health.gaps[-10:],
                                         "last_error": ws.health.last_error})
            await asyncio.sleep(5)
    finally:
        ws.stop()
        await ws_task


async def run_paper(settings: Settings, duration_s: float | None = None, strategy_name: str = "A2_sweep_choch",
                    symbol: str | None = None, interval: str = "1h") -> int:
    symbol = symbol or settings.markets[0]
    db = OpsDB(settings.path(settings.ops_db), Mode.PAPER)
    ks = KillSwitch(db)
    acct = db.get_kv("paper_account")
    broker = PaperBroker(settings.strategy_allocation_krw, settings.costs.upbit_fee_rate, min_order_krw=settings.risk.min_order_krw)
    if acct:  # restart: restore virtual balances and positions
        broker.cash, broker.coins = float(acct["cash"]), {k: float(v) for k, v in acct["coins"].items()}
    risk = RiskManager(settings, ks, settings.strategy_allocation_krw, round_trip_cost=settings.costs.scenarios["base"])
    core = TradingCore(build(strategy_name), broker, risk, db)
    core.ledger.positions = db.load_positions()
    for sym, p in core.ledger.positions.items():
        core.ledger.entry_bar[sym] = p.entry_ts // INTERVAL_MS[interval]
    rest = UpbitRest()
    store = ParquetStore(settings.path(settings.data_dir))
    sess = LiveSession(settings, core, db, ks, symbol, interval, rest, store, broker)
    await sess.load_window()
    db.log_event("INFO", "runner", f"PAPER start {symbol} {strategy_name}", {"restored": bool(acct)})
    ws = UpbitWsCollector([symbol], store, rest=UpbitRest(), on_trade=sess.on_trade, on_orderbook=sess.on_orderbook)
    try:
        await _session_loop(sess, ws, duration_s)
    finally:
        await rest.close()
        db.log_event("INFO", "runner", "PAPER stop")
    return 0


async def run_live(settings: Settings, duration_s: float | None = None, strategy_name: str = "A2_sweep_choch",
                   symbol: str | None = None, interval: str = "1h") -> int:
    from crypto_trader.brokers.upbit_live import LiveGuard, UpbitLiveBroker, UpbitPrivateClient

    guard = LiveGuard(settings)
    try:
        guard.verify()
    except PermissionError as e:
        log.error("%s", e)
        print(str(e))
        return 2
    ak, sk = upbit_keys()
    symbol = symbol or guard.approval.markets[0]
    db = OpsDB(settings.path(settings.ops_db), Mode.LIVE)
    ks = KillSwitch(db)
    last_px: dict[str, float] = {}
    broker = UpbitLiveBroker(UpbitPrivateClient(ak or "", sk or ""), db, guard, ks, ref_price=lambda s: last_px.get(s, 0.0))
    risk = RiskManager(settings, ks, min(settings.strategy_allocation_krw, guard.approval.max_krw),
                       round_trip_cost=settings.costs.scenarios["base"])
    core = TradingCore(build(strategy_name), broker, risk, db)
    core.ledger.positions = db.load_positions()
    for sym, p in core.ledger.positions.items():
        core.ledger.entry_bar[sym] = p.entry_ts // INTERVAL_MS[interval]
    issues = broker.reconcile({s: p.volume for s, p in core.ledger.positions.items() if p.is_open})
    if issues:
        print("reconcile issues -> kill switch:", issues)
    rest = UpbitRest()
    sess = LiveSession(settings, core, db, ks, symbol, interval, rest, ParquetStore(settings.path(settings.data_dir)), None)
    orig = sess.on_trade

    def on_trade(t: Trade) -> None:
        last_px[t.symbol] = t.price
        orig(t)

    sess.on_trade = on_trade  # type: ignore[method-assign]
    await sess.load_window()
    ws = UpbitWsCollector([symbol], sess.store, rest=UpbitRest(), on_trade=sess.on_trade, on_orderbook=sess.on_orderbook)
    db.log_event("WARNING", "runner", f"LIVE start {symbol} {strategy_name}", {"approval": guard.approval.__dict__})
    try:
        await _session_loop(sess, ws, duration_s)
    finally:
        await rest.close()
    return 0
