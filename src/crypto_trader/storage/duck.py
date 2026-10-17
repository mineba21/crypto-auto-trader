"""DuckDB read-only analytics over the published Parquet files (never over in-flight .tmp files)."""

from __future__ import annotations

import duckdb

from crypto_trader.storage.parquet_store import ParquetStore


def connect(store: ParquetStore) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    for kind in ("ohlcv", "trades", "orderbook", "open_interest", "funding", "liquidations", "metrics", "ticker",
                 "premium_index", "taker_volume", "mark_price"):
        pattern = store.glob(kind)
        try:
            con.execute(
                f"CREATE VIEW {kind} AS SELECT * FROM read_parquet('{pattern}', hive_partitioning=true, union_by_name=true)"
            )
        except duckdb.IOException:
            continue  # no files for this kind yet
    return con


def coverage(store: ParquetStore) -> list[dict]:
    """Per (kind, exchange, symbol): row count, first/last timestamp."""
    con = connect(store)
    out = []
    views = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()}
    for kind in sorted(views):
        cols = {r[0] for r in con.execute(f"DESCRIBE {kind}").fetchall()}
        ts = "open_ts" if "open_ts" in cols else "event_ts"
        extra = ", interval" if "interval" in cols else (", period" if "period" in cols else "")
        q = f"""SELECT exchange, symbol{extra}, count(*) n, min({ts}) first_ts, max({ts}) last_ts
                FROM {kind} GROUP BY ALL ORDER BY ALL"""
        for row in con.execute(q).fetchall():
            d = dict(zip([c[0] for c in con.description], row, strict=True))
            d["kind"] = kind
            out.append(d)
    return out
