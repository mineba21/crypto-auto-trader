import hashlib
import json

import httpx
import jwt
import pytest

from crypto_trader.brokers import upbit_live as ul
from crypto_trader.brokers.base import OrderRejected
from crypto_trader.core.models import Mode, OrderRequest, OrderState, OrderType, Side
from crypto_trader.risk.kill_switch import KillSwitch
from crypto_trader.storage.ops_db import OpsDB

SECRET = "approval-secret"


@pytest.fixture
def live_env(settings, tmp_path, monkeypatch):
    settings.mode = Mode.LIVE
    settings.live.enabled = True
    settings.live.approval_file = str(tmp_path / "approval.json")
    monkeypatch.setenv(ul.LIVE_ENV_FLAG, ul.LIVE_ENV_VALUE)
    monkeypatch.setattr(ul.time, "sleep", lambda s: None)
    ul.write_approval(tmp_path / "approval.json", SECRET, "tester", 300_000, ["A2_sweep_choch"], ["KRW-BTC"], 7)
    return settings


class FakeUpbit:
    """httpx MockTransport handler emulating the subset of Upbit private API we use."""

    def __init__(self):
        self.orders = {}
        self.posts = 0
        self.fail_next_post = None  # "timeout" | 500
        self.accounts = [{"currency": "KRW", "balance": "300000", "locked": "0", "avg_buy_price": "0"}]
        self.lookup_404 = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        tok = req.headers["Authorization"].split()[1]
        payload = jwt.decode(tok, "sk"*40, algorithms=["HS512"])
        if req.method == "POST":
            body = json.loads(req.content)
            q = "&".join(f"{k}={v}" for k, v in body.items())
            assert payload["query_hash"] == hashlib.sha512(q.encode()).hexdigest()
            if req.url.path == "/v1/orders/test":
                return httpx.Response(201, json={"uuid": "test"})
            self.posts += 1
            o = {"uuid": f"u{self.posts}", "identifier": body["identifier"], "state": "done", "ord_type": body["ord_type"],
                 "executed_volume": "0.001", "paid_fee": "50", "trades": [
                     {"uuid": f"t{self.posts}", "price": "100000000", "volume": "0.001", "created_at": "2026-01-01T00:00:00+09:00"}]}
            self.orders[body["identifier"]] = o
            if self.fail_next_post == "timeout":
                self.fail_next_post = None
                raise httpx.ReadTimeout("timeout", request=req)
            if self.fail_next_post == 500:
                self.fail_next_post = None
                self.orders.pop(body["identifier"])
                return httpx.Response(500, text="oops")
            return httpx.Response(201, json=o)
        if req.url.path == "/v1/order":
            ident = req.url.params.get("identifier")
            if self.lookup_404 or ident not in self.orders:
                return httpx.Response(404, json={"error": {"name": "order_not_found"}})
            return httpx.Response(200, json=self.orders[ident])
        if req.url.path == "/v1/accounts":
            return httpx.Response(200, json=self.accounts)
        return httpx.Response(404)


def broker(settings, fake, tmp_path, secret=SECRET):
    db = OpsDB(tmp_path / "live.sqlite", Mode.LIVE)
    ks = KillSwitch(db)
    client = ul.UpbitPrivateClient("ak", "sk"*40, transport=httpx.MockTransport(fake))
    return ul.UpbitLiveBroker(client, db, ul.LiveGuard(settings, secret), ks, ref_price=lambda s: 100_000_000), db, ks


def req(krw=10_000, strategy="A2_sweep_choch", symbol="KRW-BTC"):
    return OrderRequest(symbol, Side.BUY, OrderType.MARKET_BUY, strategy, price=krw)


def test_live_refused_by_default(settings):
    with pytest.raises(PermissionError) as e:
        ul.LiveGuard(settings, SECRET).verify()
    assert "mode is not LIVE" in str(e.value) and "live.enabled is false" in str(e.value)


def test_live_refused_with_bad_signature_or_missing_env(live_env, monkeypatch):
    with pytest.raises(PermissionError, match="signature"):
        ul.LiveGuard(live_env, "wrong").verify()
    monkeypatch.delenv(ul.LIVE_ENV_FLAG)
    with pytest.raises(PermissionError, match=ul.LIVE_ENV_FLAG):
        ul.LiveGuard(live_env, SECRET).verify()


