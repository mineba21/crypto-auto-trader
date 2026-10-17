import pytest

from crypto_trader.brokers.backtest import BacktestBroker
from crypto_trader.brokers.paper import PaperBroker, walk_book
from crypto_trader.core.models import (
    Bar,
    BookLevel,
    OrderBookSnapshot,
    OrderRequest,
    OrderState,
    OrderType,
    Side,
)
from crypto_trader.core.timeutils import now_ms


def bar(o=100.0, qv=1e9):
    return Bar("upbit", "KRW-BTC", "1h", 0, 3_600_000, o, o * 1.01, o * 0.99, o, 1e6, qv, 3_600_000)


def buy(krw, coid=None):
    r = OrderRequest("KRW-BTC", Side.BUY, OrderType.MARKET_BUY, "t", price=krw)
    if coid:
        r.client_order_id = coid
    return r


def test_market_buy_fills_next_open_with_fee_and_slippage():
    b = BacktestBroker(100_000, 0.0005, 0.001)
    o = b.submit(buy(50_000))
    assert o.state == OrderState.OPEN and b.cash == pytest.approx(100_000 - 50_000 * 1.0005)
    b.fill_at_open(bar(100.0))
    assert o.state == OrderState.FILLED and o.avg_price == pytest.approx(100.1)
    assert b.coins["KRW-BTC"] == pytest.approx(50_000 / 100.1)
    assert b.locked_cash == pytest.approx(0, abs=1e-6)


def test_partial_fill_cancels_remainder_and_releases_cash():
    b = BacktestBroker(100_000, 0.0005, 0.0, max_participation=0.05)
    o = b.submit(buy(50_000))
    b.fill_at_open(bar(100.0, qv=200_000))  # only 10,000 KRW fillable
    assert o.state == OrderState.CANCELLED and o.filled_volume == pytest.approx(100)
    assert b.cash == pytest.approx(100_000 - 10_000 * 1.0005)


def test_rejections_min_order_funds_and_no_short():
    b = BacktestBroker(10_000, 0.0005, 0.0)
    assert b.submit(buy(4_000)).state == OrderState.REJECTED
    assert b.submit(buy(20_000)).state == OrderState.REJECTED
    sell = OrderRequest("KRW-BTC", Side.SELL, OrderType.MARKET_SELL, "t", volume=1.0)
    assert b.submit(sell).reject_reason == "insufficient_funds_ask"  # long-only: nothing to sell


def test_duplicate_client_order_id_raises():
    b = BacktestBroker(100_000, 0.0005, 0.0)
    b.submit(buy(10_000, "same"))
    with pytest.raises(ValueError):
        b.submit(buy(10_000, "same"))


def test_cancel_releases_locked_cash():
    b = BacktestBroker(100_000, 0.0005, 0.0)
    o = b.submit(buy(10_000))
    b.cancel(o.client_order_id)
    assert b.cash == pytest.approx(100_000) and o.state == OrderState.CANCELLED


def book(ts=None):
    return OrderBookSnapshot("upbit", "KRW-BTC", ts or now_ms(), ts or now_ms(),
                             (BookLevel(99, 1, 101, 1), BookLevel(98, 1, 102, 2)))


def deep_book():
    return OrderBookSnapshot("upbit", "KRW-BTC", now_ms(), now_ms(), (BookLevel(99, 1e4, 101, 1e4),))


def test_walk_book_uses_depth_without_extra_slippage():
    q = walk_book(book(), Side.BUY, krw_amount=101 + 102)
    assert q.volume == pytest.approx(2) and q.price == pytest.approx(101.5)
    q2 = walk_book(book(), Side.SELL, volume=1.5)
    assert q2.price == pytest.approx((99 + 0.5 * 98) / 1.5)


def test_paper_rejects_without_fresh_book():
    p = PaperBroker(100_000, 0.0005)
    assert p.submit(buy(10_000)).state == OrderState.REJECTED
    p.on_orderbook(deep_book())
    o = p.submit(buy(10_000))
    assert o.state == OrderState.FILLED and p.coins["KRW-BTC"] > 0
