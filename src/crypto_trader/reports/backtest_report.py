"""Backtest HTML report (10 required sections) rendered from the research payload (also saved as JSON)."""

from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd

from crypto_trader.core.timeutils import fmt_kst
from crypto_trader.reports.html import esc, fmt, heat_cell, line_chart, page, table

CHART_SERIES = ["BUY_HOLD", "BASE_ema_trend", "BASE_donchian", "A2_sweep_choch", "A3_sweep_fvg", "B1_short_squeeze",
                "A+B: A2+B3filter", "B2_liq_reversal"]


def _d(ts):
    return pd.to_datetime(ts, unit="ms", utc=True).strftime("%Y-%m-%d") if ts else "-"


def render_report(p: dict) -> str:
    meta = p["meta"]
    summ = [r for r in p["summary"]]
    base = [r for r in summ if r.get("cost_name") == "base"]
    ok_base = [r for r in base if r.get("status") == "OK"]
    body = []
    body.append("<h1>Crypto Auto Trader — 백테스트 리포트</h1>")
    body.append(f"<p class='sub'>Upbit KRW 현물 · Long-only · 실행가격=Upbit, 해외 선물=필터 전용 · 생성 {esc(meta['generated_at_utc'])} UTC</p>")
    body.append(
        "<div class='grid'>"
        f"<div class='tile'><div class='k'>Git commit</div><div class='v'>{esc(meta['git_commit'])}{' *' if meta['git_dirty_src'] else ''}</div></div>"
        f"<div class='tile'><div class='k'>Config version</div><div class='v'>{esc(meta['config_version'])}</div></div>"
        f"<div class='tile'><div class='k'>전략 할당금</div><div class='v'>{meta['allocation_krw']:,.0f} KRW</div></div>"
        f"<div class='tile'><div class='k'>왕복 비용 (낙관/기본/스트레스)</div><div class='v'>{' / '.join(f'{v * 100:.2f}%' for v in meta['cost_scenarios'].values())}</div></div>"
        f"<div class='tile'><div class='k'>Upbit 수수료(편도)</div><div class='v'>{meta['fee_rate'] * 100:.3f}%</div></div>"
        f"<div class='tile'><div class='k'>체결 참여율 상한</div><div class='v'>{meta['max_participation'] * 100:.0f}% of bar</div></div>"
        "</div>")
    if meta.get("git_dirty_src"):
        body.append("<p class='note'>* 리포트 생성 시점에 src/ 에 커밋되지 않은 변경이 있었습니다. 재현하려면 해당 커밋 이후 변경분이 필요합니다.</p>")
    if meta.get("fast"):
        body.append("<p class='note'>FAST 모드: 부트스트랩 반복 축소, Walk-forward/민감도 생략.</p>")

    # 1 executive summary
    body.append("<h2>1. Executive Summary</h2>")
    body.append(_exec_summary(p, ok_base))

    # 2 data
    body.append("<h2>2. 데이터 기간 및 품질</h2>")
    av = p["availability"]
    body.append(table([{**a, "upbit_first": _d(a["upbit_first"]), "upbit_last": _d(a["upbit_last"]),
                        "derivatives_first": _d(a["derivatives_first"]), "derivatives_last": _d(a["derivatives_last"]),
                        "trades_first": fmt_kst(a["trades_first"]), "trades_last": fmt_kst(a["trades_last"]),
                        "notes": "; ".join(a["notes"])} for a in av],
                      [("market", "market", "text"), ("upbit_bars", f"Upbit {meta['interval']} bars", "num"),
                       ("upbit_first", "Upbit 시작", "text"), ("upbit_last", "Upbit 끝", "text"),
                       ("derivatives_first", "Binance OI 시작", "text"), ("derivatives_last", "Binance OI 끝", "text"),
                       ("trades_first", "Upbit 체결 시작", "text"), ("trades_last", "Upbit 체결 끝", "text"),
                       ("trade_bars_5m", "5m 체결봉", "num"), ("orderbook_snapshots", "호가 스냅샷", "num"),
                       ("notes", "비고", "text")]))
    body.append("<p class='note'>OHLCV 전략(A, BASE)과 파생 필터(B)는 2022-01 이후 전 구간에서 평가됩니다. "
                "체결 기반 CVD(C)는 Upbit REST가 제공하는 최근 약 7일 + 실시간 수집분만 존재하므로 표본이 매우 작습니다. "
                "과거 호가(OBI)와 과거 강제청산 이벤트는 공개 아카이브에 없어 생성하지 않았습니다 (C4, B2-청산확인 = 백테스트 불가).</p>")

    # 3 comparison
    body.append("<h2>3. 전략별 성과 비교 (기본 비용 0.30%, 전체 구간)</h2>")
    cols = [("strategy", "strategy", "text"), ("family", "fam", "text"), ("symbol", "market", "text"), ("bar", "bar", "text"),
            ("trades", "trades", "num"), ("total_return_gross", "gross", "pct"), ("net_return", "net", "pct"),
            ("net_pnl_krw", "net KRW", "krw"), ("mdd", "MDD", "pct"), ("sharpe", "Sharpe", "num"), ("sortino", "Sortino", "num"),
            ("profit_factor", "PF", "num"), ("win_rate", "win", "pct"), ("avg_win_krw", "avg win", "krw"),
            ("avg_loss_krw", "avg loss", "krw"), ("expectancy_krw", "E[KRW]", "krw"), ("expectancy_r", "E[R]", "r"),
            ("avg_mae", "MAE", "pct"), ("avg_mfe", "MFE", "pct"), ("cvar95_daily", "CVaR95 d", "pct"),
            ("max_consec_losses", "max L-streak", "num"), ("avg_hold_h", "hold h", "num"), ("turnover_x", "turnover x", "num"),
            ("fees_krw", "fees", "krw"), ("slippage_krw", "slip", "krw"), ("blocked_signals", "blocked", "num"),
            ("kill_switch", "kill", "text")]
    rows = sorted(ok_base + p["benchmarks"], key=lambda r: (r["symbol"], r.get("bar", ""), -(r.get("net_return") or -9)))
    body.append(table(rows, cols))
    nod = [r for r in base if r.get("status") != "OK"]
    if nod:
        body.append("<h3>데이터 부족으로 실행되지 않은 조합 (BLOCKED by data)</h3>")
        body.append(table(nod, [("strategy", "strategy", "text"), ("symbol", "market", "text"), ("bar", "bar", "text"),
                                ("missing_inputs", "missing", "text")]))
    body.append("<h3>통계 검증 (블록 부트스트랩 Sharpe, 무작위 진입 기준, 다중검정 보정)</h3>")
    body.append("<p class='note'>p(Sharpe≤0): 10일 블록 부트스트랩. holm / BH-q: 전 전략×시장 동시 검정 보정. "
                "rnd p: 같은 거래 수·보유기간의 무작위 진입 500회 대비 평균 거래수익이 더 클 확률(단측).</p>")
    body.append(table(sorted(p["stats"], key=lambda r: (r.get("holm_p") or 1)),
                      [("strategy", "strategy", "text"), ("symbol", "market", "text"), ("bar", "bar", "text"),
                       ("trades", "trades", "num"), ("bs_sharpe", "Sharpe", "num"), ("bs_ci_lo", "CI lo", "num"),
                       ("bs_ci_hi", "CI hi", "num"), ("bs_p_le_0", "p(S≤0)", "p"), ("holm_p", "holm", "p"), ("bh_q", "BH q", "p"),
                       ("rnd_strategy_mean", "trade mean", "pct"), ("rnd_random_mean", "random mean", "pct"),
                       ("rnd_p_value", "rnd p", "p")]))

    # 4/5 curves
    body.append("<h2>4. 누적 수익(평가금액) 그래프</h2>")
    body.append("<p class='sub'>기본 비용, 할당금 기준 평가금액(KRW). 최대 8개 시리즈만 표시 — 전체 수치는 3절 표.</p>")
    dd_blocks = []
    for m, curves in p["curves"].items():
        sel = [(n, curves[n]) for n in CHART_SERIES if n in curves]
        body.append(f"<h3>{esc(m)}</h3>" + line_chart(sel, "krw", baseline=meta["allocation_krw"], title=f"{m} equity"))
        dd = []
        for n, pts in sel:
            if not pts:
                continue
            v = np.array([x[1] for x in pts])
            peak = np.maximum.accumulate(v)
            dd.append((n, [[pts[i][0], float(v[i] / peak[i] - 1)] for i in range(len(pts))]))
        dd_blocks.append(f"<h3>{esc(m)}</h3>" + line_chart(dd, "pct", height=200, baseline=0.0, title=f"{m} drawdown"))
    body.append("<h2>5. Drawdown 그래프</h2>" + "".join(dd_blocks))

    # 6 monthly
    body.append("<h2>6. 월별 수익률 (평가금액 기준, 기본 비용)</h2>")
    body.append(_monthly(p))

    # 7 trades
    body.append("<h2>7. 거래 내역 및 사례 분석</h2>")
    body.append("<p class='sub'>전략×시장별 최대 손실 4건과 최대 이익 4건 (기본 비용).</p>")
    tr = p.get("trades_sample", [])
    for t in tr:
        t["entry"] = fmt_kst(t["entry_ts"])
        t["exit"] = fmt_kst(t["exit_ts"])
    body.append(f"<details><summary>거래 사례 펼치기 ({len(tr)}건)</summary>" + table(
        tr, [("strategy", "strategy", "text"), ("symbol", "market", "text"), ("entry", "entry", "text"), ("exit", "exit", "text"),
             ("entry_price", "entry px", "num"), ("exit_price", "exit px", "num"), ("pnl_krw", "PnL", "krw"),
             ("r_multiple", "R", "r"), ("mae", "MAE", "pct"), ("mfe", "MFE", "pct"), ("exit_reason", "exit", "text"),
             ("entry_reason", "entry reason", "text")]) + "</details>")
    body.append("<h3>시장 국면별 성과 (KRW-BTC 일봉 기준 국면, 전일 정보로 라벨)</h3>")
    reg = sorted(p["regimes"], key=lambda r: (r["strategy"], r["symbol"], r["regime"]))
    body.append("<details><summary>국면별 표 펼치기</summary>" + table(
        reg, [("strategy", "strategy", "text"), ("symbol", "market", "text"), ("regime", "regime", "text"),
              ("trades", "trades", "num"), ("expectancy_r", "E[R]", "r"), ("win_rate", "win", "pct"), ("pnl_krw", "PnL", "krw")])
        + "</details>")

    # 8 costs
    body.append("<h2>8. 수수료·슬리피지 영향</h2>")
    body.append(_cost_table(summ, meta['allocation_krw']))

    # 9 OOS
    body.append("<h2>9. Out-of-Sample / 기간별 성과</h2>")
    body.append("<p class='note'>파라미터는 기본값 고정(사전 튜닝 없음). 개발 2022–2024 / 검증 2025 / 최종 테스트 2026-01~09. "
                "같은 연속 실행의 구간별 성과입니다. 2025-10 급락 구간은 스트레스 시나리오로 별도 표시.</p>")
    body.append(_period_table(p["periods"]))
    body.append("<h3>Walk-forward (12개월 학습 → 3개월 테스트, KRW-BTC)</h3>")
    wf = p.get("walk_forward") or {}
    if not wf:
        body.append("<p class='muted'>(생략됨)</p>")
    for name, w in wf.items():
        body.append(f"<p><b>{esc(name)}</b>: OOS 누적 {fmt(w['oos_compounded_return'], 'pct')}, OOS 거래 {w['oos_trades']}, "
                    f"OOS E[R] {fmt(w['oos_expectancy_r'], 'r')}, 승률 {fmt(w['oos_win_rate'], 'pct')}</p>")
        body.append("<details><summary>창별 선택 파라미터</summary>" + table(
            [{**x, "chosen": str(x.get("chosen"))} for x in w["windows"]],
            [("train_start", "train start", "text"), ("test_start", "test start", "text"), ("test_end", "test end", "text"),
             ("chosen", "chosen params", "text"), ("train_obj", "train E[R]", "r"), ("test_return", "test ret", "pct"),
             ("test_trades", "test trades", "num"), ("test_expectancy_r", "test E[R]", "r")]) + "</details>")
    body.append("<h3>파라미터 민감도 (KRW-BTC, 개발 구간만)</h3>")
    sens = p.get("sensitivity") or []
    body.append(table([s for s in sens if s["strategy"] == "A2_sweep_choch"],
                      [("rr", "rr", "num"), ("stop_buf_atr", "stop buf ATR", "num"), ("k", "k", "num"), ("trades", "trades", "num"),
                       ("expectancy_r", "E[R]", "r"), ("net_return", "net", "pct")]) if sens else "<p class='muted'>(생략됨)</p>")
    if sens:
        body.append(table([s for s in sens if s["strategy"] == "BASE_donchian"],
                          [("n", "n", "num"), ("stop_atr", "stop ATR", "num"), ("trades", "trades", "num"),
                           ("expectancy_r", "E[R]", "r"), ("net_return", "net", "pct")]))
    body.append("<h3>Look-ahead 감사 (전체 데이터 vs 해당 시점까지 잘라낸 데이터의 판단 일치 여부)</h3>")
    body.append(table(p.get("lookahead", []), [("strategy", "strategy", "text"), ("checked", "points", "num"),
                                               ("mismatches", "mismatch", "num"), ("status", "status", "text")]))

    # 10 failures
    body.append("<h2>10. 실패 원인 및 개선 후보</h2>")
    body.append(_failures(p, ok_base))
    body.append("<p class='muted'>재현: <code>uv run cat backtest</code> (같은 커밋·config·data 범위). "
                "원자료 JSON: 같은 폴더의 backtest_report.json.</p>")
    return page("Backtest Report", "".join(body), "Upbit KRW spot long-only strategy research report")


