import numpy as np
import pytest

from crypto_trader.backtest.engine import run_backtest
from crypto_trader.backtest.metrics import summarize
from crypto_trader.backtest.validation import (
    benjamini_hochberg,
    block_bootstrap_sharpe,
    holm,
    lookahead_audit,
)
from crypto_trader.core.models import Signal
from crypto_trader.strategies.base import Strategy
from crypto_trader.strategies.library import REGISTRY, build
from tests.conftest import make_bars


class EnterAt(Strategy):
    name, family = "test_enter", "T"
    default_params = {"at": 10, "stop_pct": 0.05, "tp_pct": None, "max_hold": None, "warmup": 1}

    def entry(self, i, f):
        if i == self.params["at"]:
            tp = f.close[i] * (1 + self.params["tp_pct"]) if self.params["tp_pct"] else None
            return Signal(int(f.ts[i]), "", self.label, "ENTER", f.close[i] * (1 - self.params["stop_pct"]), tp)
        return None


def flat(n=40, px=100.0):
    return make_bars(np.full(n, px), spread=0.0)


def test_entry_fills_next_open_and_eod_exit(settings):
    df = flat()
    r = run_backtest(EnterAt(), df, "KRW-BTC", "1h", settings, cost_rt=0.003)
    t = r.trades[0]
    assert t.entry_ts == int(df["open_ts"][11])  # decided at close of bar 10, filled at bar 11 open
    slip = 0.003 / 2 - settings.costs.upbit_fee_rate
    assert t.entry_price == pytest.approx(100 * (1 + slip))
    assert t.exit_reason == "end_of_data"


def test_stop_fills_at_stop_or_gap_open(settings):
    c = np.full(40, 100.0)
    c[15:] = 90.0  # gap below the 95 stop
    df = make_bars(c, spread=0.0)
    df = df.with_columns(df["open"].scatter(15, 90.0), df["high"].scatter(15, 90.0))  # true gap at bar 15 open
    r = run_backtest(EnterAt(), df, "KRW-BTC", "1h", settings, cost_rt=0.0016)
    t = r.trades[0]
    assert t.exit_reason == "stop"
    slip = 0.0016 / 2 - settings.costs.upbit_fee_rate
    assert t.exit_price == pytest.approx(90 * (1 - slip))  # filled at the gapped open, not the stop
    assert t.r_multiple < -1  # gap made the loss exceed 1R: loss limits are not guarantees


def test_ledger_identity_cash_equals_alloc_plus_pnl(settings):
    df = make_bars(100 * np.exp(np.cumsum(np.random.default_rng(5).normal(0, 0.01, 400))))
    r = run_backtest(build("BASE_ema_trend"), df, "KRW-BTC", "1h", settings, 0.003)
    assert r.trades
    assert r.equity[-1] == pytest.approx(r.allocation + sum(t.pnl_krw for t in r.trades), rel=1e-9)
    s = summarize(r)
    assert s["fees_krw"] > 0 and s["trades"] == len(r.trades)


def test_intrabar_stop_fills_at_stop_price(settings):
    c = np.full(40, 100.0)
    c[15:] = 90.0  # bar 15 opens at 100 and trades down through 95
    r = run_backtest(EnterAt(), make_bars(c, spread=0.0), "KRW-BTC", "1h", settings, cost_rt=0.0016)
    slip = 0.0016 / 2 - settings.costs.upbit_fee_rate
    assert r.trades[0].exit_price == pytest.approx(95 * (1 - slip))


def test_target_and_stop_same_bar_assumes_stop(settings):
    df = flat(40)
    df = df.with_columns(df["high"].scatter(12, 110.0), df["low"].scatter(12, 90.0))
    r = run_backtest(EnterAt(tp_pct=0.05), df, "KRW-BTC", "1h", settings, 0.003)
    assert r.trades[0].exit_reason == "stop"


