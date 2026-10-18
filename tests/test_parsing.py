import io
import zipfile

import polars as pl

from crypto_trader.collectors.binance.archive import parse_funding, parse_klines, parse_metrics
from crypto_trader.collectors.bybit.ws import parse_liquidation as bybit_liq
from crypto_trader.collectors.okx.client import parse_liquidation as okx_liq
from crypto_trader.collectors.okx.client import swap_inst
from crypto_trader.collectors.upbit.rest import parse_candle, parse_orderbook, parse_trade_tick
from crypto_trader.core.models import Aggressor


def test_upbit_candle_uses_utc_and_close_availability():
    row = {"market": "KRW-BTC", "candle_date_time_utc": "2022-01-01T23:00:00", "candle_date_time_kst": "2022-01-02T08:00:00",
           "opening_price": 1.0, "high_price": 2.0, "low_price": 0.5, "trade_price": 1.5, "candle_acc_trade_volume": 3.0,
           "candle_acc_trade_price": 4.5, "timestamp": 1641081600094, "unit": 60}
    b = parse_candle(row, "1h")
    assert b.open_ts == 1641078000000
    assert b.close_ts == b.open_ts + 3_600_000 == b.available_ts
    assert (b.open, b.high, b.low, b.close) == (1.0, 2.0, 0.5, 1.5)


def test_upbit_trade_aggressor_and_seq():
    t = parse_trade_tick({"market": "KRW-BTC", "timestamp": 10, "trade_price": "100", "trade_volume": "0.1",
                          "ask_bid": "BID", "sequential_id": 99}, received_ts=20)
    assert t.aggressor == Aggressor.BUY and t.seq == 99 and t.received_ts == 20
    ws = parse_trade_tick({"code": "KRW-ETH", "trade_timestamp": 11, "timestamp": 12, "trade_price": 1, "trade_volume": 2,
                           "ask_bid": "ASK", "sequential_id": 1}, 30)
    assert ws.symbol == "KRW-ETH" and ws.aggressor == Aggressor.SELL


def test_upbit_orderbook_parse_and_spread():
    ob = parse_orderbook({"market": "KRW-BTC", "timestamp": 5, "orderbook_units": [
        {"bid_price": 99, "bid_size": 1, "ask_price": 101, "ask_size": 2}]}, 6)
    assert ob.best_bid == 99 and ob.best_ask == 101
    assert abs(ob.spread_bps - 200) < 1e-9


def test_binance_kline_header_and_microsecond_timestamps():
    with_header = b"open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n" \
                  b"1640995200000,1,2,0.5,1.5,10,1640998799999,15,7,4,6,0\n"
    no_header_us = b"1788220800000000,1,2,0.5,1.5,10,1788224399999999,15,7,4,6,0\n"
    a, b = parse_klines(with_header), parse_klines(no_header_us)
    assert a["open_ts"][0] == 1640995200000 and a["close_ts"][0] == 1640998800000
    assert b["open_ts"][0] == 1788220800000 and b["close_ts"][0] == 1788224400000  # us -> ms
    assert a["taker_buy_volume"][0] == 4.0


def test_binance_metrics_and_funding():
    m = parse_metrics(b"create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
                      b"sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
                      b"2021-12-01 00:05:00,BTCUSDT,59192.974,3365451460.62,4.1,1.13,3.9,0.82\n")
    assert m["event_ts"][0] == 1638317100000 and m["available_ts"][0] == 1638317400000
    f = parse_funding(b"calc_time,funding_interval_hours,last_funding_rate\n1640995200006,8,0.0001\n")
    assert f["funding_rate"][0] == 0.0001 and f["is_settled"][0]


def test_liquidation_parsers_normalise_side_and_notional():
    rows = okx_liq({"data": [{"instId": "BTC-USDT-SWAP", "details": [{"bkPx": "100", "sz": "3", "posSide": "long",
                                                                       "side": "sell", "ts": "7"}]}]}, 9, {"BTC-USDT-SWAP": 0.01})
    assert rows[0]["side"] == "long_liq" and abs(rows[0]["notional_usd"] - 3.0) < 1e-9
    # Bybit docs: "When you receive a Buy update, this means that a long position has been liquidated."
    b = bybit_liq({"topic": "allLiquidation.BTCUSDT",
                   "data": [{"T": 5, "s": "BTCUSDT", "S": "Buy", "v": "2", "p": "10"},
                            {"T": 6, "s": "BTCUSDT", "S": "Sell", "v": "1", "p": "10"}]}, 6)
    assert [x["side"] for x in b] == ["long_liq", "short_liq"] and b[0]["notional_usd"] == 20.0
    assert bybit_liq({"op": "pong", "success": True}, 1) == []
    assert swap_inst("ETHUSDT") == "ETH-USDT-SWAP"


def test_zip_roundtrip_unzip():
    from crypto_trader.collectors.binance.archive import BinanceArchive
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "a,b\n1,2\n")
    assert BinanceArchive._unzip(buf.getvalue()) == b"a,b\n1,2\n"
    assert isinstance(pl.DataFrame(), pl.DataFrame)


def test_binance_metrics_with_empty_ratio_fields():
    m = parse_metrics(b"create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
                      b"sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
                      b"2023-01-01 00:00:00,BTCUSDT,1.0,2.0,,,,\n")
    assert m["oi_usd"][0] == 2.0 and m["global_ls_ratio"][0] is None
