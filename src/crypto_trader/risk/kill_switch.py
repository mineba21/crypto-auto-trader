"""Persistent kill switch. State lives in the ops DB so it survives restarts; only an explicit operator reset clears it.

A switch can have a ``parent`` (e.g. a global PAPER switch shared by every strategy instance): the child is active
whenever the parent is. ``refresh()`` re-reads the DB so a switch flipped by another process (the CLI) takes effect.
"""

from __future__ import annotations

from collections.abc import Callable

from crypto_trader.core.timeutils import now_ms
from crypto_trader.storage.ops_db import OpsDB

KEY = "kill_switch"

# Reasons that must never auto-clear (loss limits, manual).
STICKY = {"CUM_LOSS", "TOTAL_LOSS", "MANUAL", "ORDER_STATE_MISMATCH", "BALANCE_MISMATCH", "ABNORMAL_ORDERS",
          "DB_WRITE_FAILURE", "UNKNOWN_ORDER_STATE"}

# Called as hook(scope, code, detail) when a NEW reason is added (used for operator notifications).
TRIGGER_HOOKS: list[Callable[[str, str, str], None]] = []


class KillSwitch:
    def __init__(self, db: OpsDB, parent: KillSwitch | None = None):
        self.db = db
        self.parent = parent
        self._cache = db.get_kv(KEY, {"active": False, "reasons": []})

    @property
    def own_active(self) -> bool:
        return bool(self._cache.get("active"))

    @property
    def active(self) -> bool:
        return self.own_active or (self.parent is not None and self.parent.active)

    def status(self) -> dict:
        st = dict(self._cache)
        if self.parent is not None and self.parent.active:
            st["parent"] = self.parent.status()
            st["active"] = True
        return st

    def refresh(self) -> None:
        """Pick up changes made by another process (e.g. `cat kill-switch on`)."""
        try:
            self._cache = self.db.get_kv(KEY, {"active": False, "reasons": []})
        except Exception:  # noqa: BLE001 - keep the last known (possibly active) state if the DB is unreadable
            pass
        if self.parent is not None:
            self.parent.refresh()

    def trigger(self, code: str, detail: str) -> None:
        reasons = self._cache.get("reasons", [])
        is_new = not any(r["code"] == code for r in reasons)
        if is_new:
            reasons.append({"code": code, "detail": detail, "ts": now_ms()})
        was_active = self.own_active
        self._cache = {"active": True, "reasons": reasons, "since": self._cache.get("since") or now_ms()}
        if not is_new and was_active:
            return  # already recorded; avoid rewriting / re-logging every health tick
        try:
            self.db.set_kv(KEY, self._cache)
            self.db.log_event("CRITICAL", "kill_switch", f"TRIGGERED {code}: {detail}")
        except Exception:  # noqa: BLE001
            # even if the DB is failing, in-memory state stays active for this process
            pass
        for hook in TRIGGER_HOOKS:
            try:
                hook(self.db.mode, code, detail)
            except Exception:  # noqa: BLE001
                pass

    def clear_transient(self, code: str) -> None:
        """Auto-clear a non-sticky reason (e.g. stale data recovered). Sticky reasons stay."""
        if code in STICKY or not any(r["code"] == code for r in self._cache.get("reasons", [])):
            return
        reasons = [r for r in self._cache.get("reasons", []) if r["code"] != code]
        self._cache = {"active": bool(reasons), "reasons": reasons, "since": self._cache.get("since") if reasons else None}
        self.db.set_kv(KEY, self._cache)
        self.db.log_event("INFO", "kill_switch", f"cleared transient {code}")

    def reset(self, operator: str, note: str = "") -> None:
        self.db.log_event("WARNING", "kill_switch", f"RESET by {operator}", {"previous": self._cache, "note": note})
        self._cache = {"active": False, "reasons": []}
        self.db.set_kv(KEY, self._cache)
