import numpy as np
import pytest

from crypto_trader.brokers.paper import PaperBroker
from crypto_trader.core.models import (
    BookLevel,
    Mode,
    OrderBookSnapshot,
    OrderRequest,
    OrderType,
    Position,
    Side,
)
from crypto_trader.core.timeutils import now_ms
from crypto_trader.execution.core import TradingCore
from crypto_trader.risk.kill_switch import KillSwitch
from crypto_trader.risk.manager import RiskManager
from crypto_trader.storage.ops_db import OpsDB
from crypto_trader.strategies.base import Features
from crypto_trader.strategies.library import build


def test_ops_db_orders_positions_survive_restart(tmp_path):
    p = tmp_path / "ops.sqlite"
    db = OpsDB(p, Mode.PAPER)
    from crypto_trader.core.models import Order, OrderState
    o = Order(OrderRequest("KRW-BTC", Side.BUY, OrderType.MARKET_BUY, "s", price=10_000), state=OrderState.UNKNOWN)
    db.upsert_order(o)
    db.save_position(Position("KRW-BTC", "s", volume=0.1, avg_price=100, stop_price=90))
    db.close()
    db2 = OpsDB(p, Mode.PAPER)
    assert db2.load_orders(active_only=True)[0].state == OrderState.UNKNOWN
    assert db2.load_positions()["KRW-BTC"].stop_price == 90
    assert OpsDB(p, Mode.LIVE).load_positions() == {}  # mode isolation


def test_paper_core_stop_exit_and_persisted_fills(settings, tmp_path, bars):
    db = OpsDB(tmp_path / "paper.sqlite", Mode.PAPER)
    ks = KillSwitch(db)
    broker = PaperBroker(300_000, settings.costs.upbit_fee_rate)
    broker.on_orderbook(OrderBookSnapshot("upbit", "KRW-BTC", now_ms(), now_ms(), (BookLevel(99.9, 1e6, 100.0, 1e6),)))
    core = TradingCore(build("A2_sweep_choch"), broker, RiskManager(settings, ks, 300_000), db)
    from crypto_trader.core.models import Signal
    core.handle_signal(Signal(now_ms(), "KRW-BTC", "A2", "ENTER", stop_price=95.0), now_ms(), 100.0)
    pos = core.ledger.positions["KRW-BTC"]
    assert pos.is_open and pos.stop_price == 95.0 and db.load_positions()["KRW-BTC"].volume == pytest.approx(pos.volume)
    assert core.stop_or_target_hit("KRW-BTC", 94.0) == "stop"
    broker.on_orderbook(OrderBookSnapshot("upbit", "KRW-BTC", now_ms(), now_ms(), (BookLevel(94.0, 1e6, 94.1, 1e6),)))
    core.exit_position("KRW-BTC", now_ms(), "stop")
    core.apply_fills(broker.drain_fills(), exit_reason="stop")
    assert not core.ledger.positions["KRW-BTC"].is_open and core.ledger.trades[0].exit_reason == "stop"
    assert len(db.fills()) == 2 and "KRW-BTC" not in db.load_positions()
    assert db.signals()[0]["accepted"] == 1


def test_backtest_core_never_touches_network(settings, bars, monkeypatch):
    import httpx

    def fail(*a, **k):
        raise AssertionError("network used in BACKTEST")

    monkeypatch.setattr(httpx.Client, "send", fail)
    monkeypatch.setattr(httpx.AsyncClient, "send", fail)
    from crypto_trader.backtest.engine import run_backtest
    run_backtest(build("A2_sweep_choch"), bars, "KRW-BTC", "1h", settings, 0.003)


def test_same_decision_function_live_window_vs_backtest(bars):
    """Live calls prepare() on a rolling window; the decision at the last bar equals the backtest decision."""
    s_full = build("BASE_donchian")
    f = Features.from_frame(bars)
    s_full.prepare(f)
    idx = [i for i in range(800, bars.height) if s_full.on_bar(i, f, "X", None)][:5]
    assert idx
    for i in idx:
        win = bars.slice(i - 700, 701)
        s = build("BASE_donchian")
        fw = Features.from_frame(win)
        s.prepare(fw)
        a = [(x.action, round(x.stop_price, 6)) for x in s_full.on_bar(i, f, "X", None)]
        b = [(x.action, round(x.stop_price, 6)) for x in s.on_bar(len(fw) - 1, fw, "X", None)]
        assert a == b
    assert np.isfinite(f["atr"][-1])
