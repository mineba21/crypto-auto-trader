"""Bybit public WS liquidation capture (topic ``allLiquidation.{symbol}``).

Liquidation streams are *samples*: exchanges may aggregate or throttle events, so totals are lower bounds.
Run only from a network/jurisdiction where Bybit permits access.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from crypto_trader.core.timeutils import now_ms
from crypto_trader.storage.parquet_store import BufferedSink, ParquetStore

log = logging.getLogger(__name__)
WS_URL = "wss://stream.bybit.com/v5/public/linear"


def parse_liquidation(msg: dict, received_ts: int) -> list[dict]:
    out = []
    for d in msg.get("data", []):
        # Bybit side = side of the liquidated position's closing order: "Buy" -> a short was liquidated.
        side = d.get("S")
        out.append({"event_ts": int(d["T"]), "received_ts": received_ts, "available_ts": received_ts,
                    "symbol": d["s"], "side": "short_liq" if side == "Buy" else "long_liq",
                    "price": float(d["p"]), "qty": float(d["v"]), "notional_usd": float(d["p"]) * float(d["v"]),
                    "source": "bybit_ws"})
    return out


async def run_liquidations(symbols: list[str], store: ParquetStore, duration_s: float | None = None) -> int:
    sink = BufferedSink(store, "liquidations", "bybit", max_age_s=30)
    deadline = time.time() + duration_s if duration_s else None
    n, backoff = 0, 1.0
    while deadline is None or time.time() < deadline:
        try:
            async with websockets.connect(WS_URL, open_timeout=10, ping_interval=20) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": [f"allLiquidation.{s}" for s in symbols]}))
                backoff = 1.0
                while deadline is None or time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 5)
                    except TimeoutError:
                        continue
                    msg = json.loads(raw)
                    if msg.get("topic", "").startswith("allLiquidation"):
                        for row in parse_liquidation(msg, now_ms()):
                            sink.add(row["symbol"], row, time.time())
                            n += 1
                    if sink.due(time.time()):
                        sink.flush()
        except (OSError, websockets.WebSocketException, TimeoutError) as e:
            log.warning("bybit ws: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)
    sink.flush()
    return n
