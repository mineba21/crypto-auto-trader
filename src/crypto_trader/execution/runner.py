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
from crypto_trader.collectors.binance.live_feed import BinanceLiveDerivatives
from crypto_trader.collectors.http import AccessBlocked
from crypto_trader.collectors.jobs import bars_to_df
from crypto_trader.collectors.upbit.rest import UpbitRest
from crypto_trader.collectors.upbit.ws import UpbitWsCollector
from crypto_trader.core.config import Settings, upbit_keys
from crypto_trader.core.models import Mode, OrderBookSnapshot, Trade
from crypto_trader.core.timeutils import INTERVAL_MS, now_ms
from crypto_trader.execution.core import TradingCore
from crypto_trader.features.derivatives import add_derivative_features
from crypto_trader.ops.notify import get_notifier
from crypto_trader.risk.kill_switch import KillSwitch
from crypto_trader.risk.manager import RiskManager, market_data_guard
from crypto_trader.storage.ops_db import OpsDB
from crypto_trader.storage.parquet_store import ParquetStore
from crypto_trader.strategies.base import Features
from crypto_trader.strategies.library import build

log = logging.getLogger(__name__)
TRANSIENT = ("STALE_MARKET_DATA", "ABNORMAL_SPREAD", "STALE_DERIVATIVES", "CLOCK_SKEW", "API_ERROR")
DERIV_FEATURES = ("oi_chg_4h", "oi_chg_24h", "oi_pctile_30d", "oi_chg_24h_pctile", "funding_z", "premium_z",
                  "fut_taker_buy_frac")


class LiveSession:
    def __init__(self, settings: Settings, core: TradingCore, db: OpsDB, ks: KillSwitch, symbol: str, interval: str,
                 rest: UpbitRest, store: ParquetStore | None, paper: PaperBroker | None,
                 deriv_feed: BinanceLiveDerivatives | None = None):
        self.s, self.core, self.db, self.ks = settings, core, db, ks
        self.symbol, self.interval, self.rest, self.store, self.paper = symbol, interval, rest, store, paper
        self.deriv_feed = deriv_feed
        self.needs_deriv = bool(set(core.strategy.required_columns) & set(DERIV_FEATURES))
        # derivative z-scores / percentiles need ~90 days of hourly history to match the backtest
        self.window_bars = 2400 if self.needs_deriv else 1000
        self.window: pl.DataFrame = pl.DataFrame()
        self.last_trade: Trade | None = None
        self.last_book: OrderBookSnapshot | None = None
        self.last_bar_open: int | None = None
        self.exiting = False
        self.started_ms = now_ms()

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

    async def load_window(self, bars: int | None = None) -> None:
        bars = bars or self.window_bars
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
        self.window = pl.concat([self.window, new], how="diagonal_relaxed").unique("open_ts", keep="last").sort("open_ts").tail(self.window_bars)
        self.last_bar_open = int(self.window["open_ts"].max())
        return True

    def frame(self) -> pl.DataFrame:
        df = self.window
        if self.needs_deriv and self.deriv_feed is not None:
            fd = self.deriv_feed
            df = add_derivative_features(df, fd.metrics, fd.funding, fd.premium, fd.fut_klines, INTERVAL_MS[self.interval])
        return df

    def decide(self) -> None:
        f = Features.from_frame(self.frame())
        bar_ts = int(self.window["close_ts"][-1])
        pos = self.core.ledger.positions.get(self.symbol)
        pos_open = bool(pos and pos.is_open)
        missing = self.core.strategy.missing_inputs(f)
        if missing:
            self.db.log_event("WARNING", "runner", f"strategy inputs missing: {missing}")
            self.db.log_signal(now_ms(), bar_ts, self.symbol, self.core.strategy.label, "EVAL", False,
                               f"missing inputs: {missing}", {"pos_open": pos_open})
            return
        self.core.strategy.prepare(f)
        i = len(f) - 1
        abs_i = int(self.window["open_ts"][-1]) // INTERVAL_MS[self.interval]
        ref = self.last_trade.price if self.last_trade else float(f.close[i])
        # decision log for every evaluated bar (paper-vs-backtest parity report uses it)
        self.db.log_signal(now_ms(), bar_ts, self.symbol, self.core.strategy.label, "EVAL", True, "",
                           {"pos_open": pos_open, "close": float(f.close[i]), "ref": ref})
        self.core.on_bar_close(i, f, self.symbol, now_ms(), ref, abs_index=abs_i)
        self.core.apply_fills(self.core.broker.drain_fills(), abs_i)
        self._persist_paper()

    def health(self) -> None:
        now = now_ms()
        self.ks.refresh()  # pick up kill switch changes made from the CLI / another process
        if now - self.started_ms < 30_000 and self.last_trade is None:
            return  # startup grace: streams are still connecting
        deriv_ts = self.deriv_feed.last_ok_ms if self.deriv_feed is not None else None
        reason = market_data_guard(now, self.last_trade.received_ts if self.last_trade else None,
                                   self.last_book.spread_bps if self.last_book else None, self.s,
                                   derivatives_ts=deriv_ts, needs_derivatives=self.needs_deriv)
        # low-liquidity markets can legitimately be quiet; use the book as liveness too
        if reason == "STALE_MARKET_DATA" and self.last_book and now - self.last_book.received_ts < self.s.risk.market_data_stale_ms:
            reason = market_data_guard(now, now, self.last_book.spread_bps, self.s, derivatives_ts=deriv_ts,
                                       needs_derivatives=self.needs_deriv)
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


