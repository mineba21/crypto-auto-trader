"""Full research run: every strategy family, combos, ablations, cost stress, periods, walk-forward and statistics.

Nothing is tuned to make results look better: strategies run with their default parameters on the full history;
walk-forward is the only place parameters are chosen, and only out-of-sample windows are reported from it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

from crypto_trader.backtest.dataset import load_research_frame, load_trade_bars, load_upbit_bars
from crypto_trader.backtest.engine import BacktestResult, buy_and_hold, random_entry_baseline, run_backtest
from crypto_trader.backtest.metrics import daily_returns, summarize
from crypto_trader.backtest.validation import (
    benjamini_hochberg,
    block_bootstrap_sharpe,
    holm,
    lookahead_audit,
    regime_labels,
    slice_metrics,
    trades_by_regime,
    walk_forward,
)
from crypto_trader.core.config import ROOT, Settings, load_settings
from crypto_trader.core.timeutils import parse_utc
from crypto_trader.features.derivatives import asof_align
from crypto_trader.storage.parquet_store import ParquetStore
from crypto_trader.strategies.library import Combined, DerivConfirm, FlowConfirm, build

log = logging.getLogger(__name__)

PERIODS = {
    "dev_2022_2024": ("2022-01-01", "2025-01-01"),
    "val_2025": ("2025-01-01", "2026-01-01"),
    "test_2026_jan_sep": ("2026-01-01", "2026-10-01"),
    "post_test_2026_oct": ("2026-10-01", "2026-12-31"),
}
STRESS = {"oct_2025_crash": ("2025-10-01", "2025-11-01")}


# ---------------------------------------------------------------- specs
def make_strategy(spec):
    kind = spec[0]
    if kind == "single":
        _, name, params = spec
        return build(name, **params)
    _, label, primary, others, params = spec
    return Combined(make_strategy(primary), [make_strategy(o) for o in others], label, **params)


def S(name, **p):
    return ("single", name, p)


def COMBO(label, primary, others, **p):
    return ("combo", label, primary, others, p)


B3 = S("B3_crowding_filter")
HOURLY_SPECS = [
    S("BASE_donchian"), S("BASE_ema_trend"),
    S("A1_sweep_reversal"), S("A2_sweep_choch"), S("A3_sweep_fvg"),
    S("A2_sweep_choch", tp_mode="structure"),
    S("B1_short_squeeze"), S("B2_liq_reversal"),
    COMBO("BASE_donchian+B3filter", S("BASE_donchian"), [B3], mode="filter"),
    COMBO("A+B: A2+B3filter", S("A2_sweep_choch"), [B3], mode="filter"),
    COMBO("A+B: A2 AND derivConfirm", S("A2_sweep_choch"), [("single", DerivConfirm.name, {})], mode="and"),
    COMBO("A+B: A2 AND derivConfirm +B3", S("A2_sweep_choch"), [("single", DerivConfirm.name, {}), B3], mode="and"),
    COMBO("A+B: A2 score(deriv,B3)", S("A2_sweep_choch"), [("single", DerivConfirm.name, {}), B3], mode="score",
          threshold=1.5),
    COMBO("A+B: A3+B3filter", S("A3_sweep_fvg"), [B3], mode="filter"),
]
FLOW = ("single", FlowConfirm.name, {})
DERIV = ("single", DerivConfirm.name, {})
FLOW_SPECS = [
    S("C1_cvd_breakout"), S("C2_cvd_divergence"), S("C3_absorption"), S("C4_obi_flow"),
    S("A2_sweep_choch"),
    COMBO("A+C: A2 AND flow", S("A2_sweep_choch"), [FLOW], mode="and"),
    COMBO("B+C: C1 AND deriv", S("C1_cvd_breakout"), [DERIV], mode="and"),
    COMBO("A+B: A2 AND deriv", S("A2_sweep_choch"), [DERIV], mode="and"),
    COMBO("A+B+C: A2 AND flow AND deriv +B3", S("A2_sweep_choch"), [FLOW, DERIV, B3], mode="and"),
    COMBO("A+B+C: A2 score", S("A2_sweep_choch"), [FLOW, DERIV, B3], mode="score", threshold=1.5),
]


DERIV_COLS = ["oi_chg_4h", "oi_chg_24h", "oi_pctile_30d", "oi_chg_24h_pctile", "funding_z", "premium_z", "fut_taker_buy_frac",
              "d_funding_rate", "d_oi_usd"]


# ------------------------------------------------------------- workers
_CACHE: dict = {}


def _frame(store_root: str, market: str, interval: str, fut: str | None, kind: str) -> pl.DataFrame:
    key = (market, interval, kind)
    if key not in _CACHE:
        store = ParquetStore(store_root)
        if kind == "bars":
            _CACHE[key] = load_research_frame(store, market, interval, fut)[0]
        else:
            tb, _ = load_trade_bars(store, market, interval)
            if not tb.is_empty() and fut:
                # derivatives features are computed on the hourly frame (enough history for z-scores/percentiles)
                # and attached to 5m bars as-of the hourly bar close -> still causal
                hourly = _frame(store_root, market, "1h", fut, "bars")
                cols = [c for c in DERIV_COLS if c in hourly.columns]
                tb = asof_align(tb, hourly.select(pl.col("close_ts").alias("available_ts"), *cols), cols, "")
            _CACHE[key] = tb
    return _CACHE[key]


def _run(job) -> BacktestResult:
    store_root, settings_json, spec, market, interval, fut, kind, cost = job
    settings = Settings.model_validate_json(settings_json)
    df = _frame(store_root, market, interval, fut, kind)
    if df.is_empty():
        s = make_strategy(spec)
        return BacktestResult(s.label, s.family, market, interval, cost, settings.strategy_allocation_krw, np.array([]),
                              np.array([]), [], 0, 0, missing_inputs=["no data"], status="NO_DATA")
    return run_backtest(make_strategy(spec), df, market, interval, settings, cost)


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return "uncommitted"


def _git_dirty() -> bool:
    try:
        return bool(subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain", "src"], text=True).strip())
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------- main
def run_research(settings: Settings, store: ParquetStore, out_html: Path, interval: str = "1h",
                 markets: list[str] | None = None, fast: bool = False, workers: int | None = None) -> Path:
    t_start = datetime.now(UTC)
    markets = markets or [m for m in settings.markets if m in settings.futures_symbols]
    costs = settings.costs.scenarios
    sjson = settings.model_dump_json()
    root = str(store.root)
    jobs = []
    for m in markets:
        fut = settings.futures_symbols.get(m)
        for spec in HOURLY_SPECS:
            for c in costs.values():
                jobs.append((root, sjson, spec, m, interval, fut, "bars", c))
        for spec in FLOW_SPECS:
            for c in costs.values():
                jobs.append((root, sjson, spec, m, "5m", fut, "trades", c))
    workers = workers or min(4, os.cpu_count() or 1)
    log.info("running %d backtests on %d workers", len(jobs), workers)
    with ProcessPoolExecutor(workers) as ex:
        results: list[BacktestResult] = list(ex.map(_run, jobs, chunksize=2))
    cost_name = {v: k for k, v in costs.items()}
    base_cost = costs["base"]

    # ---- data availability
    availability = []
    frames = {}
    for m in markets:
        fut = settings.futures_symbols.get(m)
        df, av = load_research_frame(store, m, interval, fut)
        tb, av2 = load_trade_bars(store, m, "5m")
        frames[m] = df
        availability.append({**asdict(av), "trades_first": av2.trades_first, "trades_last": av2.trades_last,
                             "trade_bars_5m": av2.trade_bars, "orderbook_snapshots": av2.orderbook_snapshots,
                             "notes": av.notes + av2.notes})

    # ---- summaries
    rows = []
    for r in results:
        s = summarize(r)
        s["cost_name"] = cost_name.get(r.cost_rt, str(r.cost_rt))
        s["bar"] = r.interval
        rows.append(s)

    # ---- per-period (base cost), bootstrap, random baseline, regimes
    daily_btc = load_upbit_bars(store, "KRW-BTC", "1d")
    labels = regime_labels(daily_btc) if not daily_btc.is_empty() else None
    period_rows, stat_rows, regime_rows = [], [], []
    base_results = [r for r in results if r.cost_rt == base_cost and r.status == "OK"]
    for r in base_results:
        for pname, (a, b) in {**PERIODS, **STRESS}.items():
            period_rows.append({"strategy": r.strategy, "symbol": r.symbol, "bar": r.interval, "period": pname,
                                **slice_metrics(r, parse_utc(a), parse_utc(b))})
        dr = daily_returns(r.ts, r.equity).to_numpy()
        bs = block_bootstrap_sharpe(dr, n_boot=300 if fast else 2000)
        src = frames[r.symbol] if r.interval == interval else _frame(root, r.symbol, "5m", settings.futures_symbols.get(r.symbol), "trades")
        rnd = random_entry_baseline(src, r.trades, base_cost, n_sims=100 if fast else 500)
        stat_rows.append({"strategy": r.strategy, "symbol": r.symbol, "bar": r.interval, "trades": len(r.trades),
                          **{f"bs_{k}": v for k, v in bs.items()}, **{f"rnd_{k}": v for k, v in rnd.items()}})
        if labels is not None and r.interval == interval:
            for lab, v in trades_by_regime(r, labels).items():
                regime_rows.append({"strategy": r.strategy, "symbol": r.symbol, "regime": lab, **v})
    valid = [i for i, s in enumerate(stat_rows) if s.get("bs_p_le_0") is not None]
    pv = [stat_rows[i]["bs_p_le_0"] for i in valid]
    if pv:
        for i, h, b in zip(valid, holm(pv), benjamini_hochberg(pv), strict=True):
            stat_rows[i]["holm_p"], stat_rows[i]["bh_q"] = h, b

    # ---- buy & hold benchmarks
    bench = []
    curves: dict[str, dict] = {}
    for m in markets:
        df = frames[m]
        if df.is_empty():
            continue
        ts, eq = buy_and_hold(df, settings.strategy_allocation_krw, base_cost)
        bh = BacktestResult("BUY_HOLD", "BENCH", m, interval, base_cost, settings.strategy_allocation_krw, ts, eq, [], 0, 0,
                            start_ts=int(df["open_ts"][0]), end_ts=int(df["close_ts"][-1]))
        s = summarize(bh)
        s["cost_name"] = "base"
        s["bar"] = interval
        bench.append(s)
        for pname, (a, b) in {**PERIODS, **STRESS}.items():
            period_rows.append({"strategy": "BUY_HOLD", "symbol": m, "bar": interval, "period": pname,
                                **slice_metrics(bh, parse_utc(a), parse_utc(b))})
        curves[m] = {"BUY_HOLD": _daily_curve(ts, eq)}
        for r in base_results:
            if r.symbol == m and r.interval == interval:
                curves[m][r.strategy] = _daily_curve(r.ts, r.equity)

    # ---- walk-forward, sensitivity, look-ahead audit (BTC primary market)
    wf, sens, audit = {}, [], []
    primary = "KRW-BTC" if "KRW-BTC" in markets else markets[0]
    pdf = frames[primary]
    if not fast and not pdf.is_empty():
        wf_cfg = {
            "BASE_donchian": {"n": [24, 48, 96], "stop_atr": [1.5, 2.5]},
            "A2_sweep_choch": {"rr": [1.5, 2.0, 3.0], "stop_buf_atr": [0.1, 0.5]},
            "A3_sweep_fvg": {"rr": [1.5, 2.5], "min_gap_atr": [0.1, 0.3]},
        }
        for name, grid in wf_cfg.items():
            wf[name] = walk_forward(lambda _n=name, **p: build(_n, **p), grid, pdf, primary, interval, settings, base_cost)
        dev = pdf.filter(pl.col("open_ts") < parse_utc("2025-01-01"))
        for rr in (1.5, 2.0, 3.0):
            for buf in (0.1, 0.2, 0.5):
                for k in (6, 12, 24):
                    r = run_backtest(build("A2_sweep_choch", rr=rr, stop_buf_atr=buf, k=k), dev, primary, interval, settings, base_cost)
                    s = summarize(r)
                    sens.append({"strategy": "A2_sweep_choch", "rr": rr, "stop_buf_atr": buf, "k": k,
                                 "trades": s.get("trades"), "expectancy_r": s.get("expectancy_r"), "net_return": s.get("net_return")})
        for n in (24, 48, 96):
            for sa in (1.5, 2.0, 3.0):
                r = run_backtest(build("BASE_donchian", n=n, stop_atr=sa), dev, primary, interval, settings, base_cost)
                s = summarize(r)
                sens.append({"strategy": "BASE_donchian", "n": n, "stop_atr": sa, "trades": s.get("trades"),
                             "expectancy_r": s.get("expectancy_r"), "net_return": s.get("net_return")})
    if not pdf.is_empty():
        for spec in HOURLY_SPECS:
            res = lookahead_audit(lambda _s=spec: make_strategy(_s), pdf, n_points=8 if fast else 20)
            audit.append({"strategy": make_strategy(spec).label, "bar": interval, **res})

    meta = {
        "generated_at_utc": t_start.isoformat(), "finished_at_utc": datetime.now(UTC).isoformat(),
        "git_commit": _git_commit(), "git_dirty_src": _git_dirty(), "config_version": settings.config_version,
        "allocation_krw": settings.strategy_allocation_krw, "cost_scenarios": costs, "fee_rate": settings.costs.upbit_fee_rate,
        "max_participation": settings.costs.max_bar_participation, "interval": interval, "markets": markets,
        "periods": PERIODS, "stress": STRESS, "fast": fast,
    }
    payload = {"meta": meta, "availability": availability, "summary": rows, "benchmarks": bench, "periods": period_rows,
               "stats": stat_rows, "regimes": regime_rows, "walk_forward": wf, "sensitivity": sens, "lookahead": audit,
               "curves": curves, "trades_sample": _trade_samples(base_results)}
    out_html.parent.mkdir(parents=True, exist_ok=True)
    json_path = out_html.with_suffix(".json")
    payload = _sanitize(payload)
    json_path.write_text(json.dumps(payload, default=_json_default, indent=1, allow_nan=False))
    from crypto_trader.reports.backtest_report import render_report

    out_html.write_text(render_report(payload), encoding="utf-8")
    return out_html


def _daily_curve(ts: np.ndarray, eq: np.ndarray) -> list[list]:
    if len(ts) == 0:
        return []
    day = ts // 86_400_000
    keep = np.concatenate([day[1:] != day[:-1], [True]])
    return [[int(t), float(e)] for t, e in zip(ts[keep], eq[keep], strict=True)]


def _trade_samples(results: list[BacktestResult], per: int = 8) -> list[dict]:
    out = []
    for r in results:
        if not r.trades:
            continue
        tr = sorted(r.trades, key=lambda t: t.pnl_krw)
        pick = tr[:per // 2] + tr[-per // 2:]
        for t in pick:
            out.append({"strategy": r.strategy, "symbol": r.symbol, "bar": r.interval, **t.__dict__})
    return out


def _sanitize(o):
    if isinstance(o, dict):
        return {str(k): _sanitize(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [_sanitize(v) for v in o]
    if isinstance(o, float | np.floating):
        return float(o) if np.isfinite(o) else None
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def _json_default(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


if __name__ == "__main__":
    s = load_settings()
    print(run_research(s, ParquetStore(s.path(s.data_dir)), ROOT / "reports" / "backtest_report.html"))
