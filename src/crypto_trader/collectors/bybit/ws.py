"""Bybit public WS liquidation capture (topic ``allLiquidation.{symbol}``, pushed every 500 ms).

Side semantics (docs: https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation):
"When you receive a ``Buy`` update, this means that a long position has been liquidated."
So ``S == "Buy"`` -> ``long_liq`` and ``S == "Sell"`` -> ``short_liq``. ``p`` is the bankruptcy price.
Run only from a network/jurisdiction where Bybit permits access.
"""

from __future__ import annotations

from crypto_trader.collectors.liquidation_stream import StreamStats, run_liquidation_stream
from crypto_trader.storage.parquet_store import ParquetStore

WS_URL = "wss://stream.bybit.com/v5/public/linear"


def parse_liquidation(msg: dict, received_ts: int) -> list[dict]:
    if not str(msg.get("topic", "")).startswith("allLiquidation"):
        return []
    out = []
    for d in msg.get("data", []):
        price, qty = float(d["p"]), float(d["v"])
        out.append({"event_ts": int(d["T"]), "received_ts": received_ts, "available_ts": received_ts,
                    "symbol": d["s"], "side": "long_liq" if d.get("S") == "Buy" else "short_liq",
                    "price": price, "qty": qty, "notional_usd": price * qty, "source": "bybit_ws"})
    return out


async def run_liquidations(symbols: list[str], store: ParquetStore, duration_s: float | None = None,
                           stats: StreamStats | None = None) -> StreamStats:
    return await run_liquidation_stream(
        "bybit", WS_URL, {"op": "subscribe", "args": [f"allLiquidation.{s}" for s in symbols]}, parse_liquidation,
        symbols, store, duration_s, app_ping={"op": "ping"}, ping_every_s=20, stats=stats)
