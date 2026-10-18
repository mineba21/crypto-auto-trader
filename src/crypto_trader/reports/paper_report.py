"""PAPER vs backtest comparison report (4-week run acceptance checks).

Per instance (strategy x market):
* uptime        — minutes with an equity record / elapsed minutes
* signal parity — for every bar the paper engine evaluated while flat, the backtest decision on the same Upbit
                  candles must be identical (same code path => mismatches point at data or timing problems)
* slippage      — paper fill price vs the decision reference price, in bps, against the backtest assumption
* spread        — average quoted spread from the collector's orderbook snapshots
* gaps / kill   — recorded data gaps and kill-switch events in the window
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

from crypto_trader.collectors.binance.live_feed import BinanceLiveDerivatives
from crypto_trader.collectors.jobs import bars_to_df
from crypto_trader.collectors.upbit.rest import UpbitRest
from crypto_trader.core.config import Settings
from crypto_trader.core.timeutils import INTERVAL_MS, fmt_kst
from crypto_trader.features.derivatives import add_derivative_features
from crypto_trader.features.orderflow import orderbook_frame_obi
from crypto_trader.reports.html import esc, page, table
from crypto_trader.storage.ops_db import OpsDB
from crypto_trader.storage.parquet_store import ParquetStore
from crypto_trader.strategies.base import Features
from crypto_trader.strategies.library import build

DERIV = ("oi_chg_4h", "oi_chg_24h", "funding_z", "oi_chg_24h_pctile", "fut_taker_buy_frac")


def signal_parity(evals: list[dict], acts: list[dict], frame: pl.DataFrame, strategy_name: str, symbol: str) -> dict:
    """Compare paper decisions with the backtest decision function on the same candles (flat bars only)."""
    if frame.is_empty() or not evals:
        return {"checked": 0, "match": 0, "mismatch": [], "skipped": len(evals)}
    s = build(strategy_name)
    f = Features.from_frame(frame)
    if s.missing_inputs(f):
        return {"checked": 0, "match": 0, "mismatch": [], "skipped": len(evals), "note": "missing inputs"}
    s.prepare(f)
    idx = {int(t): i for i, t in enumerate(f.ts)}
    paper_by_bar: dict[int, set] = {}
    for a in acts:
        if a["action"] in ("ENTER", "BLOCK") and a["bar_ts"] is not None:
            paper_by_bar.setdefault(int(a["bar_ts"]), set()).add(a["action"])
    checked = match = skipped = 0
    mism = []
    for e in evals:
        data = json.loads(e["data"] or "{}")
        if not e["accepted"] or data.get("pos_open") or e["bar_ts"] is None:
            skipped += 1
            continue
        i = idx.get(int(e["bar_ts"]))
        if i is None:
            skipped += 1
            continue
        bt = {x.action for x in s.on_bar(i, f, symbol, None)}
        pp = paper_by_bar.get(int(e["bar_ts"]), set())
        checked += 1
        if bt == pp:
            match += 1
        else:
            mism.append({"bar": fmt_kst(int(e["bar_ts"])), "paper": ",".join(sorted(pp)) or "-",
                         "backtest": ",".join(sorted(bt)) or "-"})
    return {"checked": checked, "match": match, "mismatch": mism, "skipped": skipped}


def fill_slippage_bps(fills: list[dict], evals: list[dict]) -> list[float]:
    """Adverse move of each paper fill vs the reference price of the latest decision before it (bps, + = cost)."""
    refs = sorted((e["decision_ts"], json.loads(e["data"] or "{}").get("ref")) for e in evals)
    out = []
    for fl in fills:
        prior = [r for t, r in refs if t <= fl["ts"] and r]
        if not prior:
            continue
        ref = prior[-1]
        bps = (fl["price"] / ref - 1) * 1e4
        out.append(bps if fl["side"] == "bid" else -bps)
    return out


async def build_paper_report(settings: Settings, start_ms: int, end_ms: int, out: Path) -> tuple[Path, str]:
    store = ParquetStore(settings.path(settings.data_dir))
    db_path = settings.path(settings.ops_db)
    modes = OpsDB(db_path, "PAPER").modes("PAPER:")
    rest = UpbitRest()
    candles: dict[str, pl.DataFrame] = {}
    feeds: dict[str, BinanceLiveDerivatives] = {}
    rows, mism_rows = [], []
    fee = settings.costs.upbit_fee_rate
    assumed_side_bps = (settings.costs.scenarios["base"] / 2 - fee) * 1e4
    try:
        for mode in modes:
            _, strat, sym = mode.split(":", 2)
            db = OpsDB(db_path, mode)
            sigs = db.signals_between(start_ms, end_ms)
            evals = [x for x in sigs if x["action"] == "EVAL"]
            acts = [x for x in sigs if x["action"] != "EVAL"]
            fills = db.fills_between(start_ms, end_ms)
            eq = db.equity_between(start_ms, end_ms)
            first = eq[0]["ts"] if eq else end_ms
            minutes = max(1, (end_ms - max(start_ms, first)) // 60_000)
            uptime = len(eq) / minutes if eq else 0.0
            if sym not in candles:
                step = INTERVAL_MS["1h"]
                bars = await rest.candles(sym, "1h", start_ms - 2400 * step, end_ms)
                candles[sym] = bars_to_df(bars)
            frame = candles[sym]
            base_name = strat.split("(")[0]
            needs = bool(set(build(base_name).required_columns) & set(DERIV))
            fut = settings.futures_symbols.get(sym)
            if needs and fut and not frame.is_empty():
                if fut not in feeds:
                    feeds[fut] = BinanceLiveDerivatives(store, fut)
                    await feeds[fut].refresh()
                fd = feeds[fut]
                frame = add_derivative_features(frame, fd.metrics, fd.funding, fd.premium, fd.fut_klines, INTERVAL_MS["1h"])
            par = signal_parity(evals, acts, frame, base_name, sym)
            slip = fill_slippage_bps(fills, evals)
            ks_events = [e for e in db.events(500) if e["component"] == "kill_switch" and start_ms <= e["ts"] < end_ms]
            last_eq = eq[-1]["equity_krw"] if eq else None
            rows.append({
                "instance": f"{strat} / {sym}", "uptime": uptime, "evals": len(evals),
                "parity": f"{par['match']}/{par['checked']}", "parity_ok": par["checked"] > 0 and par["checked"] == par["match"],  # no evaluated bars = not verified
                "enters": sum(1 for a in acts if a["action"] == "ENTER" and a["accepted"]),
                "blocked": sum(1 for a in acts if not a["accepted"]), "fills": len(fills),
                "slip_bps": float(np.mean(slip)) if slip else None, "slip_assumed_bps": assumed_side_bps,
                "ret": (last_eq / settings.strategy_allocation_krw - 1) if last_eq else None,
                "kill_events": len(ks_events),
            })
            for m in par["mismatch"][:20]:
                mism_rows.append({"instance": f"{strat} / {sym}", **m})
        spreads, gaps = [], []
        for sym in sorted({m.split(":", 2)[2] for m in modes}):
            ob = store.read("orderbook", "upbit", sym, start_ms=start_ms, end_ms=end_ms, dedupe=False)
            if not ob.is_empty():
                o = orderbook_frame_obi(ob, depth=5)
                spreads.append({"symbol": sym, "snapshots": ob.height, "spread_bps_mean": float(o["spread_bps"].mean()),
                                "spread_bps_p95": float(o["spread_bps"].quantile(0.95))})
            g = store.read("gaps", "upbit", sym, start_ms=start_ms, end_ms=end_ms)
            for r in g.iter_rows(named=True):
                gaps.append({"symbol": sym, "stream": r.get("stream"), "start": fmt_kst(r["event_ts"]),
                             "end": fmt_kst(r.get("end_ts")), "reason": r.get("reason")})
    finally:
        await rest.close()
        for f in feeds.values():
            await f.close()

    n = len(rows)
    up = float(np.mean([r["uptime"] for r in rows])) if rows else 0.0
    par_ok = sum(1 for r in rows if r["parity_ok"])
    slips = [r["slip_bps"] for r in rows if r["slip_bps"] is not None]
    headline = (f"instances={n} uptime(avg)={up:.1%} parity_ok={par_ok}/{n} "
                f"slippage(avg)={np.mean(slips):.1f}bps (assumed {assumed_side_bps:.1f})" if slips else
                f"instances={n} uptime(avg)={up:.1%} parity_ok={par_ok}/{n} slippage=n/a (no fills)")
    checks = [
        ("평균 가동률 ≥ 99%", up >= 0.99),
        ("모든 인스턴스 신호 일치", n > 0 and par_ok == n),
        ("실측 슬리피지 ≤ 가정치", bool(slips) and float(np.mean(slips)) <= assumed_side_bps),
        ("원인 불명 Kill Switch 0건", all(r["kill_events"] == 0 for r in rows)),
    ]
    body = [f"<h1>PAPER 운영 리포트</h1><p class='sub'>{esc(fmt_kst(start_ms))} → {esc(fmt_kst(end_ms))} · {esc(headline)}</p>",
            "<h2>합격 기준</h2><ul>" + "".join(
                f"<li><span class='badge {'b-ok' if ok else 'b-bad'}'>{'PASS' if ok else 'FAIL'}</span> {esc(t)}</li>"
                for t, ok in checks) + "</ul>",
            "<p class='note'>신호 일치: 모의매매가 포지션 없이 평가한 각 봉에서, 같은 Upbit 캔들로 백테스트 결정 함수를 돌린 결과와 "
            "ENTER/BLOCK 이 같아야 합니다. 슬리피지: 체결가 vs 결정 시점 기준가(편도, bps). 가정치는 왕복비용/2 − 수수료.</p>",
            "<h2>인스턴스</h2>" + table(rows, [
                ("instance", "instance", "text"), ("uptime", "uptime", "pctn"), ("evals", "evaluated bars", "num"),
                ("parity", "parity", "text"), ("enters", "entries", "num"), ("blocked", "blocked/rejected", "num"),
                ("fills", "fills", "num"), ("slip_bps", "slip bps", "num"), ("slip_assumed_bps", "assumed bps", "num"),
                ("ret", "return", "pct"), ("kill_events", "kill events", "num")]),
            "<h2>신호 불일치</h2>" + table(mism_rows, [("instance", "instance", "text"), ("bar", "bar", "text"),
                                                   ("paper", "paper", "text"), ("backtest", "backtest", "text")]),
            "<h2>호가 스프레드 (수집기)</h2>" + table(spreads, [("symbol", "symbol", "text"), ("snapshots", "snapshots", "num"),
                                                    ("spread_bps_mean", "mean bps", "num"), ("spread_bps_p95", "p95 bps", "num")]),
            "<h2>데이터 공백</h2>" + table(gaps, [("symbol", "symbol", "text"), ("stream", "stream", "text"),
                                             ("start", "start", "text"), ("end", "end", "text"), ("reason", "reason", "text")])]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page("Paper Report", "".join(body), "paper trading vs backtest acceptance report"), encoding="utf-8")
    return out, headline
