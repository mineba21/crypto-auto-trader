"""Binance USD-M all-market liquidation stream (``!forceOrder@arr``, update speed 1000 ms).

Docs: https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/All-Market-Liquidation-Order-Streams
* Only the LATEST liquidation per symbol within each 1000 ms window is pushed -> totals are lower bounds.
* ``o.S == "SELL"`` is a forced sell, i.e. a LONG position was liquidated (``long_liq``); ``"BUY"`` -> ``short_liq``.
* notional = average price ``ap`` x accumulated filled qty ``z``; event time = trade time ``o.T``.
"""

from __future__ import annotations

from crypto_trader.collectors.liquidation_stream import StreamStats, run_liquidation_stream
from crypto_trader.storage.parquet_store import ParquetStore

WS_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"


def parse_liquidation(msg: dict, received_ts: int) -> list[dict]:
    events = msg if isinstance(msg, list) else [msg]
    out = []
    for m in events:
        if not isinstance(m, dict) or m.get("e") != "forceOrder" or "o" not in m:
            continue
        o = m["o"]
        qty = float(o.get("z") or o.get("q") or 0)
        price = float(o.get("ap") or o.get("p") or 0)
        out.append({"event_ts": int(o.get("T") or m.get("E")), "received_ts": received_ts, "available_ts": received_ts,
                    "symbol": o["s"], "side": "long_liq" if o.get("S") == "SELL" else "short_liq",
                    "price": price, "qty": qty, "notional_usd": price * qty, "source": "binance_ws"})
    return out


async def run_liquidations(symbols: list[str], store: ParquetStore, duration_s: float | None = None,
                           stats: StreamStats | None = None) -> StreamStats:
    # the stream URL itself subscribes; Binance answers server pings automatically (websockets handles pong)
    return await run_liquidation_stream("binance", WS_URL, None, parse_liquidation, symbols, store, duration_s,
                                        stats=stats)
