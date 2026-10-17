"""OKX V5 public market data (read-only), used as an alternate/cross-check derivatives source.

https://www.okx.com/docs-v5/en/ . Contract quantities (``sz``) are in contracts; notional = sz * ctVal * price.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from crypto_trader.collectors.http import ApiError, HttpClient
from crypto_trader.core.timeutils import now_ms
from crypto_trader.storage.parquet_store import BufferedSink, ParquetStore

log = logging.getLogger(__name__)
BASE_URL = "https://www.okx.com"
WS_URL = "wss://ws.okx.com/ws/v5/public"  # port 443; :8443 was reset from our test network


def swap_inst(binance_symbol: str) -> str:
    """BTCUSDT -> BTC-USDT-SWAP"""
    base = binance_symbol.removesuffix("USDT")
    return f"{base}-USDT-SWAP"


class OkxRest:
    exchange = "okx"

    def __init__(self, client: HttpClient | None = None):
        self.http = client or HttpClient(BASE_URL, rate_per_sec=4)
        self._ct_val: dict[str, float] = {}

    async def close(self) -> None:
        await self.http.close()

    async def _get(self, path: str, params: dict) -> list:
        body = await self.http.get(path, params=params)
        if str(body.get("code")) != "0":
            raise ApiError(200, str(body), path)
        return body["data"]

    async def ct_val(self, inst_id: str) -> float:
        if inst_id not in self._ct_val:
            rows = await self._get("/api/v5/public/instruments", {"instType": "SWAP", "instId": inst_id})
            self._ct_val[inst_id] = float(rows[0]["ctVal"])
        return self._ct_val[inst_id]

    async def ticker(self, inst_id: str) -> dict:
        return (await self._get("/api/v5/market/ticker", {"instId": inst_id}))[0]

    async def mark_price(self, inst_id: str) -> dict:
        return (await self._get("/api/v5/public/mark-price", {"instType": "SWAP", "instId": inst_id}))[0]

    async def open_interest(self, inst_id: str) -> dict:
        return (await self._get("/api/v5/public/open-interest", {"instType": "SWAP", "instId": inst_id}))[0]

    async def funding_rate(self, inst_id: str) -> dict:
        """Current period funding: ``fundingRate`` here is the running *estimate*, not yet settled."""
        return (await self._get("/api/v5/public/funding-rate", {"instId": inst_id}))[0]

    async def funding_history(self, inst_id: str, start_ms: int, end_ms: int) -> list[dict]:
        out, after = [], end_ms
        while True:
            rows = await self._get("/api/v5/public/funding-rate-history", {"instId": inst_id, "after": after, "limit": 400})
            if not rows:
                break
            out += rows
            oldest = min(int(r["fundingTime"]) for r in rows)
            if oldest <= start_ms or oldest >= after:
                break
            after = oldest
        return [r for r in out if start_ms <= int(r["fundingTime"]) < end_ms]

    async def oi_history(self, inst_id: str, period: str = "1H", start_ms: int | None = None,
                         end_ms: int | None = None) -> list[list]:
        """[ts, oi_contracts, oi_ccy, oi_usd]; OKX serves a limited trailing window only."""
        params: dict = {"instId": inst_id, "period": period, "limit": 100}
        out, end = [], end_ms
        while True:
            if end:
                params["end"] = end
            if start_ms:
                params["begin"] = start_ms
            rows = await self._get("/api/v5/rubik/stat/contracts/open-interest-history", params)
            if not rows:
                break
            out += rows
            oldest = min(int(r[0]) for r in rows)
            if (start_ms and oldest <= start_ms) or (end and oldest >= end) or len(rows) < 100:
                break
            end = oldest - 1
        return out

    async def history_candles(self, inst_id: str, bar: str, start_ms: int, end_ms: int) -> list[list]:
        """[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm] newest-first pages of 100."""
        out, after = [], end_ms
        while True:
            rows = await self._get("/api/v5/market/history-candles", {"instId": inst_id, "bar": bar, "after": after,
                                                                      "limit": 100})
            if not rows:
                break
            out += rows
            oldest = min(int(r[0]) for r in rows)
            if oldest <= start_ms or oldest >= after:
                break
            after = oldest
        return [r for r in out if start_ms <= int(r[0]) < end_ms]


def parse_liquidation(msg: dict, received_ts: int, ct_vals: dict[str, float]) -> list[dict]:
    out = []
    for d in msg.get("data", []):
        inst = d.get("instId", "")
        for det in d.get("details", []):
            px, sz = float(det.get("bkPx") or 0), float(det.get("sz") or 0)
            ct = ct_vals.get(inst)
            out.append({
                "event_ts": int(det["ts"]), "received_ts": received_ts, "available_ts": received_ts,
                "symbol": inst, "side": "long_liq" if det.get("posSide") == "long" or det.get("side") == "sell" else "short_liq",
                "price": px, "qty": sz, "notional_usd": px * sz * ct if ct else None, "source": "okx_ws",
            })
    return out


async def run_liquidations(inst_ids: list[str], store: ParquetStore, rest: OkxRest | None = None,
                           duration_s: float | None = None) -> int:
    """Capture OKX SWAP liquidation-orders (all instruments) and keep the requested ones."""
    rest = rest or OkxRest()
    ct_vals = {i: await rest.ct_val(i) for i in inst_ids}
    sink = BufferedSink(store, "liquidations", "okx", max_age_s=30)
    deadline = time.time() + duration_s if duration_s else None
    n, backoff = 0, 1.0
    while deadline is None or time.time() < deadline:
        try:
            async with websockets.connect(WS_URL, open_timeout=10, ping_interval=20) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]}))
                backoff = 1.0
                while deadline is None or time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 5)
                    except TimeoutError:
                        continue
                    if raw == "pong":
                        continue
                    msg = json.loads(raw)
                    if msg.get("arg", {}).get("channel") != "liquidation-orders" or "data" not in msg:
                        continue
                    for row in parse_liquidation(msg, now_ms(), ct_vals):
                        if row["symbol"] in ct_vals:
                            sink.add(row["symbol"], row, time.time())
                            n += 1
                    if sink.due(time.time()):
                        sink.flush()
        except (OSError, websockets.WebSocketException, TimeoutError) as e:
            log.warning("okx ws: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)
    sink.flush()
    return n
