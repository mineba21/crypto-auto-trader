import pytest

from crypto_trader.core.models import Position, Signal
from crypto_trader.core.timeutils import parse_utc
from crypto_trader.risk.kill_switch import KillSwitch
from crypto_trader.risk.manager import RiskManager, market_data_guard
from crypto_trader.storage.ops_db import OpsDB


def rm(settings, alloc=300_000, cost=0.0):
    return RiskManager(settings, KillSwitch(OpsDB(":memory:", "BACKTEST")), alloc, round_trip_cost=cost)


@pytest.mark.parametrize("alloc,trade,daily", [(300_000, 4_500, 9_000), (1_000_000, 15_000, 30_000), (3_000_000, 30_000, 60_000)])
def test_spec_table_limits(settings, alloc, trade, daily):
    r = rm(settings, alloc)
    assert r.per_trade_risk(alloc) == pytest.approx(trade)
    assert r.daily_loss_limit() == pytest.approx(daily)


def sig(stop=95.0):
    return Signal(0, "KRW-BTC", "t", "ENTER", stop_price=stop)


def test_position_size_includes_costs(settings):
    r = rm(settings, 1_000_000, cost=0.003)
    d = r.check_entry(sig(95.0), 0, 100.0, 1_000_000, 1_000_000, {}, 0)
    # risk per unit = 5 + 0.3 = 5.3 -> qty = 15000/5.3
    assert d.ok and d.qty == pytest.approx(15_000 / 5.3) and d.risk_krw == pytest.approx(15_000)


def test_size_capped_by_cash_and_min_order(settings):
    r = rm(settings, 300_000)
    d = r.check_entry(sig(99.9), 0, 100.0, 300_000, 50_000, {}, 0)
    assert d.ok and d.krw_amount <= 50_000 / (1 + 2 * settings.costs.upbit_fee_rate) + 1e-6
    d2 = r.check_entry(sig(99.9), 0, 100.0, 300_000, 4_000, {}, 0)
    assert not d2.ok and "minimum" in d2.reason


def test_no_averaging_down_and_max_positions(settings):
    r = rm(settings)
    pos = {"KRW-BTC": Position("KRW-BTC", "t", volume=1, avg_price=100)}
    assert "averaging" in r.check_entry(sig(), 0, 100, 300_000, 300_000, pos, 0).reason


def test_exposure_includes_open_orders(settings):
    r = rm(settings, 300_000)
    d = r.check_entry(sig(99.0), 0, 100.0, 300_000, 300_000, {}, open_order_krw=299_000)
    assert not d.ok


def test_daily_limit_resets_at_kst_midnight_but_cumulative_persists(settings):
    r = rm(settings, 300_000)
    t0 = parse_utc("2026-01-01T00:00:00")  # 09:00 KST
    r.on_equity(t0, 300_000)
    r.on_equity(t0 + 3_600_000, 290_000)  # -10k > 9k daily limit
    assert not r.check_entry(sig(), t0 + 3_600_000, 100, 290_000, 290_000, {}, 0).ok
    # 2025-12-31T15:00Z == 2026-01-01 00:00 KST; next KST day starts at 2026-01-01T15:00Z
    nxt = parse_utc("2026-01-01T15:00:01")
    r.on_equity(nxt, 290_000)
    assert r.check_entry(sig(), nxt, 100, 290_000, 290_000, {}, 0).ok
    assert r.state.peak_equity == 300_000  # cumulative state not reset


def test_consecutive_losses_shrink_size(settings):
    r = rm(settings, 1_000_000)
    for _ in range(3):
        r.on_trade_closed(-1000, True)
    assert r.per_trade_risk(1_000_000) == pytest.approx(7_500)
    r.on_trade_closed(+100, False)
    assert r.per_trade_risk(1_000_000) == pytest.approx(15_000)


def test_drawdown_shrinks_size(settings):
    r = rm(settings, 1_000_000)
    r.on_equity(0, 1_000_000)
    assert r.size_scale(900_000) < 1.0


def test_cumulative_loss_trips_kill_switch(settings):
    settings.capital.emergency_cum_loss_krw = 100_000
    r = rm(settings, 300_000)
    r.on_equity(0, 199_000)
    assert r.ks.active and r.ks.status()["reasons"][0]["code"] == "CUM_LOSS"
    assert not r.check_entry(sig(), 0, 100, 199_000, 199_000, {}, 0).ok


def test_order_burst_trips_kill_switch(settings):
    r = rm(settings)
    for i in range(3 * settings.risk.max_orders_per_minute + 1):
        r.record_order(i)
    assert r.ks.active


def test_market_data_guard(settings):
    assert market_data_guard(100_000, None, 1, settings) == "STALE_MARKET_DATA"
    assert market_data_guard(100_000, 99_000, 100, settings) == "ABNORMAL_SPREAD"
    assert market_data_guard(10_000_000, 9_999_000, 1, settings, derivatives_ts=0, needs_derivatives=True) == "STALE_DERIVATIVES"
    assert market_data_guard(100_000, 99_000, 1, settings, clock_skew_ms=5000) == "CLOCK_SKEW"
    assert market_data_guard(100_000, 99_000, 1, settings) is None


def test_allocation_cannot_exceed_trading_capital(settings):
    with pytest.raises(ValueError):
        rm(settings, 5_000_000)
