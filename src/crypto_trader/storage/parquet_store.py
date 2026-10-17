"""Partitioned Parquet market-data store with atomic publishing.

Layout::

    {root}/market/kind={kind}/exchange={exchange}/symbol={symbol}/date={YYYY-MM-DD}/part-*.parquet

Writers stage to ``.tmp-*`` files in the same directory, fsync, then ``os.replace`` into the final name, so a
reader (DuckDB/Polars glob on ``*.parquet``) only ever sees complete files. Leftover ``.tmp-*`` files from
crashed writers are invisible to readers and cleaned by ``validate()``.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

# Primary key per data kind, used for de-duplication.
KEYS: dict[str, list[str]] = {
    "ohlcv": ["interval", "open_ts"],
    "trades": ["event_ts", "seq", "price", "volume"],
    "orderbook": ["event_ts"],
    "ticker": ["event_ts"],
    "open_interest": ["period", "event_ts"],
    "funding": ["event_ts"],
    "mark_price": ["interval", "event_ts"],
    "premium_index": ["interval", "event_ts"],
    "taker_volume": ["period", "event_ts"],
    "liquidations": ["event_ts", "side", "price", "qty"],
    "metrics": ["event_ts"],
}


class StorageError(RuntimeError):
    pass


def _date_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "_")


@dataclass
class ValidationReport:
    files_checked: int = 0
    tmp_removed: list[str] = field(default_factory=list)
    corrupt_quarantined: list[str] = field(default_factory=list)
    duplicate_rows: int = 0
    partitions_compacted: int = 0


class ParquetStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.market = self.root / "market"

    def partition_dir(self, kind: str, exchange: str, symbol: str, date: str) -> Path:
        return self.market / f"kind={kind}" / f"exchange={exchange}" / f"symbol={safe_symbol(symbol)}" / f"date={date}"

    # ------------------------------------------------------------------ write
    def _atomic_write(self, df: pl.DataFrame, final: Path) -> Path:
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.parent / f".tmp-{uuid.uuid4().hex}.parquet.tmp"  # never matches *.parquet
        try:
            df.write_parquet(tmp, compression="zstd", statistics=True)
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            # verify it is readable before publishing
            pq.ParquetFile(tmp).metadata  # noqa: B018
            os.replace(tmp, final)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise StorageError(f"failed to write {final}: {e}") from e
        return final

    def write(self, kind: str, exchange: str, symbol: str, df: pl.DataFrame, ts_col: str | None = None) -> list[Path]:
        """Append rows as new part files, one per UTC date. Returns published paths."""
        if df.is_empty():
            return []
        ts_col = ts_col or ("open_ts" if "open_ts" in df.columns else "event_ts")
        if ts_col not in df.columns:
            raise StorageError(f"{kind}: missing timestamp column {ts_col}")
        df = df.with_columns(
            pl.from_epoch(pl.col(ts_col), time_unit="ms").dt.strftime("%Y-%m-%d").alias("__date")
        )
        paths = []
        for (date,), part in df.group_by(["__date"], maintain_order=True):
            part = part.drop("__date")
            lo, hi = int(part[ts_col].min()), int(part[ts_col].max())
            name = f"part-{lo}-{hi}-{uuid.uuid4().hex[:8]}.parquet"
            paths.append(self._atomic_write(part, self.partition_dir(kind, exchange, symbol, date) / name))
        return paths

    def write_compacted(self, kind: str, exchange: str, symbol: str, df: pl.DataFrame,
                        ts_col: str | None = None) -> list[Path]:
        """Merge new rows with existing partition data, de-duplicate, and replace each date partition."""
        if df.is_empty():
            return []
        ts_col = ts_col or ("open_ts" if "open_ts" in df.columns else "event_ts")
        df = df.with_columns(pl.from_epoch(pl.col(ts_col), time_unit="ms").dt.strftime("%Y-%m-%d").alias("__date"))
        out = []
        for (date,), part in df.group_by(["__date"], maintain_order=True):
            part = part.drop("__date")
            d = self.partition_dir(kind, exchange, symbol, date)
            existing = self._read_dir(d)
            merged = pl.concat([existing, part], how="diagonal_relaxed") if existing is not None else part
            merged = self._dedupe(kind, merged).sort(ts_col)
            out.append(self._replace_partition(d, merged))
        return out

    def _replace_partition(self, d: Path, df: pl.DataFrame) -> Path:
        old = [f for f in sorted(d.glob("*.parquet")) if not f.name.startswith(".")] if d.exists() else []
        final = d / f"data-{uuid.uuid4().hex[:8]}.parquet"
        self._atomic_write(df, final)
        for f in old:
            if f != final:
                f.unlink(missing_ok=True)
        return final

    # ------------------------------------------------------------------- read
    @staticmethod
    def _read_dir(d: Path) -> pl.DataFrame | None:
        files = [f for f in sorted(d.glob("*.parquet")) if not f.name.startswith(".")] if d.exists() else []
        if not files:
            return None
        return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")

    def _dedupe(self, kind: str, df: pl.DataFrame) -> pl.DataFrame:
        keys = [k for k in KEYS.get(kind, []) if k in df.columns]
        return df.unique(subset=keys or None, keep="last", maintain_order=True)

    def glob(self, kind: str, exchange: str = "*", symbol: str = "*") -> str:
        return str(self.market / f"kind={kind}" / f"exchange={exchange}" / f"symbol={safe_symbol(symbol)}" / "date=*" / "*.parquet")

    def read(self, kind: str, exchange: str, symbol: str, start_ms: int | None = None, end_ms: int | None = None,
             ts_col: str | None = None, dedupe: bool = True) -> pl.DataFrame:
        base = self.market / f"kind={kind}" / f"exchange={exchange}" / f"symbol={safe_symbol(symbol)}"
        files = [f for f in sorted(base.glob("date=*/*.parquet")) if not f.name.startswith(".")]
        if start_ms is not None or end_ms is not None:
            lo = _date_of(start_ms) if start_ms is not None else "0000"
            hi = _date_of(end_ms) if end_ms is not None else "9999"
            files = [f for f in files if lo <= f.parent.name.removeprefix("date=") <= hi]
        if not files:
            return pl.DataFrame()
        df = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
        ts_col = ts_col or ("open_ts" if "open_ts" in df.columns else "event_ts")
        if start_ms is not None:
            df = df.filter(pl.col(ts_col) >= start_ms)
        if end_ms is not None:
            df = df.filter(pl.col(ts_col) < end_ms)
        if dedupe:
            df = self._dedupe(kind, df)
        return df.sort(ts_col)

    def list_partitions(self) -> list[dict]:
        rows = []
        for f in self.market.glob("kind=*/exchange=*/symbol=*/date=*/*.parquet"):
            parts = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in f.relative_to(self.market).parts[:-1]}
            rows.append({**parts, "file": str(f), "bytes": f.stat().st_size})
        return rows

    # --------------------------------------------------------------- validate
    def validate(self, repair: bool = True, compact: bool = False) -> ValidationReport:
        rep = ValidationReport()
        if not self.market.exists():
            return rep
        for tmp in self.market.rglob(".tmp-*"):
            rep.tmp_removed.append(str(tmp))
            if repair:
                tmp.unlink(missing_ok=True)
        quarantine = self.root / "quarantine"
        by_dir: dict[Path, list[Path]] = defaultdict(list)
        for f in self.market.rglob("*.parquet"):
            if f.name.startswith("."):
                continue
            rep.files_checked += 1
            try:
                pq.ParquetFile(f).metadata  # noqa: B018
                by_dir[f.parent].append(f)
            except Exception:
                rep.corrupt_quarantined.append(str(f))
                if repair:
                    dst = quarantine / f.relative_to(self.root)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(f, dst)
        for d, files in by_dir.items():
            kind = d.parts[-4].removeprefix("kind=")
            df = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
            deduped = self._dedupe(kind, df)
            dup = df.height - deduped.height
            rep.duplicate_rows += dup
            if repair and (dup > 0 or (compact and len(files) > 1)):
                ts_col = "open_ts" if "open_ts" in df.columns else "event_ts"
                self._replace_partition(d, deduped.sort(ts_col))
                rep.partitions_compacted += 1
        return rep


class BufferedSink:
    """Collects live rows in memory and publishes them in batches (by size or age)."""

    def __init__(self, store: ParquetStore, kind: str, exchange: str, max_rows: int = 5_000, max_age_s: float = 60.0):
        self.store, self.kind, self.exchange = store, kind, exchange
        self.max_rows, self.max_age_s = max_rows, max_age_s
        self.buf: dict[str, list[dict]] = defaultdict(list)
        self.first_ts: float | None = None
        self.write_errors = 0
        self.rows_written = 0

    def add(self, symbol: str, row: dict, now_s: float) -> None:
        self.buf[symbol].append(row)
        if self.first_ts is None:
            self.first_ts = now_s

    def due(self, now_s: float) -> bool:
        n = sum(len(v) for v in self.buf.values())
        return n >= self.max_rows or (self.first_ts is not None and now_s - self.first_ts >= self.max_age_s)

    def flush(self) -> int:
        """Publish buffered rows. On failure rows stay buffered and the error is re-raised (-> kill switch)."""
        written = 0
        for symbol in list(self.buf):
            rows = self.buf[symbol]
            if not rows:
                continue
            try:
                self.store.write(self.kind, self.exchange, symbol, pl.DataFrame(rows, infer_schema_length=None))
            except Exception:
                self.write_errors += 1
                raise
            written += len(rows)
            self.buf[symbol] = []
        self.first_ts = None
        self.rows_written += written
        return written
