import asyncio
import json

import numpy as np
import polars as pl
import websockets

from crypto_trader.collectors.binance.liquidations import parse_liquidation as binance_liq
from crypto_trader.collectors.liquidation_stream import StreamStats, run_liquidation_stream
from crypto_trader.features.derivatives import liquidation_bars
from crypto_trader.storage.parquet_store import ParquetStore

H = 3_600_000


def force_order(sym, side, ap, z, t):
    return {"e": "forceOrder", "E": t + 5, "o": {"s": sym, "S": side, "o": "LIMIT", "f": "IOC", "q": str(z), "p": "1",
                                                  "ap": str(ap), "X": "FILLED", "l": str(z), "z": str(z), "T": t}}


def test_binance_force_order_parse():
    rows = binance_liq(force_order("BTCUSDT", "SELL", 100.0, 0.5, 1000), 2000)
    assert rows == [{"event_ts": 1000, "received_ts": 2000, "available_ts": 2000, "symbol": "BTCUSDT",
                     "side": "long_liq", "price": 100.0, "qty": 0.5, "notional_usd": 50.0, "source": "binance_ws"}]
    both = binance_liq([force_order("ETHUSDT", "BUY", 10, 2, 1), {"e": "other"}], 3)
    assert [r["side"] for r in both] == ["short_liq"] and binance_liq({"result": None, "id": 1}, 1) == []


def test_stream_loop_stores_wanted_symbols_and_records_reconnect_gap(tmp_path):
    connections = []

    async def handler(ws):
        connections.append(1)
        await ws.send(json.dumps(force_order("BTCUSDT", "SELL", 100, 1, 1_000 + len(connections))))
        await ws.send(json.dumps(force_order("DOGEUSDT", "SELL", 1, 1, 2_000)))  # not wanted
        await asyncio.sleep(0.2)
        await ws.close()  # force a reconnect

    async def main():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            st = StreamStats()
            await run_liquidation_stream("binance", f"ws://127.0.0.1:{port}", None, binance_liq, ["BTCUSDT"],
                                         ParquetStore(tmp_path), duration_s=3.5, stats=st)
            return st

    st = asyncio.run(main())
    store = ParquetStore(tmp_path)
    liq = store.read("liquidations", "binance", "BTCUSDT", dedupe=False)
    assert len(connections) >= 2 and st.reconnects >= 1
    assert liq.height == st.rows >= 2 and set(liq["side"]) == {"long_liq"}
    assert store.read("liquidations", "binance", "DOGEUSDT").is_empty()
    gaps = store.read("gaps", "binance", "BTCUSDT")
    assert gaps.height >= 1 and set(gaps["stream"]) == {"liquidations"}


def _liq(rows):
    return pl.DataFrame(rows, schema={"event_ts": pl.Int64, "side": pl.Utf8, "notional_usd": pl.Float64, "source": pl.Utf8},
                        orient="row")


def test_liquidation_bars_coverage_zero_fill_gaps_and_availability():
    t0 = 100 * H
    rows = [(t0 + 10, "long_liq", 5.0, "binance"), (t0 + 2 * H + 1, "long_liq", 7.0, "binance"),
            (t0 + 2 * H + 2, "short_liq", 3.0, "binance"), (t0 + 6 * H + 5, "long_liq", 1.0, "binance")]
    lb = liquidation_bars(_liq(rows), H, gaps=[(t0 + 4 * H + 100, t0 + 4 * H + 200)])
    # first full bucket after the first event .. last full bucket before the last event
    assert lb["event_ts"].to_list() == [t0 + k * H for k in range(1, 6)]
    assert lb["available_ts"].to_list() == [t0 + k * H + H for k in range(1, 6)]  # usable only after close
    assert lb["long_liq_usd"].to_list() == [0.0, 7.0, 0.0, None, 0.0]  # quiet hour = 0, gap hour = unknown
    assert lb["short_liq_usd"][1] == 3.0


def test_liquidation_bars_use_window_covered_by_every_source():
    t0 = 100 * H
    rows = [(t0, "long_liq", 1.0, "okx"), (t0 + 10 * H, "long_liq", 1.0, "okx"),
            (t0 + 3 * H + 1, "long_liq", 2.0, "binance"), (t0 + 8 * H, "long_liq", 2.0, "binance")]
    lb = liquidation_bars(_liq(rows), H)
    assert lb["event_ts"].min() == t0 + 4 * H and lb["event_ts"].max() == t0 + 7 * H
    assert np.isfinite(lb["long_liq_usd"].to_numpy()).all()
    assert liquidation_bars(_liq([]), H).is_empty()


def test_liquidations_attach_to_bar_closing_at_bucket_end_only():
    from crypto_trader.features.derivatives import asof_align

    t0 = 100 * H
    lb = liquidation_bars(_liq([(t0, "long_liq", 1.0, "okx"), (t0 + H + 5, "long_liq", 9.0, "okx"),
                                (t0 + 4 * H, "long_liq", 1.0, "okx")]), H)
    bars = pl.DataFrame({"close_ts": [t0 + H, t0 + 2 * H, t0 + 3 * H, t0 + 9 * H]})
    out = asof_align(bars, lb, ["long_liq_usd"], "", max_age_ms=H)
    # the 9.0 bucket [t0+H, t0+2H) is visible to the bar closing at t0+2H, not to the one closing at t0+H
    assert out["long_liq_usd"].to_list() == [None, 9.0, 0.0, None]
