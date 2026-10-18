import asyncio
import json

import httpx
import numpy as np
import polars as pl
import pytest

from crypto_trader.collectors.binance.futures_rest import BinanceFuturesRest
from crypto_trader.collectors.binance.live_feed import BinanceLiveDerivatives
from crypto_trader.collectors.http import HttpClient
from crypto_trader.ops.notify import Notifier, describe_config, ping
from crypto_trader.ops.watchdog import Watchdog
from crypto_trader.reports.paper_report import fill_slippage_bps, signal_parity
from crypto_trader.risk import kill_switch as ks_mod
from crypto_trader.risk.kill_switch import KillSwitch
from crypto_trader.storage.ops_db import OpsDB
from crypto_trader.storage.parquet_store import ParquetStore
from crypto_trader.strategies.base import Features
from crypto_trader.strategies.library import build


# ------------------------------------------------------------------ notifier
def test_notifier_sends_dedupes_and_hides_token():
    calls = []

    def h(req):
        calls.append(req)
        return httpx.Response(200, json={"ok": True})

    n = Notifier("TOKEN123", "42", transport=httpx.MockTransport(h))
    assert n.send("hello", key="k", min_interval_s=60)
    assert not n.send("hello again", key="k", min_interval_s=60)  # deduped
    assert n.send("other", key="k2")
    assert len(calls) == 2 and json.loads(calls[0].content)["chat_id"] == "42"
    assert "/botTOKEN123/sendMessage" in str(calls[0].url)


def test_notifier_disabled_without_config_and_never_raises(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "TG_BOT_TOKEN", "TG_TOKEN", "BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "TG_CHAT_ID", "CHAT_ID", "TELEGRAM_CHATID"):
        monkeypatch.delenv(k, raising=False)
    assert not Notifier().enabled and Notifier().send("x") is False

    def boom(req):
        raise httpx.ConnectError("down", request=req)

    n = Notifier("t", "c", transport=httpx.MockTransport(boom))
    assert n.send("x") is False and n.failed == 1


def test_env_aliases_reported_by_name_only(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "secret-value")
    monkeypatch.setenv("TG_CHAT_ID", "7")
    cfg = describe_config()
    assert cfg["telegram_token_key"] == "TG_BOT_TOKEN" and "secret-value" not in json.dumps(cfg)


def test_ping_success_and_fail_suffix(monkeypatch):
    seen = []
    monkeypatch.setenv("HC", "https://hc-ping.com/abc")
    t = httpx.MockTransport(lambda r: (seen.append(str(r.url)), httpx.Response(200))[1])
    assert ping("HC", transport=t) and ping("HC", fail=True, transport=t)
    assert seen == ["https://hc-ping.com/abc", "https://hc-ping.com/abc/fail"]
    assert ping("NOT_SET") is False


# ------------------------------------------------------------------ watchdog
def test_watchdog_exits_on_stall():
    exits, msgs = [], []
    w = Watchdog("t", timeout_s=0.01, on_stall=msgs.append, exit_fn=exits.append)
    import time
    time.sleep(0.02)
    assert w.check_once() and exits == [3] and msgs
    w.tick()
    assert not w.stalled()


# -------------------------------------------------------------- kill switch
def test_parent_switch_blocks_children_and_refresh_sees_other_process(tmp_path):
    p = tmp_path / "ops.sqlite"
    g = KillSwitch(OpsDB(p, "PAPER"))
    child = KillSwitch(OpsDB(p, "PAPER:A2:KRW-BTC"), parent=g)
    assert not child.active
    KillSwitch(OpsDB(p, "PAPER")).trigger("MANUAL", "cli in another process")
    assert not child.active  # stale cache until refresh
    child.refresh()
    assert child.active and not child.own_active and child.status()["parent"]["active"]


def test_trigger_hook_fires_once_per_new_reason(tmp_path):
    got = []
    ks_mod.TRIGGER_HOOKS.append(lambda scope, code, detail: got.append((scope, code)))
    try:
        ks = KillSwitch(OpsDB(tmp_path / "o.sqlite", "PAPER:x:y"))
        for _ in range(5):
            ks.trigger("STALE_MARKET_DATA", "tick")
        ks.clear_transient("STALE_MARKET_DATA")
        ks.trigger("STALE_MARKET_DATA", "again")
        assert got == [("PAPER:x:y", "STALE_MARKET_DATA")] * 2
        assert len([e for e in ks.db.events(50) if e["component"] == "kill_switch" and "TRIGGERED" in e["message"]]) == 2
    finally:
        ks_mod.TRIGGER_HOOKS.pop()


def test_instance_namespaces_are_isolated(tmp_path):
    p = tmp_path / "ops.sqlite"
    a, b = OpsDB(p, "PAPER:A2:KRW-BTC"), OpsDB(p, "PAPER:EMA:KRW-BTC")
    a.set_kv("paper_account", {"cash": 1})
    from crypto_trader.core.models import Position
    a.save_position(Position("KRW-BTC", "A2", volume=1, avg_price=1))
    assert b.get_kv("paper_account") is None and b.load_positions() == {}
    assert OpsDB(p, "PAPER").modes("PAPER:") == ["PAPER:A2:KRW-BTC"]


# --------------------------------------------------------------- compaction
def test_compaction_merges_closed_days_only(tmp_path):
    st = ParquetStore(tmp_path)
    day1, day2 = 0, 86_400_000
    for k in range(5):
        st.write("trades", "upbit", "KRW-BTC", pl.DataFrame({"event_ts": [day1 + k], "seq": [k], "price": [1.0], "volume": [1.0]}))
        st.write("trades", "upbit", "KRW-BTC", pl.DataFrame({"event_ts": [day2 + k], "seq": [k], "price": [1.0], "volume": [1.0]}))
    st.write("trades", "upbit", "KRW-BTC", pl.DataFrame({"event_ts": [day1 + 1], "seq": [1], "price": [1.0], "volume": [1.0]}))
    res = st.compact(before_date="1970-01-02")
    assert res["partitions"] == 1 and res["files_before"] == 6 and res["rows"] == 5
    d1 = st.partition_dir("trades", "upbit", "KRW-BTC", "1970-01-01")
    d2 = st.partition_dir("trades", "upbit", "KRW-BTC", "1970-01-02")
    assert len(list(d1.glob("*.parquet"))) == 1 and len(list(d2.glob("*.parquet"))) == 5
    assert st.read("trades", "upbit", "KRW-BTC").height == 10


# ---------------------------------------------------------- live deriv feed
def _fapi_handler(req: httpx.Request) -> httpx.Response:
    t0 = 1_790_000_000_000  # in the past: the feed drops still-open klines
    p = req.url.path
    if p == "/futures/data/openInterestHist":
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "sumOpenInterest": "10", "sumOpenInterestValue": "1000",
                                          "timestamp": t0 + i * 300_000} for i in range(3)])
    if p == "/futures/data/takerlongshortRatio":
        return httpx.Response(200, json=[{"buySellRatio": "1.1", "buyVol": "1", "sellVol": "1", "timestamp": t0}])
    if p == "/futures/data/globalLongShortAccountRatio":
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "longShortRatio": "2.0", "timestamp": t0}])
    if p == "/fapi/v1/fundingRate":
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": t0}])
    if p in ("/fapi/v1/premiumIndexKlines", "/fapi/v1/klines"):
        row = [t0, "1", "2", "0.5", "1.5", "10", t0 + 3_599_999, "0", 1, "4", "0", "0"]
        return httpx.Response(200, json=[row])
    return httpx.Response(404)