def _exec_summary(p: dict, ok_base: list[dict]) -> str:
    hourly = [r for r in ok_base if r.get("bar") == p["meta"]["interval"]]
    stats = {(s["strategy"], s["symbol"], s["bar"]): s for s in p["stats"]}
    bench = {b["symbol"]: b for b in p["benchmarks"]}
    sig = [r for r in hourly if (stats.get((r["strategy"], r["symbol"], r["bar"])) or {}).get("holm_p") is not None
           and stats[(r["strategy"], r["symbol"], r["bar"])]["holm_p"] < 0.05]
    pos = [r for r in hourly if (r.get("net_return") or 0) > 0 and (r.get("expectancy_r") or 0) > 0]
    beat = [r for r in hourly if r["symbol"] in bench and (r.get("sharpe") or 0) > (bench[r["symbol"]].get("sharpe") or 0)]
    li = [f"<li>시간봉 전략×시장 {len(hourly)}개 중 비용 차감 후 순수익·양의 기대값: <b>{len(pos)}</b>개</li>",
          f"<li>다중검정(Holm) 보정 후 Sharpe>0 이 5% 유의한 조합: <b>{len(sig)}</b>개 {'(' + ', '.join(esc(r['strategy'] + '/' + r['symbol']) for r in sig) + ')' if sig else ''}</li>",
          f"<li>Buy &amp; Hold 보다 Sharpe 가 높은 조합: <b>{len(beat)}</b>개</li>"]
    top = sorted(hourly, key=lambda r: -(r.get("sharpe") or -9))[:5]
    li.append("<li>Sharpe 상위: " + ", ".join(f"{esc(r['strategy'])}/{esc(r['symbol'])} (net {r['net_return'] * 100:.1f}%, "
                                               f"MDD {r['mdd'] * 100:.1f}%, E[R] {r['expectancy_r']:+.2f})" for r in top) + "</li>")
    fam = defaultdict(list)
    for r in hourly:
        fam[r["family"]].append(r.get("expectancy_r") or 0)
    li.append("<li>계열별 평균 E[R]: " + ", ".join(f"{k} {np.mean(v):+.3f}" for k, v in sorted(fam.items())) + "</li>")
    return ("<ul>" + "".join(li) + "</ul><p class='note'>이 수치는 과거 데이터 시뮬레이션이며 실거래 수익률이 아닙니다. "
            "실거래 성과는 검증되지 않았습니다. 6개월 1억 원 목표는 어떤 전략의 기대값으로도 뒷받침되지 않습니다.</p>")


