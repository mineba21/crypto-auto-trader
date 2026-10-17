import polars as pl
import pytest

from crypto_trader.storage.parquet_store import BufferedSink, ParquetStore, StorageError


def df(ts):
    return pl.DataFrame({"interval": ["1h"] * len(ts), "open_ts": ts, "close": [float(t) for t in ts]})


def test_write_partitions_by_utc_date_and_reads_back(tmp_path):
    st = ParquetStore(tmp_path)
    paths = st.write("ohlcv", "upbit", "KRW-BTC", df([0, 86_400_000]))
    assert len(paths) == 2 and all(p.exists() for p in paths)
    assert {p.parent.name for p in paths} == {"date=1970-01-01", "date=1970-01-02"}
    assert st.read("ohlcv", "upbit", "KRW-BTC")["open_ts"].to_list() == [0, 86_400_000]


def test_compaction_dedupes(tmp_path):
    st = ParquetStore(tmp_path)
    st.write_compacted("ohlcv", "upbit", "KRW-BTC", df([0, 3_600_000]))
    st.write_compacted("ohlcv", "upbit", "KRW-BTC", df([3_600_000, 7_200_000]))
    files = list((tmp_path / "market").rglob("*.parquet"))
    assert len(files) == 1
    assert st.read("ohlcv", "upbit", "KRW-BTC").height == 3


def test_validate_cleans_tmp_quarantines_corrupt_and_dupes(tmp_path):
    st = ParquetStore(tmp_path)
    st.write("ohlcv", "upbit", "KRW-BTC", df([0, 3_600_000]))
    st.write("ohlcv", "upbit", "KRW-BTC", df([3_600_000]))  # duplicate row in second part file
    d = st.partition_dir("ohlcv", "upbit", "KRW-BTC", "1970-01-01")
    (d / ".tmp-crashed.parquet.tmp").write_bytes(b"partial")
    (d / "part-bad.parquet").write_bytes(b"not parquet")
    rep = st.validate(repair=True)
    assert rep.tmp_removed and rep.corrupt_quarantined and rep.duplicate_rows == 1
    assert not list(d.glob(".tmp-*"))
    assert (tmp_path / "quarantine").exists()
    assert st.read("ohlcv", "upbit", "KRW-BTC", dedupe=False).height == 2


def test_readers_never_see_tmp_files(tmp_path):
    st = ParquetStore(tmp_path)
    d = st.partition_dir("trades", "upbit", "KRW-BTC", "1970-01-01")
    d.mkdir(parents=True)
    pl.DataFrame({"event_ts": [1]}).write_parquet(d / ".tmp-inflight.parquet.tmp")
    pl.DataFrame({"event_ts": [2]}).write_parquet(d / ".tmp-legacy.parquet")  # hidden files are ignored too
    assert st.read("trades", "upbit", "KRW-BTC").is_empty()


def test_write_failure_raises_and_sink_keeps_rows(tmp_path, monkeypatch):
    st = ParquetStore(tmp_path)
    sink = BufferedSink(st, "trades", "upbit", max_rows=1)
    sink.add("KRW-BTC", {"event_ts": 1, "price": 1.0}, 0)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", boom)
    with pytest.raises(StorageError):
        sink.flush()
    assert sink.write_errors == 1 and len(sink.buf["KRW-BTC"]) == 1  # nothing lost
    assert not list(tmp_path.rglob(".tmp-*"))
