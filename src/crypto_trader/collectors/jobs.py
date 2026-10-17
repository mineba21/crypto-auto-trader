"""Collection jobs: API probe, historical backfill and live capture."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import polars as pl

from crypto_trader.collectors.binance.archive import BinanceArchive
from crypto_trader.collectors.binance.futures_rest import BinanceFuturesRest
from crypto_trader.collectors.bithumb.rest import BithumbRest
from crypto_trader.collectors.bybit.rest import BybitRest
from crypto_trader.collectors.http import AccessBlocked, HttpClient
from crypto_trader.collectors.okx import client as okx
from crypto_trader.collectors.upbit.rest import UpbitRest
from crypto_trader.collectors.upbit.ws import UpbitWsCollector
from crypto_trader.core.models import Bar
from crypto_trader.core.timeutils import now_ms
from crypto_trader.storage.parquet_store import ParquetStore

log = logging.getLogger(__name__)


def bars_to_df(bars: list[Bar], received_ts: int | None = None) -> pl.DataFrame:
    rts = received_ts or now_ms()
    rows = [{**{k: getattr(b, k) for k in Bar.__slots__ if k not in ("exchange", "symbol")}, "received_ts": rts}
            for b in bars]
    return pl.DataFrame(rows) if rows else pl.DataFrame()


# --------------------------------------------------------------------------- probe
PROBES = [
    ("upbit", "REST markets", "https://api.upbit.com/v1/market/all"),
    ("upbit", "REST candles 1h", "https://api.upbit.com/v1/candles/minutes/60?market=KRW-BTC&count=1"),
    ("upbit", "REST orderbook", "https://api.upbit.com/v1/orderbook?markets=KRW-BTC"),
    ("upbit", "REST trades/ticks", "https://api.upbit.com/v1/trades/ticks?market=KRW-BTC&count=1"),
    ("upbit", "REST KRW-USDT ticker", "https://api.upbit.com/v1/ticker?markets=KRW-USDT"),
    ("bithumb", "REST markets", "https://api.bithumb.com/v1/market/all"),
    ("binance", "fapi ticker/price", "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT"),
    ("binance", "fapi openInterest", "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"),
    ("binance", "fapi premiumIndex", "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"),
    ("binance", "fapi fundingRate", "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1"),
    ("binance", "openInterestHist", "https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=1h&limit=1"),
    ("binance", "archive metrics", "https://data.binance.vision/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2024-01-01.zip"),
    ("binance", "archive fundingRate", "https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip"),
    ("binance", "archive liquidationSnapshot", "https://data.binance.vision/data/futures/um/daily/liquidationSnapshot/BTCUSDT/BTCUSDT-liquidationSnapshot-2024-01-01.zip"),
    ("bybit", "v5 tickers", "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"),
    ("bybit", "v5 open-interest", "https://api.bybit.com/v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=1h&limit=1"),
    ("okx", "public open-interest", "https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP"),
    ("okx", "funding-rate-history", "https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=1"),
    ("okx", "rubik OI history", "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history?instId=BTC-USDT-SWAP&period=1H&limit=1"),
    ("okx", "spot ticker BTC-USDT", "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT"),
]

WS_PROBES = [
    ("upbit", "wss://api.upbit.com/websocket/v1",
     [{"ticket": "probe"}, {"type": "trade", "codes": ["KRW-BTC"]}, {"format": "DEFAULT"}]),
    ("okx", "wss://ws.okx.com/ws/v5/public", {"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]}),
    ("binance", "wss://fstream.binance.com/ws/btcusdt@forceOrder", None),
]


async def probe_apis(out_dir: Path | None = None, include_ws: bool = True) -> list[dict]:
    """Check reachability of every data source from *this* network. Results are facts, not assumptions."""
    import websockets

    results = []
    async with httpx.AsyncClient(timeout=15) as c:
        for ex, name, url in PROBES:
            t0 = time.monotonic()
            try:
                r = await c.get(url)
                status, body = r.status_code, r.text[:160].replace("\n", " ")
                rl = r.headers.get("Remaining-Req") or r.headers.get("x-mbx-used-weight-1m") or ""
            except Exception as e:  # noqa: BLE001
                status, body, rl = None, f"{type(e).__name__}: {e}", ""
            verdict = "OK" if status == 200 else ("NOT_PUBLISHED" if status == 404 else "BLOCKED" if status in (403, 451) else "ERROR")
            results.append({"exchange": ex, "endpoint": name, "transport": "REST", "url": url, "status": status,
                            "verdict": verdict, "latency_ms": round((time.monotonic() - t0) * 1000),
                            "rate_limit_header": rl, "body_head": body})
    if include_ws:
        for ex, url, sub in WS_PROBES:
            t0 = time.monotonic()
            try:
                async with websockets.connect(url, open_timeout=10) as ws:
                    if sub is not None:
                        await ws.send(json.dumps(sub))
                    m = await asyncio.wait_for(ws.recv(), 15)
                    verdict, body = "OK", str(m)[:160]
            except Exception as e:  # noqa: BLE001
                verdict, body = "ERROR", f"{type(e).__name__}: {e}"
            results.append({"exchange": ex, "endpoint": url, "transport": "WS", "url": url, "status": None,
                            "verdict": verdict, "latency_ms": round((time.monotonic() - t0) * 1000),
                            "rate_limit_header": "", "body_head": body})
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            loc = (await c.get("https://ipinfo.io/json")).json()
            network = {k: loc.get(k) for k in ("country", "region", "org")}
    except Exception:  # noqa: BLE001
        network = {}
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {"probed_at_utc": datetime.now(UTC).isoformat(), "network": network, "results": results}
        (out_dir / "api_probe_results.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return results


# ------------------------------------------------------------------- history
async def collect_upbit_history(store: ParquetStore, markets: list[str], interval: str, start_ms: int, end_ms: int,
                                exchange: str = "upbit") -> dict[str, int]:
    rest = UpbitRest() if exchange == "upbit" else BithumbRest()
    out = {}
    try:
        for m in markets:
            bars = await rest.candles(m, interval, start_ms, end_ms)
            df = bars_to_df(bars)
            if not df.is_empty():
                store.write_compacted("ohlcv", exchange, m, df)
            out[m] = len(bars)
            log.info("%s %s %s: %d bars", exchange, m, interval, len(bars))
    finally:
        await rest.close()
    return out


async def collect_binance_archive(store: ParquetStore, symbols: list[str], start: date, end: date,
                                  cache_dir: Path, interval: str = "1h") -> dict:
    arc = BinanceArchive(cache_dir)
    summary: dict = {}
    try:
        for sym in symbols:
            s: dict = {}
            fk = await arc.futures_klines(sym, interval, start, end)
            if not fk.is_empty():
                store.write_compacted("ohlcv", "binance_futures", sym, fk.with_columns(pl.lit(interval).alias("interval")))
            s["futures_klines"] = fk.height
            sk = await arc.spot_klines(sym, interval, start, end)
            if not sk.is_empty():
                store.write_compacted("ohlcv", "binance_spot", sym, sk.with_columns(pl.lit(interval).alias("interval")))
            s["spot_klines"] = sk.height
            pk = await arc.premium_index_klines(sym, interval, start, end)
            if not pk.is_empty():
                store.write_compacted("premium_index", "binance", sym,
                                      pk.select(pl.col("open_ts").alias("event_ts"), "close_ts", "available_ts",
                                                pl.col("open").alias("premium_open"), pl.col("close").alias("premium_close"),
                                                pl.col("high").alias("premium_high"), pl.col("low").alias("premium_low"))
                                      .with_columns(pl.lit(interval).alias("interval")))
            s["premium_index"] = pk.height
            fu = await arc.funding(sym, start, end)
            if not fu.is_empty():
                store.write_compacted("funding", "binance", sym, fu)
            s["funding"] = fu.height
            me = await arc.metrics(sym, start, end)
            if not me.is_empty():
                store.write_compacted("metrics", "binance", sym, me)
            s["metrics"] = me.height
            summary[sym] = s
            log.info("binance archive %s %s", sym, s)
    finally:
        summary["_missing_files"] = len(arc.missing)
        summary["_missing_sample"] = arc.missing[:20]
        summary["_checksum_failures"] = arc.checksum_failures
        summary["_parse_failures"] = arc.parse_failures
        await arc.close()
    return summary


async def collect_okx_recent(store: ParquetStore, binance_symbols: list[str], start_ms: int, end_ms: int) -> dict:
    rest = okx.OkxRest()
    out = {}
    try:
        for sym in binance_symbols:
            inst = okx.swap_inst(sym)
            fh = await rest.funding_history(inst, start_ms, end_ms)
            if fh:
                df = pl.DataFrame([{"event_ts": int(r["fundingTime"]), "available_ts": int(r["fundingTime"]),
                                    "funding_rate": float(r["realizedRate"] or r["fundingRate"]), "is_settled": True}
                                   for r in fh])
                store.write_compacted("funding", "okx", inst, df)
            oi = await rest.oi_history(inst, "1H")
            ct = await rest.ct_val(inst)
            if oi:
                df = pl.DataFrame([{"period": "1h", "event_ts": int(r[0]), "available_ts": int(r[0]) + 3_600_000,
                                    "oi_contracts": float(r[1]), "oi_base": float(r[2]), "oi_usd": float(r[3])} for r in oi])
                store.write_compacted("open_interest", "okx", inst, df)
            out[inst] = {"funding": len(fh), "oi_1h": len(oi), "ct_val": ct}
    finally:
        await rest.close()
    return out


# --------------------------------------------------------------------- live
async def poll_derivatives_snapshot(store: ParquetStore, binance_symbols: list[str], rest: okx.OkxRest) -> int:
    """One poll of OKX OI / estimated funding / mark price. Estimated funding is flagged ``is_settled=False``."""
    rows = 0
    rts = now_ms()
    for sym in binance_symbols:
        inst = okx.swap_inst(sym)
        oi = await rest.open_interest(inst)
        fr = await rest.funding_rate(inst)
        mp = await rest.mark_price(inst)
        store.write("open_interest", "okx", inst, pl.DataFrame([{
            "period": "snapshot", "event_ts": int(oi["ts"]), "received_ts": rts, "available_ts": rts,
            "oi_contracts": float(oi["oi"]), "oi_base": float(oi["oiCcy"]), "oi_usd": float(oi.get("oiUsd") or 0)}]))
        store.write("funding", "okx_estimate", inst, pl.DataFrame([{
            "event_ts": rts, "received_ts": rts, "available_ts": rts, "funding_rate": float(fr["fundingRate"]),
            "funding_time": int(fr["fundingTime"]), "is_settled": False}]))
        store.write("mark_price", "okx", inst, pl.DataFrame([{
            "interval": "snapshot", "event_ts": int(mp["ts"]), "received_ts": rts, "available_ts": rts,
            "mark_price": float(mp["markPx"])}]))
        rows += 3
    return rows


async def collect_live(store: ParquetStore, markets: list[str], binance_symbols: list[str], duration_s: float,
                       poll_s: float = 60.0) -> dict:
    """Live capture: Upbit trades+orderbook (WS), OKX liquidations (WS), OKX derivatives snapshots (REST poll)."""
    ws = UpbitWsCollector(markets, store, flush_s=min(30.0, duration_s))
    rest = okx.OkxRest()
    insts = [okx.swap_inst(s) for s in binance_symbols]
    stats = {"okx_snapshot_rows": 0, "okx_errors": []}

    async def poller():
        end = time.time() + duration_s
        while time.time() < end:
            try:
                stats["okx_snapshot_rows"] += await poll_derivatives_snapshot(store, binance_symbols, rest)
            except Exception as e:  # noqa: BLE001
                stats["okx_errors"].append(str(e)[:200])
            await asyncio.sleep(min(poll_s, max(0.0, end - time.time())))

    try:
        res = await asyncio.gather(ws.run(duration_s), okx.run_liquidations(insts, store, rest, duration_s), poller(),
                                   return_exceptions=True)
    finally:
        await rest.close()
        await ws.rest.close()
    stats["okx_liquidations"] = res[1] if not isinstance(res[1], Exception) else repr(res[1])
    stats["upbit_ws"] = {**asdict(ws.health), "trades_rows": ws.sinks["trades"].rows_written,
                         "orderbook_rows": ws.sinks["orderbook"].rows_written}
    stats["errors"] = [repr(r) for r in res if isinstance(r, Exception)]
    return stats


async def check_blocked_sources() -> dict[str, str]:
    """Call Binance/Bybit REST once; return BLOCKED reason or OK. Never retried around a geo block."""
    out = {}
    from crypto_trader.collectors.binance import futures_rest
    from crypto_trader.collectors.bybit import rest as bybit_rest

    for name, cls, base, call in (
        ("binance_fapi", BinanceFuturesRest, futures_rest.BASE_URL, lambda c: c.price("BTCUSDT")),
        ("bybit_v5", BybitRest, bybit_rest.BASE_URL, lambda c: c.tickers("BTCUSDT")),
    ):
        c = cls(HttpClient(base, max_retries=1))
        try:
            await call(c)
            out[name] = "OK"
        except AccessBlocked as e:
            out[name] = f"BLOCKED (HTTP {e.status})"
        except Exception as e:  # noqa: BLE001
            out[name] = f"ERROR {type(e).__name__}: {str(e)[:120]}"
        finally:
            await c.close()
    return out


async def collect_upbit_trades_backfill(store: ParquetStore, markets: list[str], days: int = 7,
                                        max_pages_per_day: int = 4000) -> dict:
    """Backfill Upbit trade ticks over REST (Upbit serves only the most recent ~7 days).

    Pages backwards with ``cursor=sequential_id`` within each ``days_ago`` bucket. Rows are tagged source=rest and
    ``available_ts`` = event_ts (they are historical; in live use only the WS receive time counts).
    """
    rest = UpbitRest()
    out: dict = {}
    try:
        for m in markets:
            total = 0
            for d in range(0, days + 1):
                cursor = None
                rows: list[dict] = []
                for _ in range(max_pages_per_day):
                    params: dict = {"market": m, "count": 500}
                    if d:
                        params["days_ago"] = d
                    if cursor:
                        params["cursor"] = cursor
                    page = await rest.http.get("/v1/trades/ticks", params=params)
                    if not page:
                        break
                    rts = now_ms()
                    for r in page:
                        rows.append({"event_ts": int(r["timestamp"]), "received_ts": rts, "available_ts": int(r["timestamp"]),
                                     "price": float(r["trade_price"]), "volume": float(r["trade_volume"]),
                                     "aggressor": r["ask_bid"], "seq": int(r["sequential_id"]), "source": "rest"})
                    nxt = str(page[-1]["sequential_id"])
                    if nxt == cursor or len(page) < 500:
                        break
                    cursor = nxt
                if rows:
                    store.write_compacted("trades", "upbit", m, pl.DataFrame(rows))
                total += len(rows)
                log.info("upbit trades %s days_ago=%d rows=%d", m, d, len(rows))
            out[m] = total
    finally:
        await rest.close()
    return out