def _monthly(p: dict) -> str:
    out = []
    for m, curves in p["curves"].items():
        rows = []
        for name in CHART_SERIES:
            pts = curves.get(name)
            if not pts:
                continue
            s = pd.Series([x[1] for x in pts], index=pd.to_datetime([x[0] for x in pts], unit="ms", utc=True).tz_convert("Asia/Seoul"))
            mo = s.resample("ME").last()
            ret = mo.pct_change()
            ret.iloc[0] = mo.iloc[0] / p["meta"]["allocation_krw"] - 1
            rows.append((name, ret))
        if not rows:
            continue
        months = rows[0][1].index
        yrs = sorted({d.year for d in months})
        html_rows = []
        for name, ret in rows:
            for y in yrs:
                cells = [heat_cell(ret.get(pd.Timestamp(f"{y}-{mm:02d}-01", tz="Asia/Seoul") + pd.offsets.MonthEnd(0)), 0.15)
                         for mm in range(1, 13)]
                yr = ret[ret.index.year == y]
                tot = float(np.prod(1 + yr.dropna()) - 1) if len(yr) else None
                html_rows.append(f"<tr><td>{esc(name)}</td><td>{y}</td>{''.join(cells)}{heat_cell(tot, 0.5)}</tr>")
        head = "<tr><th>strategy</th><th>year</th>" + "".join(f"<th>{i}</th>" for i in range(1, 13)) + "<th>year</th></tr>"
        out.append(f"<h3>{esc(m)}</h3><details><summary>월별 표 펼치기</summary><div class='tbl'><table><thead>{head}</thead>"
                   f"<tbody>{''.join(html_rows)}</tbody></table></div></details>")
    return "".join(out)


