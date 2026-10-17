"""Upbit public WebSocket collector (trade / orderbook / ticker) with reconnect and trade gap recovery.

Gap handling:
* Each trade carries ``sequential_id`` (unique, increasing per market). After a reconnect the collector pulls
  recent ticks over REST and replays those newer than the last seen sequence id.
* If the oldest REST tick is still newer than the last seen trade, the hole cannot be recovered (REST keeps only
  a limited window); a ``gap`` record is written so CVD/order-flow features over that window are invalidated.
* Orderbook snapshots cannot be back-filled: reconnect windows are recorded as gaps and OBI is invalid there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import websockets

from crypto_trader.collectors.upbit.rest import UpbitRest, parse_orderbook, parse_trade_tick
from crypto_trader.core.models import OrderBookSnapshot, Trade
from crypto_trader.core.timeutils import now_ms
from crypto_trader.storage.parquet_store import BufferedSink, ParquetStore

log = logging.getLogger(__name__)

WS_URL = "wss://api.upbit.com/websocket/v1"


def trade_row(t: Trade) -> dict:
    return {"event_ts": t.event_ts, "received_ts": t.received_ts, "available_ts": t.received_ts, "price": t.price,
            "volume": t.volume, "aggressor": str(t.aggressor), "seq": t.seq, "source": "ws"}


def orderbook_row(ob: OrderBookSnapshot, depth: int = 15) -> dict:
    row: dict = {"event_ts": ob.event_ts, "received_ts": ob.received_ts, "available_ts": ob.received_ts,
                 "levels": len(ob.levels)}
    for i, lv in enumerate(ob.levels[:depth]):
        row[f"bp{i}"], row[f"bs{i}"], row[f"ap{i}"], row[f"as{i}"] = lv.bid_price, lv.bid_size, lv.ask_price, lv.ask_size
    return row


@dataclass
class StreamHealth:
    connected: bool = False
    reconnects: int = 0
    last_msg_ms: dict[str, int] = field(default_factory=dict)
    gaps: list[dict] = field(default_factory=list)
    out_of_order: int = 0
    last_error: str = ""


class UpbitWsCollector:
    def __init__(
        self,
        symbols: list[str],
        store: ParquetStore | None,
        rest: UpbitRest | None = None,
        on_trade: Callable[[Trade], Awaitable[None] | None] | None = None,
        on_orderbook: Callable[[OrderBookSnapshot], Awaitable[None] | None] | None = None,
        url: str = WS_URL,
        flush_s: float = 30.0,
    ):
        self.symbols = symbols
        self.rest = rest or UpbitRest()
        self.on_trade, self.on_orderbook = on_trade, on_orderbook
        self.url = url
        self.health = StreamHealth()
        self.last_seq: dict[str, int] = {}  # highest sequential_id seen (gap detection)
        self.seen: dict[str, deque] = {}  # recent ids: fills of one order can arrive out of id order
        self._seen_set: dict[str, set] = {}
        self.last_trade_ts: dict[str, int] = {}
        self.last_ob_ts: dict[str, int] = {}
        self.sinks = {}
        if store is not None:
            self.sinks = {
                "trades": BufferedSink(store, "trades", "upbit", max_age_s=flush_s),
                "orderbook": BufferedSink(store, "orderbook", "upbit", max_age_s=flush_s),
                "gaps": BufferedSink(store, "gaps", "upbit", max_rows=1, max_age_s=0),
            }
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def subscription(self) -> list[dict]:
        return [
            {"ticket": f"cat-{uuid.uuid4().hex[:12]}"},
            {"type": "trade", "codes": self.symbols},
            {"type": "orderbook", "codes": self.symbols},
            {"format": "DEFAULT"},
        ]

    async def _emit(self, cb, obj) -> None:
        if cb is None:
            return
        r = cb(obj)
        if asyncio.iscoroutine(r):
            await r

    def _record_gap(self, symbol: str, stream: str, start_ms: int, end_ms: int, reason: str) -> None:
        g = {"event_ts": start_ms, "end_ts": end_ms, "stream": stream, "reason": reason, "received_ts": now_ms()}
        self.health.gaps.append({"symbol": symbol, **g})
        del self.health.gaps[:-200]
        if "gaps" in self.sinks:
            self.sinks["gaps"].add(symbol, g, time.time())
        log.warning("gap %s %s %s-%s %s", symbol, stream, start_ms, end_ms, reason)

    async def handle_trade(self, t: Trade) -> None:
        if t.seq is not None:
            seen = self.seen.setdefault(t.symbol, deque(maxlen=20_000))
            if t.seq in self._seen_set.setdefault(t.symbol, set()):
                return  # duplicate (e.g. replayed by backfill)
            if len(seen) == seen.maxlen:
                self._seen_set[t.symbol].discard(seen[0])
            seen.append(t.seq)
            self._seen_set[t.symbol].add(t.seq)
        if t.event_ts < self.last_trade_ts.get(t.symbol, 0):
            self.health.out_of_order += 1
        if t.seq is not None:
            self.last_seq[t.symbol] = max(t.seq, self.last_seq.get(t.symbol, 0))
        self.last_trade_ts[t.symbol] = max(t.event_ts, self.last_trade_ts.get(t.symbol, 0))
        if "trades" in self.sinks:
            self.sinks["trades"].add(t.symbol, trade_row(t), time.time())
        await self._emit(self.on_trade, t)

    async def handle_orderbook(self, ob: OrderBookSnapshot) -> None:
        if ob.event_ts < self.last_ob_ts.get(ob.symbol, 0):
            self.health.out_of_order += 1
            return  # stale snapshot: never let it overwrite newer state
        self.last_ob_ts[ob.symbol] = ob.event_ts
        if "orderbook" in self.sinks:
            self.sinks["orderbook"].add(ob.symbol, orderbook_row(ob), time.time())
        await self._emit(self.on_orderbook, ob)

    async def handle_message(self, raw: bytes | str) -> None:
        msg = json.loads(raw)
        rts = now_ms()
        typ = msg.get("type")
        self.health.last_msg_ms[typ or "?"] = rts
        if typ == "trade":
            await self.handle_trade(parse_trade_tick(msg, rts))
        elif typ == "orderbook":
            await self.handle_orderbook(parse_orderbook(msg, rts))
        elif "error" in msg:
            self.health.last_error = str(msg["error"])
            log.error("upbit ws error %s", msg)

    async def backfill_trades(self, disconnected_at: int) -> None:
        """After reconnect, replay REST ticks newer than the last WS trade; record unrecoverable holes."""
        for sym in self.symbols:
            last = self.last_seq.get(sym)
            if last is None:
                continue
            try:
                ticks = await self.rest.recent_trades(sym, count=500)
            except Exception as e:  # REST failure -> the window is a gap
                self._record_gap(sym, "trades", self.last_trade_ts.get(sym, disconnected_at), now_ms(), f"backfill failed: {e}")
                continue
            ticks = sorted(ticks, key=lambda t: (t.seq or 0))
            if ticks and (ticks[0].seq or 0) > last:
                self._record_gap(sym, "trades", self.last_trade_ts.get(sym, disconnected_at), ticks[0].event_ts,
                                 "REST window does not reach last WS trade")
            for t in ticks:
                if (t.seq or 0) > last:
                    await self.handle_trade(Trade(t.exchange, t.symbol, t.event_ts, t.received_ts, t.price, t.volume,
                                                  t.aggressor, t.seq))
            self._record_gap(sym, "orderbook", disconnected_at, now_ms(), "ws reconnect; orderbook not recoverable")

    def flush(self, force: bool = False) -> None:
        now_s = time.time()
        for sink in self.sinks.values():
            if force or sink.due(now_s):
                sink.flush()

    async def run(self, duration_s: float | None = None) -> None:
        deadline = time.time() + duration_s if duration_s else None
        backoff = 1.0
        disconnected_at: int | None = None
        while not self._stop.is_set() and (deadline is None or time.time() < deadline):
            try:
                async with websockets.connect(self.url, open_timeout=10, ping_interval=30, ping_timeout=20,
                                              max_size=2**22) as ws:
                    await ws.send(json.dumps(self.subscription()))
                    self.health.connected = True
                    if disconnected_at is not None:
                        self.health.reconnects += 1
                        await self.backfill_trades(disconnected_at)
                        disconnected_at = None
                    backoff = 1.0
                    while not self._stop.is_set():
                        timeout = 5.0 if deadline is None else max(0.1, min(5.0, deadline - time.time()))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout)
                            await self.handle_message(raw)
                        except TimeoutError:
                            pass
                        self.flush()
                        if deadline is not None and time.time() >= deadline:
                            break
            except (OSError, websockets.WebSocketException, TimeoutError) as e:
                self.health.last_error = f"{type(e).__name__}: {e}"
                log.warning("upbit ws disconnected: %s; reconnect in %.1fs", e, backoff)
            self.health.connected = False
            if disconnected_at is None:
                disconnected_at = now_ms()
            if self._stop.is_set() or (deadline is not None and time.time() >= deadline):
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        self.flush(force=True)