def test_strategy_missing_inputs_reported(settings, bars):
    r = run_backtest(build("B1_short_squeeze"), bars, "KRW-BTC", "1h", settings, 0.003)
    assert r.status == "NO_DATA" and "oi_chg_4h" in r.missing_inputs


@pytest.mark.parametrize("name", ["A1_sweep_reversal", "A2_sweep_choch", "A3_sweep_fvg", "BASE_donchian", "BASE_ema_trend"])
def test_strategies_have_no_lookahead(name, bars):
    res = lookahead_audit(lambda: build(name), bars, n_points=12)
    assert res["status"] == "PASS", res


def test_long_only_signals(bars, settings):
    for name in ("A1_sweep_reversal", "A2_sweep_choch", "A3_sweep_fvg", "BASE_donchian"):
        r = run_backtest(build(name), bars, "KRW-BTC", "1h", settings, 0.003)
        for t in r.trades:
            assert t.volume > 0 and t.entry_ts <= t.exit_ts


def test_multiple_testing_corrections():
    p = [0.01, 0.04, 0.03, 0.2]
    assert holm(p) == pytest.approx([0.04, 0.09, 0.09, 0.2])
    q = benjamini_hochberg(p)
    assert q[0] == pytest.approx(0.04) and max(q) <= 1


def test_bootstrap_detects_positive_drift():
    r = np.random.default_rng(0).normal(0.003, 0.01, 800)
    out = block_bootstrap_sharpe(r, n_boot=400)
    assert out["sharpe"] > 0 and out["p_le_0"] < 0.05 and out["ci_lo"] < out["sharpe"] < out["ci_hi"]


def test_registry_complete():
    for k in ("A1_sweep_reversal", "A2_sweep_choch", "A3_sweep_fvg", "B1_short_squeeze", "B2_liq_reversal",
              "B3_crowding_filter", "C1_cvd_breakout", "C2_cvd_divergence", "C3_absorption", "C4_obi_flow"):
        assert k in REGISTRY


def _flow_frame(n=1500, seed=4):
    import polars as pl

    rng = np.random.default_rng(seed)
    df = make_bars(random_walk_local(n, seed), step_ms=300_000)
    delta = rng.normal(0, 1, n)
    obi = rng.uniform(-1, 1, n)
    return df.with_columns(pl.Series("delta", delta), pl.Series("cvd", np.cumsum(delta)), pl.Series("valid", np.ones(n, bool)),
                           pl.Series("obi", obi), pl.Series("obi_valid", np.ones(n, bool)),
                           pl.Series("spread_bps", np.full(n, 2.0)),
                           pl.Series("oi_chg_4h", rng.normal(0.01, 0.01, n)), pl.Series("funding_z", rng.normal(0, 1, n)),
                           pl.Series("oi_chg_24h_pctile", rng.uniform(0, 1, n)))


def random_walk_local(n, seed):
    return 1e8 * np.exp(np.cumsum(np.random.default_rng(seed).normal(0, 0.003, n)))


@pytest.mark.parametrize("name", ["C1_cvd_breakout", "C2_cvd_divergence", "C3_absorption", "C4_obi_flow"])
def test_orderflow_strategies_run_and_are_causal(name, settings):
    df = _flow_frame()
    r = run_backtest(build(name), df, "KRW-BTC", "5m", settings, 0.003)
    assert r.status == "OK"
    assert lookahead_audit(lambda: build(name), df, n_points=8)["status"] == "PASS"


def test_combined_modes_run(settings):
    from crypto_trader.strategies.library import Combined, DerivConfirm, FlowConfirm
    df = _flow_frame()
    for mode in ("and", "filter", "score"):
        s = Combined(build("A2_sweep_choch"), [FlowConfirm(), DerivConfirm(), build("B3_crowding_filter")], f"combo-{mode}",
                     mode=mode)
        assert run_backtest(s, df, "KRW-BTC", "5m", settings, 0.003).status == "OK"