def instance_mode(strategy: str, symbol: str) -> str:
    return f"PAPER:{strategy}:{symbol}"


def build_paper_session(settings: Settings, strategy_name: str, symbol: str, interval: str, global_ks: KillSwitch,
                        rest: UpbitRest, feeds: dict[str, BinanceLiveDerivatives]) -> LiveSession:
    """One isolated PAPER instance: own virtual account, positions, kill switch and logs (mode namespace)."""
    db = OpsDB(settings.path(settings.ops_db), instance_mode(strategy_name, symbol))
    ks = KillSwitch(db, parent=global_ks)
    alloc = settings.strategy_allocation_krw
    broker = PaperBroker(alloc, settings.costs.upbit_fee_rate, min_order_krw=settings.risk.min_order_krw)
    acct = db.get_kv("paper_account")
    if acct:  # restart: restore virtual balances
        broker.cash, broker.coins = float(acct["cash"]), {k: float(v) for k, v in acct["coins"].items()}
    risk = RiskManager(settings, ks, alloc, round_trip_cost=settings.costs.scenarios["base"])
    rs = db.get_kv("risk_state")
    if rs:  # keep daily/peak/streak state across restarts
        risk.state.day, risk.state.day_start_equity = rs.get("day", ""), rs.get("day_start_equity", alloc)
        risk.state.peak_equity, risk.state.consecutive_losses = rs.get("peak_equity", alloc), rs.get("consecutive_losses", 0)
    core = TradingCore(build(strategy_name), broker, risk, db)
    core.ledger.positions = db.load_positions()
    for sym, p in core.ledger.positions.items():
        core.ledger.entry_bar[sym] = p.entry_ts // INTERVAL_MS[interval]
    fut = settings.futures_symbols.get(symbol)
    sess = LiveSession(settings, core, db, ks, symbol, interval, rest, None, broker, deriv_feed=None)
    if sess.needs_deriv and fut:
        sess.deriv_feed = feeds.setdefault(fut, BinanceLiveDerivatives(ParquetStore(settings.path(settings.data_dir)), fut))
    db.log_event("INFO", "runner", f"PAPER instance start {symbol} {strategy_name}", {"restored": bool(acct)})
    return sess


