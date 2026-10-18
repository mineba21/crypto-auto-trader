"""Operational state store (SQLite in WAL mode).

Orders, fills, positions, risk state, kill switch and the audit log live here, not in Parquet: they need
transactional updates and must survive restarts. Every mode (BACKTEST/PAPER/LIVE) uses its own ``mode`` column
and LIVE should use its own database file, so paper activity can never be mistaken for live state.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from crypto_trader.core.models import Fill, Order, OrderRequest, OrderState, OrderType, Position, Side
from crypto_trader.core.timeutils import now_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    price REAL,
    volume REAL,
    state TEXT NOT NULL,
    exchange_order_id TEXT,
    filled_volume REAL DEFAULT 0,
    avg_price REAL DEFAULT 0,
    paid_fee REAL DEFAULT 0,
    reject_reason TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    stop_price REAL,
    take_profit REAL,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_state ON orders(mode, state);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    trade_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    ts INTEGER NOT NULL,
    price REAL NOT NULL,
    volume REAL NOT NULL,
    fee REAL NOT NULL,
    slippage_krw REAL DEFAULT 0,
    UNIQUE(mode, client_order_id, trade_id)
);
CREATE TABLE IF NOT EXISTS positions (
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    volume REAL NOT NULL,
    avg_price REAL NOT NULL,
    entry_ts INTEGER,
    stop_price REAL,
    take_profit REAL,
    entry_fee REAL DEFAULT 0,
    initial_risk_krw REAL DEFAULT 0,
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (mode, symbol)
);
CREATE TABLE IF NOT EXISTS kv (
    mode TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (mode, key)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    decision_ts INTEGER NOT NULL,
    bar_ts INTEGER,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    action TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    reason TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    mode TEXT NOT NULL,
    ts INTEGER NOT NULL,
    cash_krw REAL NOT NULL,
    equity_krw REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    PRIMARY KEY (mode, ts)
);
CREATE TABLE IF NOT EXISTS heartbeats (
    component TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    status TEXT NOT NULL,
    detail TEXT
);
"""