def _cost_table(summ: list[dict], alloc: float) -> str:
    by = defaultdict(dict)
    for r in summ:
        if r.get("status") != "OK":
            continue
        k = (r["strategy"], r["symbol"], r["bar"])
        by[k][r["cost_name"]] = r
    rows = []
    for (s, m, b), d in sorted(by.items()):
        if "base" not in d:
            continue
        rows.append({"strategy": s, "symbol": m, "bar": b, "trades": d["base"]["trades"],
                     "opt": d.get("optimistic", {}).get("net_return"), "base": d["base"]["net_return"],
                     "stress": d.get("stress", {}).get("net_return"), "gross": d["base"]["total_return_gross"],
                     "fees": d["base"]["fees_krw"], "slip": d["base"]["slippage_krw"],
                     "cost_share": (d["base"]["fees_krw"] + d["base"]["slippage_krw"]) / max(1.0, abs(d["base"]["total_return_gross"] * alloc)) if d["base"]["total_return_gross"] else None})
    return ("<p class='note'>비용 시나리오별 순수익률. 비용 비중 = (수수료+슬리피지) / |총손익(비용 전)|. 1을 넘으면 비용이 총 엣지보다 큼.</p>"
            + table(rows, [("strategy", "strategy", "text"), ("symbol", "market", "text"), ("bar", "bar", "text"),
                           ("trades", "trades", "num"), ("gross", "gross", "pct"), ("opt", "net @0.16%", "pct"),
                           ("base", "net @0.30%", "pct"), ("stress", "net @0.50%", "pct"), ("fees", "fees KRW", "krw"),
                           ("slip", "slippage KRW", "krw"), ("cost_share", "cost/|gross|", "num")]))


