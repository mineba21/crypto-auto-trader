"""Persistent kill switch. State lives in the ops DB so it survives restarts; only an explicit operator reset clears it."""

from __future__ import annotations

from crypto_trader.core.timeutils import now_ms
from crypto_trader.storage.ops_db import OpsDB

KEY = "kill_switch"

# Reasons that must never auto-clear (loss limits, manual).
STICKY = {"CUM_LOSS", "TOTAL_LOSS", "MANUAL", "ORDER_STATE_MISMATCH", "BALANCE_MISMATCH", "ABNORMAL_ORDERS",
          "DB_WRITE_FAILURE", "UNKNOWN_ORDER_STATE"}


class KillSwitch:
    def __init__(self, db: OpsDB):
        self.db = db
        self._cache = db.get_kv(KEY, {"active": False, "reasons": []})

    @property
    def active(self) -> bool:
        return bool(self._cache.get("active"))

    def status(self) -> dict:
        return dict(self._cache)

    def trigger(self, code: str, detail: str) -> None:
        reasons = self._cache.get("reasons", [])
        if not any(r["code"] == code for r in reasons):
            reasons.append({"code": code, "detail": detail, "ts": now_ms()})
        self._cache = {"active": True, "reasons": reasons, "since": self._cache.get("since") or now_ms()}
        try:
            self.db.set_kv(KEY, self._cache)
            self.db.log_event("CRITICAL", "kill_switch", f"TRIGGERED {code}: {detail}")
        except Exception:
            # even if the DB is failing, in-memory state stays active for this process
            pass

    def clear_transient(self, code: str) -> None:
        """Auto-clear a non-sticky reason (e.g. stale data recovered). Sticky reasons stay."""
        if code in STICKY:
            return
        reasons = [r for r in self._cache.get("reasons", []) if r["code"] != code]
        self._cache = {"active": bool(reasons), "reasons": reasons, "since": self._cache.get("since") if reasons else None}
        self.db.set_kv(KEY, self._cache)

    def reset(self, operator: str, note: str = "") -> None:
        self.db.log_event("WARNING", "kill_switch", f"RESET by {operator}", {"previous": self._cache, "note": note})
        self._cache = {"active": False, "reasons": []}
        self.db.set_kv(KEY, self._cache)
