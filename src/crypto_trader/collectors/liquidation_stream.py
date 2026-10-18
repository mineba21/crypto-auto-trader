"""Shared WebSocket loop for exchange liquidation streams (Binance / Bybit).

Liquidation streams are *samples* (exchanges throttle or aggregate them), so totals are lower bounds.
Every disconnect window is written to the ``gaps`` kind so downstream features can mark it as unknown, not zero.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import websockets

from crypto_trader.core.timeutils import now_ms
from crypto_trader.storage.parquet_store import BufferedSink, ParquetStore

log = logging.getLogger(__name__)


@dataclass
class StreamStats:
    connected: bool = False
    reconnects: int = 0
    rows: int = 0
    last_msg_ms: int | None = None
    last_error: str = ""
    gaps: list[dict] = field(default_factory=list)


async def run_liquidation_stream(
    exchange: str,
    url: str,
    subscribe: dict | None,
    parse: Callable[[dict, int], list[dict]],
    symbols: list[str],
    store: ParquetStore,
    duration_s: float | None = None,
    app_ping: dict | None = None,
    ping_every_s: float = 20.0,
    stats: StreamStats | None = None,
) -> StreamStats:
    """Keep a liquidation WS open, store rows for ``symbols`` only, and record reconnect gaps."""
    stats = stats or StreamStats()
    wanted = set(symbols)
    sink = BufferedSink(store, "liquidations", exchange, max_age_s=30)
    gap_sink = BufferedSink(store, "gaps", exchange, max_rows=1, max_age_s=0)
    deadline = time.time() + duration_s if duration_s else None
    backoff = 1.0
    down_since: int | None = None
    while deadline is None or time.time() < deadline:
        try:
            async with websockets.connect(url, open_timeout=10, ping_interval=20, ping_timeout=20) as ws:
                if subscribe is not None:
                    await ws.send(json.dumps(subscribe))
                stats.connected = True
                if down_since is not None:
                    stats.reconnects += 1
                    for s in symbols:
                        g = {"event_ts": down_since, "end_ts": now_ms(), "stream": "liquidations",
                             "reason": "ws reconnect", "received_ts": now_ms()}
                        gap_sink.add(s, g, time.time())
                        stats.gaps = (stats.gaps + [{"symbol": s, **g}])[-50:]
                    gap_sink.flush()
                    down_since = None
                backoff = 1.0
                last_ping = time.time()
                while deadline is None or time.time() < deadline:
                    if app_ping is not None and time.time() - last_ping >= ping_every_s:
                        await ws.send(json.dumps(app_ping))
                        last_ping = time.time()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 5)
                    except TimeoutError:
                        if sink.due(time.time()):
                            sink.flush()
                        continue
                    stats.last_msg_ms = now_ms()
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    for row in parse(msg, now_ms()):
                        if row["symbol"] in wanted:
                            sink.add(row["symbol"], row, time.time())
                            stats.rows += 1
                    if sink.due(time.time()):
                        sink.flush()
        except (OSError, websockets.WebSocketException, TimeoutError) as e:
            stats.last_error = f"{type(e).__name__}: {e}"[:200]
            log.warning("%s liquidation ws: %s; reconnect in %.0fs", exchange, e, backoff)
        stats.connected = False
        if down_since is None:
            down_since = now_ms()
        sink.flush()
        if deadline is not None and time.time() >= deadline:
            break
        await asyncio.sleep(backoff)
        backoff = min(60.0, backoff * 2)
    sink.flush()
    return stats