def _period_table(periods: list[dict]) -> str:
    names = ["dev_2022_2024", "val_2025", "test_2026_jan_sep", "oct_2025_crash"]
    by = defaultdict(dict)
    for r in periods:
        by[(r["strategy"], r["symbol"], r["bar"])][r["period"]] = r
    rows = []
    for (s, m, b), d in sorted(by.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])):
        row = {"strategy": s, "symbol": m, "bar": b}
        for n in names:
            x = d.get(n, {})
            row[f"{n}_ret"] = x.get("net_return")
            row[f"{n}_er"] = x.get("expectancy_r")
            row[f"{n}_n"] = x.get("trades")
        rows.append(row)
    cols = [("strategy", "strategy", "text"), ("symbol", "market", "text"), ("bar", "bar", "text")]
    for n, lab in zip(names, ["dev", "val 2025", "test 2026", "2025-10 stress"], strict=True):
        cols += [(f"{n}_ret", f"{lab} ret", "pct"), (f"{n}_er", f"{lab} E[R]", "r"), (f"{n}_n", f"{lab} n", "num")]
    return table(rows, cols)


def _failures(p: dict, ok_base: list[dict]) -> str:
    items = []
    hourly = [r for r in ok_base if r.get("bar") == p["meta"]["interval"]]
    for r in sorted(hourly, key=lambda r: r.get("expectancy_r") or 0)[:6]:
        gross = r.get("total_return_gross") or 0
        why = []
        if gross < 0:
            why.append("비용 이전(gross)에도 손실 → 신호 자체에 엣지 없음")
        elif (r.get("net_return") or 0) < 0:
            why.append("gross 는 양수지만 비용 차감 후 손실 → 거래 빈도/비용이 엣지보다 큼")
        if (r.get("win_rate") or 0) < 0.35 and (r.get("avg_win_krw") or 0) < 2 * abs(r.get("avg_loss_krw") or 1):
            why.append("낮은 승률 대비 손익비 부족")
        if (r.get("avg_mfe") or 0) > 0.02 and (r.get("expectancy_r") or 0) < 0:
            why.append("MFE 는 있으나 청산 규칙이 이익을 보존하지 못함")
        items.append(f"<li><b>{esc(r['strategy'])}/{esc(r['symbol'])}</b>: E[R] {r['expectancy_r']:+.3f}, "
                     f"net {r['net_return'] * 100:.1f}% — {'; '.join(why) or '원인 추가 분석 필요'}</li>")
    la = [a for a in p.get("lookahead", []) if a.get("status") == "FAIL"]
    gen = ["체결 기반(C) 전략은 7일 내외 표본으로 통계적 판단 불가 → PAPER 모드에서 실시간 체결·호가를 최소 4주 이상 축적 후 재평가.",
           "과거 강제청산 데이터 부재 → B2는 '청산 확인 없는' 버전만 검증됨. 청산 WS를 장기 수집해야 원래 설계를 검증 가능.",
           "생존 편향: 유니버스(BTC/ETH/XRP)는 사후적으로 선택된 생존 대형 종목이며 상장폐지 종목을 포함하지 않음.",
           "개선 후보(과최적화 주의, 반드시 walk-forward로만 평가): 추세 국면 필터, 변동성 기반 보유기간, 부분 익절, 시간대 필터."]
    if la:
        gen.insert(0, "Look-ahead 감사 실패 전략 존재: " + ", ".join(esc(a["strategy"]) for a in la) + " → 결과 무효, 수정 필요.")
    return "<h3>가장 나쁜 조합</h3><ul>" + "".join(items) + "</ul><h3>구조적 한계 및 다음 단계</h3><ul>" + "".join(
        f"<li>{g}</li>" for g in gen) + "</ul>"
