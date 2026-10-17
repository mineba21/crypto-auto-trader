"""UpbitLiveBroker: real Upbit KRW spot orders. DISABLED unless every gate in ``LiveGuard`` passes.

Order safety:
* Every order gets a never-reused ``identifier`` (= client_order_id) and is persisted BEFORE it is sent.
* ``POST /v1/orders`` is sent exactly once (no automatic retry). A timeout / 5xx / transport error marks the order
  UNKNOWN; we then look it up by identifier. While any order is UNKNOWN, no new orders are allowed.
* On startup ``reconcile()`` re-reads every non-terminal order and the account balances; any disagreement with the
  internal ledger trips the kill switch instead of being "fixed" silently.
* Upbit offers no server-side stop-loss order type; stops are monitored client-side by the execution engine. If this
  process dies, stops are NOT enforced. This is an accepted, documented risk (docs/LIVE_RUNBOOK.md).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlencode

import httpx
import jwt

from crypto_trader.brokers.base import Balance, Broker, OrderRejected
from crypto_trader.core.config import Settings
from crypto_trader.core.models import Fill, Mode, Order, OrderRequest, OrderState, OrderType, Side
from crypto_trader.core.timeutils import now_ms, parse_utc
from crypto_trader.risk.kill_switch import KillSwitch
from crypto_trader.storage.ops_db import OpsDB

log = logging.getLogger(__name__)
BASE_URL = "https://api.upbit.com"
LIVE_ENV_FLAG = "CAT_ENABLE_LIVE"
LIVE_ENV_VALUE = "I_ACCEPT_REAL_MONEY_RISK"


# ------------------------------------------------------------------ approval
@dataclass
class LiveApproval:
    approved_by: str
    approved_at: str
    expires_at: str
    max_krw: float
    strategies: list[str]
    markets: list[str]
    signature: str

    def payload(self) -> bytes:
        d = {k: getattr(self, k) for k in ("approved_by", "approved_at", "expires_at", "max_krw", "strategies", "markets")}
        return json.dumps(d, sort_keys=True).encode()

    @staticmethod
    def sign(payload: bytes, secret: str) -> str:
        return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    @classmethod
    def load(cls, path: Path) -> LiveApproval:
        return cls(**json.loads(path.read_text()))


class LiveGuard:
    """All of these must hold before a single live order is sent."""

    def __init__(self, settings: Settings, approval_secret: str | None = None):
        self.s = settings
        self.secret = approval_secret if approval_secret is not None else os.environ.get("CAT_LIVE_APPROVAL_SECRET", "")
        self.approval: LiveApproval | None = None

    def verify(self) -> None:
        problems = []
        if self.s.mode != Mode.LIVE:
            problems.append("settings.mode is not LIVE")
        if not self.s.live.enabled:
            problems.append("settings.live.enabled is false")
        if os.environ.get(LIVE_ENV_FLAG) != LIVE_ENV_VALUE:
            problems.append(f"env {LIVE_ENV_FLAG} not set to the acknowledgement value")
        if not self.secret:
            problems.append("CAT_LIVE_APPROVAL_SECRET missing")
        path = self.s.path(self.s.live.approval_file)
        if not path.exists():
            problems.append(f"approval file {path} missing")
        else:
            a = LiveApproval.load(path)
            if self.secret and not hmac.compare_digest(a.signature, LiveApproval.sign(a.payload(), self.secret)):
                problems.append("approval signature invalid")
            if parse_utc(a.expires_at) < now_ms():
                problems.append("approval expired")
            if a.max_krw > self.s.capital.max_trading_capital_krw:
                problems.append("approval max_krw exceeds max trading capital")
            self.approval = a
        if problems:
            raise PermissionError("LIVE trading not permitted: " + "; ".join(problems))

    def check_order(self, req: OrderRequest, exposure_krw: float, ref_price: float) -> None:
        a = self.approval
        if a is None:
            raise PermissionError("guard not verified")
        if parse_utc(a.expires_at) < now_ms():
            raise PermissionError("approval expired")
        if req.symbol not in a.markets:
            raise PermissionError(f"market {req.symbol} not approved")
        if req.strategy.split("(")[0] not in a.strategies:
            raise PermissionError(f"strategy {req.strategy} not approved")
        if req.side == Side.BUY and exposure_krw + req.notional_krw(ref_price) > a.max_krw:
            raise PermissionError("order would exceed approved max_krw")


# ------------------------------------------------------------------ client
class UpbitPrivateClient:
    def __init__(self, access_key: str, secret_key: str, transport: httpx.BaseTransport | None = None, timeout: float = 5.0):
        if not access_key or not secret_key:
            raise ValueError("Upbit API keys are required for the private client")
        self.ak, self.sk = access_key, secret_key
        self.http = httpx.Client(base_url=BASE_URL, timeout=timeout, transport=transport)

    def _token(self, params: dict | None) -> str:
        payload = {"access_key": self.ak, "nonce": str(uuid.uuid4())}
        if params:
            q = unquote(urlencode(params, doseq=True))
            payload["query_hash"] = hashlib.sha512(q.encode()).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        return jwt.encode(payload, self.sk, algorithm="HS512")

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> httpx.Response:
        token = self._token(body if body is not None else params)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        return self.http.request(method, path, params=params, json=body, headers=headers)

    def close(self) -> None:
        self.http.close()


def _state_from_upbit(o: dict) -> OrderState:
    st = o.get("state")
    executed = float(o.get("executed_volume") or 0)
    if st == "wait":
        return OrderState.PARTIAL if executed > 0 else OrderState.OPEN
    if st == "watch":
        return OrderState.WATCH
    if st == "done":
        return OrderState.FILLED
    if st == "cancel":
        # Upbit closes a market buy as "cancel" when leftover KRW is returned after execution.
        if executed > 0 and o.get("ord_type") in ("price", "market"):
            return OrderState.FILLED
        return OrderState.CANCELLED
    return OrderState.UNKNOWN


class UpbitLiveBroker(Broker):
    mode = Mode.LIVE

    def __init__(self, client: UpbitPrivateClient, db: OpsDB, guard: LiveGuard, kill_switch: KillSwitch,
                 use_order_test: bool = True, ref_price=lambda symbol: 0.0):
        if db.mode != Mode.LIVE:
            raise ValueError("UpbitLiveBroker requires an OpsDB opened in LIVE mode")
        self.c, self.db, self.guard, self.ks = client, db, guard, kill_switch
        self.use_order_test = use_order_test
        self.ref_price = ref_price
        self.orders: dict[str, Order] = {o.client_order_id: o for o in db.load_orders(active_only=True)}
        self._fills: list[Fill] = []
        self.guard.verify()

    # --------------------------------------------------------------- helpers
    @staticmethod
    def order_body(req: OrderRequest) -> dict:
        b: dict = {"market": req.symbol, "side": str(req.side), "ord_type": str(req.order_type), "identifier": req.client_order_id}
        if req.order_type == OrderType.LIMIT:
            b["price"], b["volume"] = _fmt(req.price), _fmt(req.volume)
        elif req.order_type == OrderType.MARKET_BUY:
            b["price"] = _fmt(req.price)
        elif req.order_type == OrderType.MARKET_SELL:
            b["volume"] = _fmt(req.volume)
        else:
            raise OrderRejected("best orders not enabled")
        return b

    def has_unknown(self) -> bool:
        return any(o.state in (OrderState.UNKNOWN, OrderState.SUBMITTING) for o in self.orders.values())

    def _exposure(self) -> float:
        return sum(o.request.notional_krw(self.ref_price(o.request.symbol)) for o in self.orders.values()
                   if o.state.is_active and o.request.side == Side.BUY)

    # ------------------------------------------------------------------ API
    def submit(self, req: OrderRequest) -> Order:
        if self.ks.active and req.side == Side.BUY:
            raise OrderRejected("kill switch active: new buys blocked (protective sells still allowed)")
        if self.has_unknown():
            raise OrderRejected("an order is in UNKNOWN state; reconcile first")
        if self.db.order_exists(req.client_order_id):
            raise OrderRejected("duplicate client_order_id")
        self.guard.check_order(req, self._exposure(), self.ref_price(req.symbol))
        body = self.order_body(req)
        req.created_ts = req.created_ts or now_ms()
        o = Order(request=req, state=OrderState.CREATED, updated_ts=now_ms())
        self.db.upsert_order(o)  # persist before sending (crash-safe)
        if self.use_order_test:
            r = self.c.request("POST", "/v1/orders/test", body={**body, "identifier": f"t-{req.client_order_id}"[:64]})
            if r.status_code >= 400:
                o.state, o.reject_reason = OrderState.REJECTED, f"pre-check: {r.text[:200]}"
                self.db.upsert_order(o)
                return o
        o.state = OrderState.SUBMITTING
        self.db.upsert_order(o)
        self.orders[req.client_order_id] = o
        try:
            r = self.c.request("POST", "/v1/orders", body=body)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            o.state, o.reject_reason = OrderState.UNKNOWN, f"send outcome unknown: {type(e).__name__}"
            self.db.upsert_order(o)
            self.db.log_event("ERROR", "upbit_live", "order send outcome unknown", {"coid": req.client_order_id})
            self._resolve_unknown(o)
            return o
        if r.status_code in (200, 201):
            self._apply_remote(o, r.json())
        elif 400 <= r.status_code < 500 and r.status_code != 429:
            o.state, o.reject_reason = OrderState.REJECTED, r.text[:300]
        else:  # 429 / 5xx: the exchange may or may not have accepted it
            o.state, o.reject_reason = OrderState.UNKNOWN, f"HTTP {r.status_code}"
            self.db.upsert_order(o)
            self._resolve_unknown(o)
            return o
        self.db.upsert_order(o)
        return o

    def _resolve_unknown(self, o: Order, attempts: int = 3, wait_s: float = 1.0) -> None:
        for _ in range(attempts):
            time.sleep(wait_s)
            try:
                r = self.c.request("GET", "/v1/order", params={"identifier": o.client_order_id})
            except (httpx.TimeoutException, httpx.TransportError):
                continue
            if r.status_code == 200:
                self._apply_remote(o, r.json())
                self.db.upsert_order(o)
                return
            if r.status_code == 404:
                continue  # may not be visible yet
        # still unclear: keep UNKNOWN, block new orders
        self.ks.trigger("UNKNOWN_ORDER_STATE", f"order {o.client_order_id} unresolved after send")

    def _apply_remote(self, o: Order, data: dict) -> None:
        o.exchange_order_id = data.get("uuid", o.exchange_order_id)
        o.state = _state_from_upbit(data)
        o.paid_fee = float(data.get("paid_fee") or 0)
        o.updated_ts = now_ms()
        executed_total = float(data.get("executed_volume") or 0)
        for t in data.get("trades", []) or []:
            vol = float(t["volume"])
            fee = o.paid_fee * vol / executed_total if executed_total > 0 else 0.0  # Upbit reports fee per order
            ts = parse_utc(t["created_at"]) if t.get("created_at") else now_ms()
            f = Fill(o.client_order_id, o.request.symbol, o.request.side, ts, float(t["price"]), vol, fee,
                     trade_id=t.get("uuid", ""))
            if self.db.add_fill(f):
                o.apply_fill(f)
                self._fills.append(f)
        executed = float(data.get("executed_volume") or 0)
        if executed and not o.fills:
            o.filled_volume = executed

    def cancel(self, client_order_id: str) -> Order:
        o = self.orders.get(client_order_id)
        if o is None:
            raise KeyError(client_order_id)
        try:
            r = self.c.request("DELETE", "/v1/order", params={"identifier": client_order_id})
        except (httpx.TimeoutException, httpx.TransportError) as e:
            self.db.log_event("ERROR", "upbit_live", f"cancel failed: {e}", {"coid": client_order_id})
            return o  # state unchanged; next refresh will tell
        if r.status_code == 200:
            self._apply_remote(o, r.json())
        else:
            self.db.log_event("ERROR", "upbit_live", f"cancel HTTP {r.status_code}", {"body": r.text[:200]})
        refreshed = self.get_order(client_order_id)
        return refreshed or o

    def get_order(self, client_order_id: str) -> Order | None:
        o = self.orders.get(client_order_id)
        if o is None:
            return None
        r = self.c.request("GET", "/v1/order", params={"identifier": client_order_id})
        if r.status_code == 200:
            self._apply_remote(o, r.json())
            self.db.upsert_order(o)
        return o

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.state.is_active]

    def balances(self) -> dict[str, Balance]:
        r = self.c.request("GET", "/v1/accounts")
        r.raise_for_status()
        return {a["currency"]: Balance(a["currency"], float(a["balance"]), float(a["locked"]), float(a["avg_buy_price"] or 0))
                for a in r.json()}

    def drain_fills(self) -> list[Fill]:
        f, self._fills = self._fills, []
        return f

    # ------------------------------------------------------------- recovery
    def reconcile(self, internal_positions: dict[str, float], tolerance: float = 1e-8) -> list[str]:
        """Refresh every non-terminal order, then compare exchange balances with the internal ledger."""
        issues = []
        for o in list(self.orders.values()):
            if o.state.is_active:
                self.get_order(o.client_order_id)
                if o.state in (OrderState.UNKNOWN, OrderState.SUBMITTING, OrderState.CREATED):
                    r = self.c.request("GET", "/v1/order", params={"identifier": o.client_order_id})
                    if r.status_code == 404 and o.state == OrderState.CREATED:
                        o.state, o.reject_reason = OrderState.REJECTED, "never reached exchange"
                        self.db.upsert_order(o)
                    elif r.status_code != 200:
                        issues.append(f"order {o.client_order_id} unresolved")
        bal = self.balances()
        for symbol, vol in internal_positions.items():
            cur = symbol.split("-", 1)[1]
            ex = bal.get(cur, Balance(cur, 0.0)).total
            if abs(ex - vol) > max(tolerance, vol * 1e-6):
                issues.append(f"balance mismatch {cur}: exchange={ex} internal={vol}")
        if issues:
            self.ks.trigger("BALANCE_MISMATCH" if any("balance" in i for i in issues) else "ORDER_STATE_MISMATCH",
                            "; ".join(issues)[:500])
        self.db.log_event("INFO" if not issues else "CRITICAL", "upbit_live", "reconcile", {"issues": issues})
        return issues


def _fmt(x: float | None) -> str:
    if x is None:
        raise OrderRejected("missing numeric field")
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return s or "0"


def write_approval(path: Path, secret: str, approved_by: str, max_krw: float, strategies: list[str], markets: list[str],
                   days: int) -> LiveApproval:
    now = datetime.now(UTC)
    a = LiveApproval(approved_by, now.isoformat(), datetime.fromtimestamp(now.timestamp() + days * 86400, UTC).isoformat(),
                     max_krw, strategies, markets, "")
    a.signature = LiveApproval.sign(a.payload(), secret)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(a.__dict__, indent=2))
    return a
