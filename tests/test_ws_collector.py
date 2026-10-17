import asyncio
import json

from crypto_trader.collectors.upbit.ws import UpbitWsCollector
from crypto_trader.core.models import Aggressor, Trade
from crypto_trader.storage.parquet_store import ParquetStore


class FakeRest:
    def __init__(self, ticks):
        self.ticks = ticks

    async def recent_trades(self, symbol, count=500):
        return self.ticks

    async def close(self):
        pass


def msg(seq, ts, price=1.0):
    return json.dumps({"type": "trade", "code": "KRW-BTC", "timestamp": ts, "trade_timestamp": ts, "trade_price": price,
                       "trade_volume": 1.0, "ask_bid": "BID", "sequential_id": seq})


def test_dedupe_by_sequence_and_sink(tmp_path):
    got = []
    c = UpbitWsCollector(["KRW-BTC"], ParquetStore(tmp_path), rest=FakeRest([]), on_trade=got.append)
    for m in (msg(1, 10), msg(2, 20), msg(2, 20), msg(1, 10)):
        asyncio.run(c.handle_message(m))
    assert [t.seq for t in got] == [1, 2]
    c.flush(force=True)
    assert ParquetStore(tmp_path).read("trades", "upbit", "KRW-BTC").height == 2


def test_stale_orderbook_snapshot_dropped(tmp_path):
    got = []
    c = UpbitWsCollector(["KRW-BTC"], None, rest=FakeRest([]), on_orderbook=got.append)
    ob = lambda ts: json.dumps({"type": "orderbook", "code": "KRW-BTC", "timestamp": ts, "orderbook_units": [  # noqa: E731
        {"bid_price": 1, "bid_size": 1, "ask_price": 2, "ask_size": 1}]})
    for ts in (10, 30, 20):
        asyncio.run(c.handle_message(ob(ts)))
    assert [o.event_ts for o in got] == [10, 30] and c.health.out_of_order == 1


def test_reconnect_backfill_replays_and_records_unrecoverable_gap(tmp_path):
    t = lambda s, ts: Trade("upbit", "KRW-BTC", ts, ts, 1.0, 1.0, Aggressor.BUY, s)  # noqa: E731
    c = UpbitWsCollector(["KRW-BTC"], ParquetStore(tmp_path), rest=FakeRest([t(3, 30), t(4, 40), t(2, 20), t(1, 10)]))
    asyncio.run(c.handle_message(msg(1, 10)))
    asyncio.run(c.backfill_trades(disconnected_at=15))
    assert c.last_seq["KRW-BTC"] == 4  # 2,3,4 replayed in order
    # REST window overlaps the last WS trade (seq 1) -> no trade gap, only the orderbook gap
    assert [g["stream"] for g in c.health.gaps] == ["orderbook"]
    c2 = UpbitWsCollector(["KRW-BTC"], None, rest=FakeRest([t(50, 500)]))
    asyncio.run(c2.handle_message(msg(1, 10)))
    asyncio.run(c2.backfill_trades(disconnected_at=15))
    assert c2.health.gaps[0]["stream"] == "trades"
