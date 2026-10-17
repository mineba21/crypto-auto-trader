"""Binance public data archive (https://data.binance.vision) downloader + normalizer.

Observed format quirks (verified 2026-10-17, see docs/API_ACCESS.md):
* ``futures/um/daily/metrics``: 5-minute OI & long/short ratios from 2021-12-01 (BTCUSDT). ``create_time`` is a
  ``YYYY-MM-DD HH:MM:SS`` UTC string. Decimal precision changed over time; some days start late (missing rows).
  The metric is a *snapshot at create_time*; we set ``available_ts = create_time + 5min`` conservatively.
* ``futures/um/monthly/fundingRate``: ``calc_time`` in ms, settled (confirmed) rates only.
* ``futures/um/monthly/klines``: header row, ms timestamps.
* ``spot/monthly/klines``: no header; timestamps switched from ms to **microseconds** from 2025-01.
* ``liquidationSnapshot`` is not published for USD-M futures (404) -> real liquidations need live WS capture.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import zipfile
from datetime import date, timedelta
from pathlib import Path

import httpx
import polars as pl

log = logging.getLogger(__name__)

BASE = "https://data.binance.vision/data"
KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
              "taker_buy_volume", "taker_buy_quote_volume", "ignore"]


def _months(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _days(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def normalize_epoch_ms(col: pl.Expr) -> pl.Expr:
    """Archive timestamps may be ms (13 digits) or us (16 digits). Normalize to ms."""
    return pl.when(col > 10**14).then(col // 1000).otherwise(col).cast(pl.Int64)


def _read_csv_bytes(raw: bytes, expected_cols: list[str] | None = None) -> pl.DataFrame:
    first = raw.split(b"\n", 1)[0].decode(errors="replace")
    has_header = any(c.isalpha() for c in first.replace("e-", "").replace("E-", ""))
    if has_header:
        return pl.read_csv(io.BytesIO(raw), infer_schema_length=10_000)
    df = pl.read_csv(io.BytesIO(raw), has_header=False, infer_schema_length=10_000)
    if expected_cols and df.width == len(expected_cols):
        df.columns = expected_cols
    return df


def parse_klines(raw: bytes) -> pl.DataFrame:
    df = _read_csv_bytes(raw, KLINE_COLS)
    return df.select(
        normalize_epoch_ms(pl.col("open_time").cast(pl.Int64)).alias("open_ts"),
        (normalize_epoch_ms(pl.col("close_time").cast(pl.Int64)) + 1).alias("close_ts"),
        *[pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close", "volume", "quote_volume",
                                                "taker_buy_volume", "taker_buy_quote_volume")],
        pl.col("count").cast(pl.Int64).alias("trades"),
    ).with_columns(pl.col("close_ts").alias("available_ts"))


def parse_metrics(raw: bytes) -> pl.DataFrame:
    df = _read_csv_bytes(raw)
    ts = pl.col("create_time").str.strptime(pl.Datetime("ms", "UTC"), "%Y-%m-%d %H:%M:%S").dt.epoch("ms")
    return df.select(
        ts.alias("event_ts"),
        (ts + 300_000).alias("available_ts"),
        pl.col("sum_open_interest").cast(pl.Float64).alias("oi_base"),
        pl.col("sum_open_interest_value").cast(pl.Float64).alias("oi_usd"),
        pl.col("count_toptrader_long_short_ratio").cast(pl.Float64).alias("top_ls_ratio_accounts"),
        pl.col("sum_toptrader_long_short_ratio").cast(pl.Float64).alias("top_ls_ratio_positions"),
        pl.col("count_long_short_ratio").cast(pl.Float64).alias("global_ls_ratio"),
        pl.col("sum_taker_long_short_vol_ratio").cast(pl.Float64).alias("taker_buy_sell_ratio"),
    )


def parse_funding(raw: bytes) -> pl.DataFrame:
    df = _read_csv_bytes(raw, ["calc_time", "funding_interval_hours", "last_funding_rate"])
    ts = normalize_epoch_ms(pl.col("calc_time").cast(pl.Int64))
    return df.select(
        ts.alias("event_ts"),
        ts.alias("available_ts"),  # settled rate is known at settlement time
        pl.col("last_funding_rate").cast(pl.Float64).alias("funding_rate"),
        pl.col("funding_interval_hours").cast(pl.Int64).alias("interval_hours"),
        pl.lit(True).alias("is_settled"),
    )


class BinanceArchive:
    exchange = "binance"

    def __init__(self, cache_dir: str | Path, client: httpx.AsyncClient | None = None, concurrency: int = 4,
                 verify_checksum: bool = True):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.AsyncClient(timeout=60)
        self.sem = asyncio.Semaphore(concurrency)
        self.verify = verify_checksum
        self.missing: list[str] = []
        self.checksum_failures: list[str] = []

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch(self, rel: str) -> bytes | None:
        """Return CSV bytes for an archive zip (cached), or None if not published (404)."""
        local = self.cache / rel
        if local.exists():
            return self._unzip(local.read_bytes())
        async with self.sem:
            r = await self.client.get(f"{BASE}/{rel}")
            if r.status_code == 404:
                self.missing.append(rel)
                return None
            r.raise_for_status()
            content = r.content
            if self.verify:
                c = await self.client.get(f"{BASE}/{rel}.CHECKSUM")
                if c.status_code == 200:
                    expected = c.text.split()[0].strip()
                    if hashlib.sha256(content).hexdigest() != expected:
                        self.checksum_failures.append(rel)
                        raise ValueError(f"checksum mismatch {rel}")
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(".part")
        tmp.write_bytes(content)
        tmp.replace(local)
        return self._unzip(content)

    @staticmethod
    def _unzip(content: bytes) -> bytes:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            return z.read(z.namelist()[0])

    async def _gather(self, rels: list[str], parser) -> pl.DataFrame:
        raws = await asyncio.gather(*(self.fetch(r) for r in rels))
        frames = [parser(r) for r in raws if r]
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    async def _monthly_with_daily_fallback(self, prefix: str, symbol: str, interval: str, start: date, end: date,
                                           parser) -> pl.DataFrame:
        """Monthly files are published after month end; the current month is assembled from daily files."""
        frames = []
        for m in _months(start, end):
            raw = await self.fetch(f"{prefix.replace('{freq}', 'monthly')}/{symbol}/{interval}/{symbol}-{interval}-{m}.zip")
            if raw:
                frames.append(parser(raw))
                continue
            y, mo = map(int, m.split("-"))
            first = max(start, date(y, mo, 1))
            last = min(end, (date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1)))
            rels = [f"{prefix.replace('{freq}', 'daily')}/{symbol}/{interval}/{symbol}-{interval}-{d}.zip"
                    for d in _days(first, last)]
            df = await self._gather(rels, parser)
            if not df.is_empty():
                frames.append(df)
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    async def futures_klines(self, symbol: str, interval: str, start: date, end: date) -> pl.DataFrame:
        return await self._monthly_with_daily_fallback("futures/um/{freq}/klines", symbol, interval, start, end, parse_klines)

    async def spot_klines(self, symbol: str, interval: str, start: date, end: date) -> pl.DataFrame:
        return await self._monthly_with_daily_fallback("spot/{freq}/klines", symbol, interval, start, end, parse_klines)

    async def premium_index_klines(self, symbol: str, interval: str, start: date, end: date) -> pl.DataFrame:
        return await self._monthly_with_daily_fallback("futures/um/{freq}/premiumIndexKlines", symbol, interval,
                                                       start, end, parse_klines)

    async def funding(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        rels = [f"futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{m}.zip" for m in _months(start, end)]
        return await self._gather(rels, parse_funding)

    async def metrics(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        rels = [f"futures/um/daily/metrics/{symbol}/{symbol}-metrics-{d}.zip" for d in _days(start, end)]
        return await self._gather(rels, parse_metrics)