def test_requires_live_mode_db(live_env, tmp_path):
    with pytest.raises(ValueError):
        ul.UpbitLiveBroker(ul.UpbitPrivateClient("a", "b"), OpsDB(tmp_path / "p.sqlite", Mode.PAPER),
                           ul.LiveGuard(live_env, SECRET), KillSwitch(OpsDB(":memory:", "LIVE")))


def test_submit_success_persists_and_records_fill(live_env, tmp_path):
    fake = FakeUpbit()
    b, db, _ = broker(live_env, fake, tmp_path)
    o = b.submit(req())
    assert o.state == OrderState.FILLED and o.exchange_order_id == "u1" and fake.posts == 1
    assert db.order_exists(o.client_order_id) and len(db.fills()) == 1
    assert db.fills()[0]["fee"] == pytest.approx(50)


def test_guard_limits_market_strategy_amount(live_env, tmp_path):
    b, _, _ = broker(live_env, FakeUpbit(), tmp_path)
    with pytest.raises(PermissionError, match="market"):
        b.submit(req(symbol="KRW-ETH"))
    with pytest.raises(PermissionError, match="strategy"):
        b.submit(req(strategy="A1_sweep_reversal"))
    with pytest.raises(PermissionError, match="max_krw"):
        b.submit(req(krw=400_000))


def test_duplicate_order_id_rejected(live_env, tmp_path):
    b, _, _ = broker(live_env, FakeUpbit(), tmp_path)
    r = req()
    b.submit(r)
    with pytest.raises(OrderRejected):
        b.submit(r)


def test_timeout_is_resolved_by_lookup_not_resend(live_env, tmp_path):
    fake = FakeUpbit()
    fake.fail_next_post = "timeout"
    b, _, ks = broker(live_env, fake, tmp_path)
    o = b.submit(req())
    assert fake.posts == 1  # never re-sent
    assert o.state == OrderState.FILLED and not ks.active


def test_unresolved_unknown_blocks_new_orders_and_trips_kill_switch(live_env, tmp_path):
    fake = FakeUpbit()
    fake.fail_next_post = 500
    b, _, ks = broker(live_env, fake, tmp_path)
    o = b.submit(req())
    assert o.state == OrderState.UNKNOWN and ks.active
    with pytest.raises(OrderRejected):
        b.submit(req())
    assert fake.posts == 1


def test_kill_switch_blocks_buys_but_allows_protective_sell(live_env, tmp_path):
    fake = FakeUpbit()
    b, _, ks = broker(live_env, fake, tmp_path)
    ks.trigger("MANUAL", "test")
    with pytest.raises(OrderRejected):
        b.submit(req())
    sell = OrderRequest("KRW-BTC", Side.SELL, OrderType.MARKET_SELL, "A2_sweep_choch", volume=0.001)
    assert b.submit(sell).state == OrderState.FILLED


def test_reconcile_balance_mismatch_trips_kill_switch(live_env, tmp_path):
    fake = FakeUpbit()
    b, _, ks = broker(live_env, fake, tmp_path)
    issues = b.reconcile({"KRW-BTC": 0.5})
    assert issues and ks.active and ks.status()["reasons"][0]["code"] == "BALANCE_MISMATCH"


def test_restart_reloads_active_orders(live_env, tmp_path):
    fake = FakeUpbit()
    fake.fail_next_post = 500
    fake.lookup_404 = True
    b, db, ks = broker(live_env, fake, tmp_path)
    o = b.submit(req())
    assert o.state == OrderState.UNKNOWN
    ks.reset("t")
    b2, _, _ = broker(live_env, fake, tmp_path)  # process restart
    assert o.client_order_id in b2.orders and b2.has_unknown()


def test_jwt_get_query_hash():
    seen = {}

    def h(req):
        seen["p"] = jwt.decode(req.headers["Authorization"].split()[1], "sk"*40, algorithms=["HS512"])
        return httpx.Response(200, json={})

    c = ul.UpbitPrivateClient("ak", "sk"*40, transport=httpx.MockTransport(h))
    c.request("GET", "/v1/order", params={"identifier": "abc"})
    assert seen["p"]["query_hash"] == hashlib.sha512(b"identifier=abc").hexdigest()
    assert seen["p"]["access_key"] == "ak" and seen["p"]["query_hash_alg"] == "SHA512"
