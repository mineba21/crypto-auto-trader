"""Statistical validation helpers: bootstrap, multiple-testing correction, walk-forward, regimes, look-ahead audit."""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable

import numpy as np
import pandas as pd
import polars as pl

from crypto_trader.backtest.engine import BacktestResult, run_backtest
from crypto_trader.backtest.metrics import daily_returns
from crypto_trader.core.config import Settings
from crypto_trader.strategies.base import Features, Strategy


# ----------------------------------------------------------------- bootstrap
def block_bootstrap_sharpe(returns: np.ndarray, block: int = 10, n_boot: int = 2000, seed: int = 11) -> dict:
    """Stationary-ish moving block bootstrap of daily returns -> Sharpe CI and P(Sharpe <= 0)."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 3 * block:
        return {"n_days": len(r), "sharpe": None, "ci_lo": None, "ci_hi": None, "p_le_0": None}
    rng = np.random.default_rng(seed)
    n = len(r)
    nb = math.ceil(n / block)
    sh = np.empty(n_boot)
    for k in range(n_boot):
        starts = rng.integers(0, n - block + 1, size=nb)
        sample = np.concatenate([r[s:s + block] for s in starts])[:n]
        sd = sample.std(ddof=1)
        sh[k] = sample.mean() / sd * math.sqrt(365) if sd > 0 else 0.0
    sd = r.std(ddof=1)
    obs = r.mean() / sd * math.sqrt(365) if sd > 0 else 0.0
    # p-value for H0: true Sharpe <= 0, via the centred bootstrap distribution
    centred = sh - sh.mean()
    p = float((np.sum(centred >= obs) + 1) / (n_boot + 1))
    return {"n_days": n, "sharpe": float(obs), "ci_lo": float(np.percentile(sh, 2.5)),
            "ci_hi": float(np.percentile(sh, 97.5)), "p_le_0": p}


def holm(pvals: list[float]) -> list[float]:
    m = len(pvals)
    order = np.argsort(pvals)
    adj = np.empty(m)
    running = 0.0
    for rank, idx in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvals[idx]))
        adj[idx] = running
    return adj.tolist()


def benjamini_hochberg(pvals: list[float]) -> list[float]:
    m = len(pvals)
    order = np.argsort(pvals)[::-1]
    adj = np.empty(m)
    prev = 1.0
    for rank_from_top, idx in enumerate(order):
        rank = m - rank_from_top
        prev = min(prev, pvals[idx] * m / rank)
        adj[idx] = prev
    return adj.tolist()


# ------------------------------------------------------------------ periods
def slice_metrics(res: BacktestResult, start_ms: int, end_ms: int) -> dict:
    """Metrics of a sub-period of one continuous run (segment of the same equity path)."""
    if res.status != "OK" or len(res.ts) == 0:
        return {"status": res.status}
    m = (res.ts >= start_ms) & (res.ts < end_ms)
    if m.sum() < 2:
        return {"status": "NO_DATA_IN_PERIOD"}
    eq, ts = res.equity[m], res.ts[m]
    idx = np.argmax(m)
    base = res.equity[idx - 1] if idx > 0 else res.allocation
    trades = [t for t in res.trades if start_ms <= t.entry_ts < end_ms]
    dr = daily_returns(ts, eq)
    sd = dr.std()
    peak = np.maximum.accumulate(np.concatenate([[base], eq]))
    return {
        "status": "OK", "net_return": float(eq[-1] / base - 1),
        "mdd": float(np.max(1 - np.concatenate([[base], eq]) / peak)),
        "sharpe": float(dr.mean() / sd * math.sqrt(365)) if sd and sd > 0 else 0.0,
        "trades": len(trades),
        "expectancy_r": float(np.mean([t.r_multiple for t in trades])) if trades else 0.0,
        "expectancy_krw": float(np.mean([t.pnl_krw for t in trades])) if trades else 0.0,
        "win_rate": float(np.mean([t.pnl_krw > 0 for t in trades])) if trades else 0.0,
    }


# ------------------------------------------------------------- walk-forward
def walk_forward(make: Callable[..., Strategy], grid: dict[str, list], df: pl.DataFrame, symbol: str, interval: str,
                 settings: Settings, cost_rt: float, train_months: int = 12, test_months: int = 3,
                 objective: str = "expectancy_r", min_trades: int = 10) -> dict:
    """Rolling re-optimisation. For each window pick the grid point with best train objective (subject to a
    minimum trade count), then evaluate it on the following unseen test window. Only test windows are reported."""
    ts = pd.to_datetime(df["open_ts"].to_numpy(), unit="ms", utc=True)
    start, end = ts[0].normalize(), ts[-1]
    windows = []
    cur = start
    while True:
        tr_end = cur + pd.DateOffset(months=train_months)
        te_end = tr_end + pd.DateOffset(months=test_months)
        if tr_end >= end:
            break
        windows.append((cur, tr_end, min(te_end, end)))
        cur = cur + pd.DateOffset(months=test_months)
    keys = list(grid)
    combos = [dict(zip(keys, v, strict=True)) for v in itertools.product(*[grid[k] for k in keys])]
    rows, test_trades, test_rets = [], [], []
    for tr0, tr1, te1 in windows:
        ms = lambda x: int(x.timestamp() * 1000)  # noqa: E731
        train = df.filter((pl.col("open_ts") >= ms(tr0)) & (pl.col("open_ts") < ms(tr1)))
        # test runs on train+test history so indicators are warmed up, but only test-window trades count
        hist = df.filter((pl.col("open_ts") >= ms(tr0)) & (pl.col("open_ts") < ms(te1)))
        best, best_val = None, -np.inf
        for p in combos:
            r = run_backtest(make(**p), train, symbol, interval, settings, cost_rt)
            tr = r.trades
            if len(tr) < min_trades:
                continue
            val = np.mean([t.r_multiple for t in tr]) if objective == "expectancy_r" else r.equity[-1]
            if val > best_val:
                best, best_val = p, val
        if best is None:
            rows.append({"train_start": str(tr0.date()), "test_end": str(te1.date()), "chosen": None, "test_trades": 0})
            continue
        r = run_backtest(make(**best), hist, symbol, interval, settings, cost_rt)
        sm = slice_metrics(r, ms(tr1), ms(te1))
        tt = [t for t in r.trades if ms(tr1) <= t.entry_ts < ms(te1)]
        test_trades += tt
        test_rets.append(sm.get("net_return", 0.0))
        rows.append({"train_start": str(tr0.date()), "test_start": str(tr1.date()), "test_end": str(te1.date()),
                     "chosen": best, "train_obj": float(best_val), "test_return": sm.get("net_return"),
                     "test_trades": len(tt), "test_expectancy_r": sm.get("expectancy_r")})
    oos = float(np.prod([1 + x for x in test_rets]) - 1) if test_rets else 0.0
    return {"windows": rows, "oos_compounded_return": oos, "oos_trades": len(test_trades),
            "oos_expectancy_r": float(np.mean([t.r_multiple for t in test_trades])) if test_trades else None,
            "oos_win_rate": float(np.mean([t.pnl_krw > 0 for t in test_trades])) if test_trades else None}


# --------------------------------------------------------------- regimes
def regime_labels(daily: pl.DataFrame) -> pd.Series:
    """Label each KST day from KRW-BTC daily closes: trend (bull/bear/range) x volatility (hi/lo)."""
    c = pd.Series(daily["close"].to_numpy(), index=pd.to_datetime(daily["open_ts"].to_numpy(), unit="ms", utc=True))
    sma50 = c.rolling(50).mean()
    slope = sma50.pct_change(10)
    vol = c.pct_change().rolling(30).std()
    vol_hi = vol > vol.rolling(365, min_periods=90).median()
    trend = np.where((c > sma50) & (slope > 0.02), "bull", np.where((c < sma50) & (slope < -0.02), "bear", "range"))
    lab = pd.Series([f"{t}/{'hi' if v else 'lo'}vol" for t, v in zip(trend, vol_hi, strict=True)], index=c.index)
    # label for day d must only use info up to d-1 close -> shift by one day
    return lab.shift(1).dropna()


def trades_by_regime(res: BacktestResult, labels: pd.Series) -> dict[str, dict]:
    if not res.trades:
        return {}
    idx = labels.index
    out: dict[str, list] = {}
    for t in res.trades:
        d = pd.Timestamp(t.entry_ts, unit="ms", tz="UTC").normalize()
        pos = idx.searchsorted(d, side="right") - 1
        lab = labels.iloc[pos] if pos >= 0 else "unknown"
        out.setdefault(lab, []).append(t)
    return {k: {"trades": len(v), "expectancy_r": float(np.mean([t.r_multiple for t in v])),
                "win_rate": float(np.mean([t.pnl_krw > 0 for t in v])), "pnl_krw": float(sum(t.pnl_krw for t in v))}
            for k, v in sorted(out.items())}


# ----------------------------------------------------------- look-ahead
def lookahead_audit(make: Callable[[], Strategy], df: pl.DataFrame, n_points: int = 25, seed: int = 3) -> dict:
    """Decision at bar i computed on the full frame must equal the decision computed on df[:i+1].

    Any difference means a feature used information from after bar i.
    """
    full = Features.from_frame(df)
    s_full = make()
    if s_full.missing_inputs(full):
        return {"checked": 0, "mismatches": 0, "status": "SKIPPED (missing inputs)"}
    s_full.prepare(full)
    rng = np.random.default_rng(seed)
    lo = max(s_full.warmup + 5, 300)
    if df.height <= lo + 1:
        return {"checked": 0, "mismatches": 0, "status": "SKIPPED (too short)"}
    # bias the sample towards bars where the full-data run produced a signal
    sig_idx = [i for i in range(lo, df.height) if s_full.on_bar(i, full, "X", None)]
    pts = list(rng.choice(sig_idx, size=min(n_points, len(sig_idx)), replace=False)) if sig_idx else []
    pts += list(rng.integers(lo, df.height, size=max(5, n_points - len(pts))))
    mism = []
    for i in pts:
        i = int(i)
        part = Features.from_frame(df.head(i + 1))
        s = make()
        s.prepare(part)
        a = [(x.action, round(x.stop_price or 0, 6)) for x in s_full.on_bar(i, full, "X", None)]
        b = [(x.action, round(x.stop_price or 0, 6)) for x in s.on_bar(i, part, "X", None)]
        if a != b:
            mism.append({"i": i, "full": a, "prefix": b})
    return {"checked": len(pts), "mismatches": len(mism), "examples": mism[:3],
            "status": "PASS" if not mism else "FAIL"}