class OpsDB:
    def __init__(self, path: str | Path, mode: str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mode = str(mode)
        self._lock = threading.RLock()
        self.con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=FULL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript(SCHEMA)

    def close(self) -> None:
        self.con.close()

    def _exec(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.con.execute(sql, params)

    def tx(self):
        """Context manager for an explicit transaction."""
        db = self

        class _Tx:
            def __enter__(self_inner):
                db._lock.acquire()
                db.con.execute("BEGIN IMMEDIATE")
                return db

            def __exit__(self_inner, et, ev, tb):
                try:
                    db.con.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    db._lock.release()
                return False

        return _Tx()

    # ------------------------------------------------------------- key/value
    def set_kv(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv(mode,key,value,updated_ts) VALUES(?,?,?,?) "
            "ON CONFLICT(mode,key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
            (self.mode, key, json.dumps(value), now_ms()),
        )

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self._exec("SELECT value FROM kv WHERE mode=? AND key=?", (self.mode, key)).fetchone()
        return json.loads(row["value"]) if row else default

    # ---------------------------------------------------------------- orders
    def upsert_order(self, o: Order) -> None:
        r = o.request
        self._exec(
            """INSERT INTO orders(client_order_id,mode,strategy,symbol,side,order_type,price,volume,state,
                   exchange_order_id,filled_volume,avg_price,paid_fee,reject_reason,reason,stop_price,take_profit,
                   created_ts,updated_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(client_order_id) DO UPDATE SET state=excluded.state,
                   exchange_order_id=COALESCE(excluded.exchange_order_id, orders.exchange_order_id),
                   filled_volume=excluded.filled_volume, avg_price=excluded.avg_price, paid_fee=excluded.paid_fee,
                   reject_reason=excluded.reject_reason, updated_ts=excluded.updated_ts""",
            (r.client_order_id, self.mode, r.strategy, r.symbol, str(r.side), str(r.order_type), r.price, r.volume,
             str(o.state), o.exchange_order_id, o.filled_volume, o.avg_price, o.paid_fee, o.reject_reason, r.reason,
             r.stop_price, r.take_profit, r.created_ts or now_ms(), o.updated_ts or now_ms()),
        )

    def order_exists(self, client_order_id: str) -> bool:
        return self._exec("SELECT 1 FROM orders WHERE client_order_id=?", (client_order_id,)).fetchone() is not None

    def load_orders(self, active_only: bool = False) -> list[Order]:
        sql = "SELECT * FROM orders WHERE mode=?"
        if active_only:
            sql += " AND state NOT IN ('done','cancel','rejected')"
        out = []
        for row in self._exec(sql + " ORDER BY created_ts", (self.mode,)).fetchall():
            req = OrderRequest(
                symbol=row["symbol"], side=Side(row["side"]), order_type=OrderType(row["order_type"]),
                strategy=row["strategy"], volume=row["volume"], price=row["price"],
                client_order_id=row["client_order_id"], reason=row["reason"], stop_price=row["stop_price"],
                take_profit=row["take_profit"], created_ts=row["created_ts"],
            )
            out.append(Order(request=req, state=OrderState(row["state"]), exchange_order_id=row["exchange_order_id"],
                             filled_volume=row["filled_volume"], avg_price=row["avg_price"], paid_fee=row["paid_fee"],
                             reject_reason=row["reject_reason"], updated_ts=row["updated_ts"]))
        return out

    def add_fill(self, f: Fill) -> bool:
        cur = self._exec(
            "INSERT OR IGNORE INTO fills(mode,client_order_id,trade_id,symbol,side,ts,price,volume,fee,slippage_krw) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.mode, f.client_order_id, f.trade_id or f"{f.ts}-{f.volume}", f.symbol, str(f.side), f.ts, f.price,
             f.volume, f.fee, f.slippage_krw),
        )
        return cur.rowcount > 0

    def fills(self, limit: int = 200) -> list[dict]:
        rows = self._exec("SELECT * FROM fills WHERE mode=? ORDER BY ts DESC LIMIT ?", (self.mode, limit)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- positions
    def save_position(self, p: Position) -> None:
        if not p.is_open:
            self._exec("DELETE FROM positions WHERE mode=? AND symbol=?", (self.mode, p.symbol))
            return
        self._exec(
            """INSERT INTO positions(mode,symbol,strategy,volume,avg_price,entry_ts,stop_price,take_profit,entry_fee,
                   initial_risk_krw,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(mode,symbol) DO UPDATE SET strategy=excluded.strategy, volume=excluded.volume,
                   avg_price=excluded.avg_price, entry_ts=excluded.entry_ts, stop_price=excluded.stop_price,
                   take_profit=excluded.take_profit, entry_fee=excluded.entry_fee,
                   initial_risk_krw=excluded.initial_risk_krw, updated_ts=excluded.updated_ts""",
            (self.mode, p.symbol, p.strategy, p.volume, p.avg_price, p.entry_ts, p.stop_price, p.take_profit,
             p.entry_fee, p.initial_risk_krw, now_ms()),
        )

    def load_positions(self) -> dict[str, Position]:
        out = {}
        for r in self._exec("SELECT * FROM positions WHERE mode=?", (self.mode,)).fetchall():
            out[r["symbol"]] = Position(symbol=r["symbol"], strategy=r["strategy"], volume=r["volume"],
                                        avg_price=r["avg_price"], entry_ts=r["entry_ts"] or 0,
                                        stop_price=r["stop_price"], take_profit=r["take_profit"],
                                        entry_fee=r["entry_fee"], initial_risk_krw=r["initial_risk_krw"])
        return out

    # ------------------------------------------------------- events & signals
    def log_event(self, level: str, component: str, message: str, data: dict | None = None) -> None:
        self._exec("INSERT INTO events(mode,ts,level,component,message,data) VALUES(?,?,?,?,?,?)",
                   (self.mode, now_ms(), level, component, message, json.dumps(data, default=str) if data else None))

    def events(self, limit: int = 100, level: str | None = None) -> list[dict]:
        sql, p = "SELECT * FROM events WHERE mode=?", [self.mode]
        if level:
            sql += " AND level=?"
            p.append(level)
        return [dict(r) for r in self._exec(sql + " ORDER BY id DESC LIMIT ?", (*p, limit)).fetchall()]

    def log_signal(self, decision_ts: int, bar_ts: int | None, symbol: str, strategy: str, action: str,
                   accepted: bool, reason: str, data: dict | None = None) -> None:
        self._exec(
            "INSERT INTO signals(mode,decision_ts,bar_ts,symbol,strategy,action,accepted,reason,data) VALUES(?,?,?,?,?,?,?,?,?)",
            (self.mode, decision_ts, bar_ts, symbol, strategy, action, int(accepted), reason,
             json.dumps(data, default=str) if data else None),
        )

    def signals(self, limit: int = 100) -> list[dict]:
        rows = self._exec("SELECT * FROM signals WHERE mode=? ORDER BY id DESC LIMIT ?", (self.mode, limit)).fetchall()
        return [dict(r) for r in rows]

    def record_equity(self, ts: int, cash: float, equity: float, realized: float, unrealized: float) -> None:
        self._exec("INSERT OR REPLACE INTO equity(mode,ts,cash_krw,equity_krw,realized_pnl,unrealized_pnl) VALUES(?,?,?,?,?,?)",
                   (self.mode, ts, cash, equity, realized, unrealized))

    def equity_curve(self, limit: int = 5000) -> list[dict]:
        rows = self._exec("SELECT * FROM equity WHERE mode=? ORDER BY ts DESC LIMIT ?", (self.mode, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def heartbeat(self, component: str, status: str, detail: str = "") -> None:
        self._exec("INSERT OR REPLACE INTO heartbeats(component,ts,status,detail) VALUES(?,?,?,?)",
                   (component, now_ms(), status, detail))

    def modes(self, prefix: str = "") -> list[str]:
        """All mode namespaces present in this DB (e.g. PAPER:A2_sweep_choch:KRW-BTC)."""
        rows = self._exec("SELECT DISTINCT mode FROM kv UNION SELECT DISTINCT mode FROM signals ORDER BY 1").fetchall()
        return [r[0] for r in rows if r[0].startswith(prefix)]

    def signals_between(self, start_ms: int, end_ms: int) -> list[dict]:
        rows = self._exec("SELECT * FROM signals WHERE mode=? AND decision_ts>=? AND decision_ts<? ORDER BY id",
                          (self.mode, start_ms, end_ms)).fetchall()
        return [dict(r) for r in rows]

    def fills_between(self, start_ms: int, end_ms: int) -> list[dict]:
        rows = self._exec("SELECT * FROM fills WHERE mode=? AND ts>=? AND ts<? ORDER BY ts", (self.mode, start_ms, end_ms)).fetchall()
        return [dict(r) for r in rows]

    def equity_between(self, start_ms: int, end_ms: int) -> list[dict]:
        rows = self._exec("SELECT * FROM equity WHERE mode=? AND ts>=? AND ts<? ORDER BY ts", (self.mode, start_ms, end_ms)).fetchall()
        return [dict(r) for r in rows]

    def heartbeats(self) -> list[dict]:
        return [dict(r) for r in self._exec("SELECT * FROM heartbeats ORDER BY component").fetchall()]