def test_live_feed_maps_rest_into_archive_schema(tmp_path):
    rest = BinanceFuturesRest(HttpClient("https://fapi.binance.com", transport=httpx.MockTransport(_fapi_handler)))
    feed = BinanceLiveDerivatives(ParquetStore(tmp_path), "BTCUSDT", rest=rest)
    assert asyncio.run(feed.refresh()) == "OK"
    m = feed.metrics
    assert m["oi_usd"].to_list() == [1000.0] * 3 and m["available_ts"][0] == m["event_ts"][0] + 300_000
    assert m["taker_buy_sell_ratio"][0] == 1.1 and m["global_ls_ratio"][0] == 2.0
    assert feed.funding["funding_rate"][0] == 0.0001 and feed.fut_klines["taker_buy_volume"][0] == 4.0
    assert feed.premium["premium_close"][0] == 1.5


def test_live_feed_reports_block_without_bypass(tmp_path):
    t = httpx.MockTransport(lambda r: httpx.Response(451, text="restricted location"))
    feed = BinanceLiveDerivatives(ParquetStore(tmp_path), "BTCUSDT",
                                  rest=BinanceFuturesRest(HttpClient("https://fapi.binance.com", transport=t, max_retries=1)))
    assert asyncio.run(feed.refresh()).startswith("BLOCKED") and feed.last_ok_ms is None


# ------------------------------------------------------------- paper report
def test_signal_parity_and_slippage(bars):
    s = build("BASE_donchian")
    f = Features.from_frame(bars)
    s.prepare(f)
    sig_i = [i for i in range(300, bars.height) if s.on_bar(i, f, "KRW-BTC", None)][:3]
    quiet_i = [i for i in range(300, bars.height) if not s.on_bar(i, f, "KRW-BTC", None)][:3]
    evals = [{"action": "EVAL", "accepted": 1, "bar_ts": int(f.ts[i]), "decision_ts": int(f.ts[i]),
              "data": json.dumps({"pos_open": False, "ref": 100.0})} for i in sig_i + quiet_i]
    acts = [{"action": "ENTER", "accepted": 1, "bar_ts": int(f.ts[i])} for i in sig_i]
    par = signal_parity(evals, acts, bars, "BASE_donchian", "KRW-BTC")
    assert par["checked"] == 6 and par["match"] == 6
    par2 = signal_parity(evals, acts[:-1], bars, "BASE_donchian", "KRW-BTC")
    assert par2["match"] == 5 and par2["mismatch"][0]["backtest"] == "ENTER"
    fills = [{"ts": int(f.ts[sig_i[0]]) + 1, "price": 100.2, "side": "bid"}, {"ts": int(f.ts[sig_i[0]]) + 2, "price": 99.9, "side": "ask"}]
    assert fill_slippage_bps(fills, evals) == pytest.approx([20.0, 10.0])
    assert np.isfinite(par["checked"])


def test_dotenv_loader_does_not_override_existing(tmp_path, monkeypatch):
    from crypto_trader.cli import _load_dotenv
    (tmp_path / ".env").write_text('# c\nexport FOO_A="1"\nFOO_B=2\nBAD LINE\n')
    monkeypatch.setenv("FOO_B", "keep")
    monkeypatch.delenv("FOO_A", raising=False)
    _load_dotenv(tmp_path / ".env")
    import os
    assert os.environ["FOO_A"] == "1" and os.environ["FOO_B"] == "keep"
