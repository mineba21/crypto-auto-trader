"""Performance metrics. All returns are net of fees and slippage unless prefixed ``gross_``."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from crypto_trader.backtest.engine import BacktestResult
from crypto_trader.execution.core import ClosedTrade


def daily_returns(ts: np.ndarray, equity: np.ndarray) -> pd.Series:
    if len(ts) == 0:
        return pd.Series(dtype=float)
    s = pd.Series(equity, index=pd.to_datetime(ts, unit="ms", utc=True).tz_convert("Asia/Seoul"))
    d = s.resample("1D").last().dropna()
    return d.pct_change().dropna()


def max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    return float(np.max(1 - equity / peak))


def drawdown_series(equity: np.ndarray) -> np.ndarray:
    peak = np.maximum.accumulate(equity)
    return equity / peak - 1


def max_consecutive_losses(trades: list[ClosedTrade]) -> int:
    best = cur = 0
    for t in trades:
        cur = cur + 1 if t.pnl_krw < 0 else 0
        best = max(best, cur)
    return best


def summarize(res: BacktestResult) -> dict:
    out: dict = {"strategy": res.strategy, "family": res.family, "symbol": res.symbol, "interval": res.interval,
                 "cost_rt": res.cost_rt, "status": res.status, "missing_inputs": ",".join(res.missing_inputs)}
    if res.status != "OK" or len(res.equity) == 0:
        return out
    alloc = res.allocation
    tr = res.trades
    net = res.equity[-1] / alloc - 1
    gross_pnl = sum(t.pnl_krw + t.fees_krw + t.slippage_krw for t in tr)
    dr = daily_returns(res.ts, res.equity)
    sd = dr.std()
    downside = dr[dr < 0].std()
    wins = [t.pnl_krw for t in tr if t.pnl_krw > 0]
    losses = [t.pnl_krw for t in tr if t.pnl_krw <= 0]
    years = max(1e-9, (res.end_ts - res.start_ts) / (365.25 * 86_400_000))
    q05 = dr.quantile(0.05) if len(dr) > 20 else np.nan
    cvar = dr[dr <= q05].mean() if len(dr) > 20 else np.nan
    notional = sum(t.entry_price * t.volume + t.exit_price * t.volume for t in tr)
    out.update({
        "start": pd.to_datetime(res.start_ts, unit="ms", utc=True).strftime("%Y-%m-%d"),
        "end": pd.to_datetime(res.end_ts, unit="ms", utc=True).strftime("%Y-%m-%d"),
        "years": years,
        "trades": len(tr),
        "total_return_gross": gross_pnl / alloc,
        "net_return": net,
        "net_pnl_krw": res.equity[-1] - alloc,
        "cagr": (1 + net) ** (1 / years) - 1 if net > -1 else -1.0,
        "mdd": max_drawdown(res.equity),
        "sharpe": float(dr.mean() / sd * math.sqrt(365)) if sd and sd > 0 else 0.0,
        "sortino": float(dr.mean() / downside * math.sqrt(365)) if downside and downside > 0 else 0.0,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (float("inf") if wins else 0.0),
        "win_rate": len(wins) / len(tr) if tr else 0.0,
        "avg_win_krw": float(np.mean(wins)) if wins else 0.0,
        "avg_loss_krw": float(np.mean(losses)) if losses else 0.0,
        "expectancy_krw": float(np.mean([t.pnl_krw for t in tr])) if tr else 0.0,
        "expectancy_r": float(np.mean([t.r_multiple for t in tr])) if tr else 0.0,
        "avg_mae": float(np.mean([t.mae for t in tr])) if tr else 0.0,
        "avg_mfe": float(np.mean([t.mfe for t in tr])) if tr else 0.0,
        "cvar95_daily": float(cvar) if cvar == cvar else None,
        "max_consec_losses": max_consecutive_losses(tr),
        "avg_hold_h": float(np.mean([(t.exit_ts - t.entry_ts) / 3_600_000 for t in tr])) if tr else 0.0,
        "turnover_x": notional / alloc,
        "fees_krw": res.fees,
        "slippage_krw": res.slippage,
        "rejections": len(res.rejections),
        "blocked_signals": res.blocked,
        "kill_switch": ",".join(r["code"] for r in res.kill_switch.get("reasons", [])),
    })
    return out


def monthly_pnl(res: BacktestResult) -> pd.Series:
    if len(res.ts) == 0:
        return pd.Series(dtype=float)
    s = pd.Series(res.equity, index=pd.to_datetime(res.ts, unit="ms", utc=True).tz_convert("Asia/Seoul"))
    m = s.resample("ME").last()
    return m.diff().fillna(m.iloc[0] - res.allocation)


def trade_frame(res: BacktestResult) -> pd.DataFrame:
    return pd.DataFrame([t.__dict__ for t in res.trades])
