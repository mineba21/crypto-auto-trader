"""Real public API smoke tests (no keys). Run with: uv run pytest -m network"""

import asyncio

import pytest

from crypto_trader.collectors.http import AccessBlocked
from crypto_trader.collectors.upbit.rest import UpbitRest

pytestmark = pytest.mark.network


def test_upbit_public_rest():
    async def go():
        r = UpbitRest()
        try:
            mk = await r.krw_markets()
            ob = await r.orderbook(["KRW-BTC"])
            return mk, ob
        finally:
            await r.close()

    mk, ob = asyncio.run(go())
    assert "KRW-BTC" in mk and "KRW-USDT" in mk
    assert ob[0].best_ask > ob[0].best_bid > 0


def test_binance_fapi_reports_block_instead_of_bypass():
    from crypto_trader.collectors.binance.futures_rest import BinanceFuturesRest

    async def go():
        c = BinanceFuturesRest()
        try:
            return await c.price("BTCUSDT")
        finally:
            await c.close()

    try:
        assert float(asyncio.run(go())["price"]) > 0
    except AccessBlocked as e:
        assert e.status in (403, 451)