async def run_paper_fleet(settings: Settings, instances: list[tuple[str, str]], duration_s: float | None = None,
                          interval: str = "1h") -> int:
    """Run several PAPER instances off one shared Upbit WebSocket. Never sends real orders."""
    from crypto_trader.ops.fleet_ops import FleetOps

    global_db = OpsDB(settings.path(settings.ops_db), "PAPER")
    global_ks = KillSwitch(global_db)
    rest = UpbitRest()
    feeds: dict[str, BinanceLiveDerivatives] = {}
    sessions = [build_paper_session(settings, st, sym, interval, global_ks, rest, feeds) for st, sym in instances]
    for sess in sessions:
        await sess.load_window()
    for f in feeds.values():
        await f.refresh()
    skipped = [s for s in sessions if s.needs_deriv and (s.deriv_feed is None or s.deriv_feed.status != "OK")]
    for sess in skipped:
        status = sess.deriv_feed.status if sess.deriv_feed else "no futures mapping"
        sess.db.log_event("WARNING", "runner", f"instance skipped: derivatives feed {status}")
        log.warning("instance skipped %s: derivatives feed %s", sess.db.mode, status)
        get_notifier().send(f"⚠️ {sess.core.strategy.label}/{sess.symbol} skipped: Binance derivatives {status}",
                            key=f"skip:{sess.db.mode}", min_interval_s=86_400)
    sessions = [s for s in sessions if s not in skipped]
    if not sessions:
        log.error("no runnable paper instances")
        return 2
    by_symbol: dict[str, list[LiveSession]] = {}
    for sess in sessions:
        by_symbol.setdefault(sess.symbol, []).append(sess)

    def on_trade(t: Trade) -> None:
        for sess in by_symbol.get(t.symbol, []):
            sess.on_trade(t)

    def on_orderbook(ob: OrderBookSnapshot) -> None:
        for sess in by_symbol.get(ob.symbol, []):
            sess.on_orderbook(ob)

    # the fleet does not write market data (the collector service does) -> store=None
    ws = UpbitWsCollector(sorted(by_symbol), None, rest=UpbitRest(), on_trade=on_trade, on_orderbook=on_orderbook)
    ops = FleetOps(settings, global_db, sessions, ws, feeds)
    ops.start()
    ws_task = asyncio.create_task(ws.run(duration_s))
    deadline = time.time() + duration_s if duration_s else None
    last_feed = 0.0
    try:
        while deadline is None or time.time() < deadline:
            if time.time() - last_feed > 300:
                for f in feeds.values():
                    await f.refresh()
                last_feed = time.time()
            for sess in sessions:
                await _tick(sess)
            global_db.set_kv("ws_health", {"connected": ws.health.connected, "reconnects": ws.health.reconnects,
                                           "last_msg_ms": ws.health.last_msg_ms, "gaps": ws.health.gaps[-10:],
                                           "last_error": ws.health.last_error})
            await ops.tick()
            await asyncio.sleep(5)
    finally:
        ws.stop()
        await ws_task
        await rest.close()
        for f in feeds.values():
            await f.close()
        ops.stop()
        global_db.log_event("INFO", "runner", "PAPER fleet stop")
    return 0


async def _tick(sess: LiveSession) -> None:
    try:
        if await sess.poll_new_bar():
            sess.decide()
        sess.health()
    except sqlite3.Error as e:
        sess.ks.trigger("DB_WRITE_FAILURE", str(e))
    except AccessBlocked as e:
        sess.ks.trigger("API_ERROR", f"access blocked: {e.status}")
    except Exception as e:  # noqa: BLE001
        log.exception("instance %s loop error", sess.db.mode)
        sess.ks.trigger("API_ERROR", f"{type(e).__name__}: {str(e)[:200]}")


async def run_paper(settings: Settings, duration_s: float | None = None, strategy_name: str = "A2_sweep_choch",
                    symbol: str | None = None, interval: str = "1h") -> int:
    return await run_paper_fleet(settings, [(strategy_name, symbol or settings.markets[0])], duration_s, interval)


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
