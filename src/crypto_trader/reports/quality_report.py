"""Market-data quality HTML report (Phase 1 deliverable)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from crypto_trader.core.config import ROOT, Settings
from crypto_trader.core.timeutils import fmt_kst
from crypto_trader.reports.html import esc, page, table
from crypto_trader.storage import quality
from crypto_trader.storage.duck import coverage
from crypto_trader.storage.parquet_store import ParquetStore


def _d(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M") if ms else "-"


def build_quality_report(store: ParquetStore, settings: Settings, out: Path) -> Path:
    body = ["<h1>시장 데이터 품질 리포트</h1>",
            f"<p class='sub'>생성 {datetime.now(UTC).isoformat(timespec='seconds')} UTC · 저장소 {esc(store.root)}</p>"]
    probe = ROOT / "docs" / "generated" / "api_probe_results.json"
    if probe.exists():
        pj = json.loads(probe.read_text())
        body.append(f"<h2>API 접근 검증</h2><p class='sub'>측정 {esc(pj['probed_at_utc'])} · 네트워크 {esc(pj.get('network'))}</p>")
        body.append("<p class='note'>이 측정은 개발용 클라우드 컨테이너(대한민국 외부)에서 수행되었습니다. 한국 네트워크에서의 결과는 "
                    "운영 서버에서 <code>uv run cat probe</code> 로 다시 확인해야 합니다. 차단된 API는 우회하지 않습니다.</p>")
        body.append(table(pj["results"], [("verdict", "verdict", "text"), ("exchange", "exchange", "text"),
                                           ("transport", "type", "text"), ("endpoint", "endpoint", "text"),
                                           ("status", "HTTP", "num"), ("latency_ms", "ms", "num"),
                                           ("rate_limit_header", "rate-limit hdr", "text"), ("body_head", "response head", "text")]))
    rep = store.validate(repair=False)
    body.append("<h2>저장소 무결성</h2>")
    body.append(table([{"files": rep.files_checked, "tmp": len(rep.tmp_removed), "corrupt": len(rep.corrupt_quarantined),
                        "dups": rep.duplicate_rows}],
                      [("files", "parquet files", "num"), ("tmp", "leftover .tmp", "num"), ("corrupt", "corrupt", "num"),
                       ("dups", "duplicate rows", "num")]))
    cov = coverage(store)
    for c in cov:
        c["first"], c["last"] = _d(c["first_ts"]), _d(c["last_ts"])
    body.append("<h2>데이터 커버리지 (DuckDB)</h2>")
    body.append(table(cov, [("kind", "kind", "text"), ("exchange", "exchange", "text"), ("symbol", "symbol", "text"),
                            ("interval", "interval", "text"), ("period", "period", "text"), ("n", "rows", "num"),
                            ("first", "first (UTC)", "text"), ("last", "last (UTC)", "text")]))

    rows = []
    for part in sorted({(p["exchange"], p["symbol"]) for p in store.list_partitions() if p["kind"] == "ohlcv"}):
        df = store.read("ohlcv", *part)
        for iv in df["interval"].unique().to_list():
            q = quality.ohlcv_quality(df.filter(pl.col("interval") == iv), iv)
            rows.append({"exchange": part[0], "symbol": part[1], "interval": iv, **q, "first": _d(q.get("first_ts")),
                         "last": _d(q.get("last_ts")),
                         "largest": "; ".join(f"{_d(t)} +{n}" for t, n in q.get("largest_gaps", []))})
    body.append("<h2>OHLCV 품질</h2><p class='note'>Upbit 는 체결이 없는 구간의 캔들을 생성하지 않습니다. 따라서 저유동 구간의 "
                "'missing' 은 수집 실패가 아니라 무체결일 수 있으며, 채워 넣지 않습니다. 대형 공백(점검 등)은 largest gaps 에 표시.</p>")
    body.append(table(rows, [("exchange", "exchange", "text"), ("symbol", "symbol", "text"), ("interval", "iv", "text"),
                             ("rows", "rows", "num"), ("first", "first", "text"), ("last", "last", "text"),
                             ("missing", "missing", "num"), ("missing_pct", "missing %", "pctn"), ("gap_events", "gaps", "num"),
                             ("duplicates", "dups", "num"), ("misaligned", "misaligned", "num"), ("bad_ohlc", "bad OHLC", "num"),
                             ("zero_volume", "zero vol", "num"), ("return_outliers_8sd", "outliers 8σ", "num"),
                             ("largest", "largest gaps (start +bars)", "text")]))

    der = []
    for ex, sym in sorted({(p["exchange"], p["symbol"]) for p in store.list_partitions() if p["kind"] == "metrics"}):
        q = quality.regular_series_quality(store.read("metrics", ex, sym), 300_000)
        der.append({"series": f"{ex} metrics 5m", "symbol": sym, **q, "first": _d(q.get("first_ts")), "last": _d(q.get("last_ts"))})
    for ex, sym in sorted({(p["exchange"], p["symbol"]) for p in store.list_partitions() if p["kind"] == "funding"}):
        df = store.read("funding", ex, sym)
        step = 8 * 3_600_000 if ex == "binance" else 8 * 3_600_000
        q = quality.regular_series_quality(df.with_columns((pl.col("event_ts") // 1000 * 1000).alias("event_ts")), step) if ex != "okx_estimate" else {"rows": df.height}
        der.append({"series": f"{ex} funding", "symbol": sym, **q, "first": _d(q.get("first_ts")), "last": _d(q.get("last_ts"))})
    body.append("<h2>파생 데이터 품질</h2><p class='note'>Binance metrics 는 5분 스냅샷이며 일부 날짜는 늦게 시작하거나 행이 빠집니다. "
                "Funding 주기는 심볼·시기에 따라 8h 외 4h 등으로 바뀔 수 있어 irregular 로 집계됩니다. 확정 funding 과 실시간 추정치(okx_estimate)는 별도 시리즈입니다.</p>")
    body.append(table(der, [("series", "series", "text"), ("symbol", "symbol", "text"), ("rows", "rows", "num"),
                            ("first", "first", "text"), ("last", "last", "text"), ("missing", "missing", "num"),
                            ("missing_pct", "missing %", "pctn"), ("gap_events", "gaps", "num"),
                            ("irregular_steps", "irregular", "num")]))

    live = []
    for kind in ("trades", "orderbook", "liquidations"):
        for ex, sym in sorted({(p["exchange"], p["symbol"]) for p in store.list_partitions() if p["kind"] == kind}):
            df = store.read(kind, ex, sym, dedupe=False)
            q = quality.latency_quality(df if kind != "trades" else df.filter(pl.col("source") == "ws") if "source" in df.columns else df)
            live.append({"kind": kind, "exchange": ex, "symbol": sym, "total_rows": df.height, **q,
                         "first": fmt_kst(q.get("first_ts")), "last": fmt_kst(q.get("last_ts"))})
    gaps = []
    for ex, sym in sorted({(p["exchange"], p["symbol"]) for p in store.list_partitions() if p["kind"] == "gaps"}):
        g = store.read("gaps", ex, sym)
        for r in g.iter_rows(named=True):
            gaps.append({"symbol": sym, "stream": r.get("stream"), "start": fmt_kst(r["event_ts"]), "end": fmt_kst(r.get("end_ts")),
                         "reason": r.get("reason")})
    body.append("<h2>실시간 수집 지연·순서 (WS 수신분)</h2>")
    body.append(table(live, [("kind", "kind", "text"), ("exchange", "exchange", "text"), ("symbol", "symbol", "text"),
                             ("total_rows", "rows (all)", "num"), ("rows", "ws rows", "num"), ("first", "first", "text"),
                             ("last", "last", "text"), ("lat_p50_ms", "p50 ms", "num"), ("lat_p95_ms", "p95 ms", "num"),
                             ("lat_p99_ms", "p99 ms", "num"), ("lat_max_ms", "max ms", "num"),
                             ("negative_latency", "neg lat", "num"), ("out_of_order", "out-of-order", "num")]))
    body.append("<h3>기록된 데이터 공백</h3>" + table(gaps, [("symbol", "symbol", "text"), ("stream", "stream", "text"),
                                                    ("start", "start", "text"), ("end", "end", "text"), ("reason", "reason", "text")]))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page("Data Quality Report", "".join(body), "market data coverage and quality"), encoding="utf-8")
    return out
