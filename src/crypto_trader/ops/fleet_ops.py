"""Unattended-operation duties shared by the long-running services.

* FleetOps (PAPER fleet): watchdog, kill-switch alerts, dead-man pings, daily summary (09:00 KST), weekly
  paper-vs-backtest report (Mon 09:10 KST), disk alerts.
* CollectorOps (market data collector): watchdog, hourly compaction of closed partitions, disk alerts, pings,
  per-day collection stats for the daily summary.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from crypto_trader.core.config import ROOT, Settings
from crypto_trader.core.timeutils import fmt_kst, ms_to_kst, now_ms
from crypto_trader.ops.disk import disk_status
from crypto_trader.ops.notify import get_notifier, ping
from crypto_trader.ops.watchdog import Watchdog
from crypto_trader.risk import kill_switch as ks_mod
from crypto_trader.storage.ops_db import OpsDB
from crypto_trader.storage.parquet_store import ParquetStore

log = logging.getLogger(__name__)


def _kill_hook(scope: str, code: str, detail: str) -> None:
    transient = code in ("STALE_MARKET_DATA", "ABNORMAL_SPREAD", "STALE_DERIVATIVES", "CLOCK_SKEW", "API_ERROR")
    get_notifier().send(f"{'⚠️' if transient else '🛑'} Kill switch {code} [{scope}]\n{detail[:300]}",
                        key=f"ks:{scope}:{code}", min_interval_s=3600 if transient else 60)


class _Every:
    def __init__(self, seconds: float):
        self.seconds, self.last = seconds, 0.0

    def due(self) -> bool:
        if time.time() - self.last >= self.seconds:
            self.last = time.time()
            return True
        return False


def _kst_slot(hour: int, minute: int, weekday: int | None = None) -> str | None:
    """Return a slot id once the KST wall clock has passed hour:minute today (and weekday matches)."""
    now = ms_to_kst(now_ms())
    if weekday is not None and now.weekday() != weekday:
        return None
    if (now.hour, now.minute) < (hour, minute):
        return None
    return now.strftime("%Y-%m-%d")


class FleetOps:
    def __init__(self, settings: Settings, global_db: OpsDB, sessions: list, ws, feeds: dict):
        self.s, self.db, self.sessions, self.ws, self.feeds = settings, global_db, sessions, ws, feeds
        self.notifier = get_notifier()
        self.watchdog = Watchdog("paper-fleet", timeout_s=300, on_stall=lambda m: self.notifier.send("🛑 " + m))
        self.ping_every, self.disk_every = _Every(300), _Every(600)
        self.seen_gaps = 0

    def start(self) -> None:
        if _kill_hook not in ks_mod.TRIGGER_HOOKS:
            ks_mod.TRIGGER_HOOKS.append(_kill_hook)
        self.watchdog.start()
        names = ", ".join(f"{s.core.strategy.label}/{s.symbol}" for s in self.sessions)
        feeds = ", ".join(f"{k}:{v.status}" for k, v in self.feeds.items()) or "none"
        self.notifier.send(f"▶️ PAPER fleet started ({len(self.sessions)} instances)\n{names}\nderivatives: {feeds}",
                           key="start", min_interval_s=60)

    def stop(self) -> None:
        self.watchdog.stop()

    async def tick(self) -> None:
        self.watchdog.tick()
        self.db.heartbeat("paper-fleet", "OK", f"{len(self.sessions)} instances")
        if self.ping_every.due():
            healthy = self.ws.health.connected and not any(s.ks.parent and s.ks.parent.own_active for s in self.sessions)
            ping("HEALTHCHECK_URL_PAPER", fail=not healthy, msg=self.summary_text(short=True))
        if self.disk_every.due():
            self._disk_check()
        gaps = self.ws.health.gaps
        if len(gaps) > self.seen_gaps:
            g = gaps[-1]
            self.notifier.send(f"⚠️ data gap {g.get('symbol')} {g.get('stream')}: {g.get('reason')}",
                               key=f"gap:{g.get('symbol')}:{g.get('stream')}", min_interval_s=1800)
            self.seen_gaps = len(gaps)
        slot = _kst_slot(9, 0)
        if slot and self.db.get_kv("daily_summary_sent") != slot:
            self.db.set_kv("daily_summary_sent", slot)
            self.notifier.send(self.summary_text(), key=f"daily:{slot}", min_interval_s=3600)
        wslot = _kst_slot(9, 10, weekday=0)
        if wslot and self.db.get_kv("weekly_report_sent") != wslot:
            self.db.set_kv("weekly_report_sent", wslot)
            await self._weekly_report()

    def _disk_check(self) -> None:
        st = disk_status(self.s.path(self.s.data_dir))
        self.db.set_kv("disk", st)
        if st["level"] != "OK":
            self.notifier.send(f"{'🛑' if st['level'] == 'CRITICAL' else '⚠️'} disk free {st['free_gb']} GB",
                               key=f"disk:{st['level']}", min_interval_s=6 * 3600)

    def summary_text(self, short: bool = False) -> str:
        lines = [f"📊 PAPER {fmt_kst(now_ms())}"]
        since = now_ms() - 86_400_000
        for s in self.sessions:
            eq = s.db.equity_curve(1)
            e = eq[-1]["equity_krw"] if eq else None
            alloc = s.core.risk.allocation
            fills = s.db.fills_between(since, now_ms())
            pos = s.core.ledger.positions.get(s.symbol)
            state = "KILL" if s.ks.active else ("LONG" if pos and pos.is_open else "flat")
            ret = f"{(e / alloc - 1) * 100:+.2f}%" if e else "-"
            lines.append(f"{s.core.strategy.label}/{s.symbol}: {ret} {state} fills24h={len(fills)}")
        if short:
            return "\n".join(lines)
        col = OpsDB(self.s.path("data/ops/ops_collector.sqlite"), "COLLECTOR")
        cstats = col.get_kv("daily_stats", {})
        disk = self.db.get_kv("disk", {})
        lines.append(f"collector: {cstats.get('summary', 'no stats')}")
        lines.append(f"WS reconnects={self.ws.health.reconnects} gaps={len(self.ws.health.gaps)} disk_free={disk.get('free_gb')}GB")
        lines.append("feeds: " + (", ".join(f"{k}:{v.status}" for k, v in self.feeds.items()) or "none"))
        return "\n".join(lines)

    async def _weekly_report(self) -> None:
        from crypto_trader.reports.paper_report import build_paper_report

        end = now_ms()
        start = end - 7 * 86_400_000
        out = ROOT / "reports" / f"paper_report_{ms_to_kst(end).strftime('%Y%m%d')}.html"
        try:
            path, headline = await build_paper_report(self.s, start, end, out)
            self.notifier.send(f"🗓 weekly PAPER report\n{headline}\nfile: {path.name}", key=f"weekly:{out.name}")
        except Exception as e:  # noqa: BLE001
            log.exception("weekly report failed")
            self.notifier.send(f"⚠️ weekly report failed: {type(e).__name__}: {str(e)[:200]}", key="weekly-fail")


class CollectorOps:
    def __init__(self, settings: Settings, store: ParquetStore, ws, sinks_stats):
        self.s, self.store, self.ws = settings, store, ws
        self.sinks_stats = sinks_stats  # callable -> dict of rows written so far
        self.db = OpsDB(settings.path("data/ops/ops_collector.sqlite"), "COLLECTOR")
        self.notifier = get_notifier()
        self.watchdog = Watchdog("collector", timeout_s=300, on_stall=lambda m: self.notifier.send("🛑 " + m))
        self.ping_every, self.disk_every, self.compact_every = _Every(300), _Every(600), _Every(3600)
        self.day_start_rows: dict | None = None
        self.day = ""
        self.seen_gaps = 0

    def start(self) -> None:
        self.watchdog.start()
        self.notifier.send("▶️ collector started", key="collector-start", min_interval_s=60)

    def stop(self) -> None:
        self.watchdog.stop()

    async def tick(self, okx_errors: list[str]) -> None:
        self.watchdog.tick()
        self.db.heartbeat("collector", "OK" if self.ws.health.connected else "WS_DOWN", self.ws.health.last_error[:200])
        rows = self.sinks_stats()
        day = ms_to_kst(now_ms()).strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.day_start_rows = day, dict(rows)
        today = {k: rows.get(k, 0) - (self.day_start_rows or {}).get(k, 0) for k in rows}
        self.db.set_kv("daily_stats", {"day": day, "rows": today, "summary": ", ".join(f"{k}={v:,}" for k, v in today.items()),
                                      "ws_reconnects": self.ws.health.reconnects, "okx_errors": len(okx_errors)})
        if self.ping_every.due():
            ping("HEALTHCHECK_URL_COLLECTOR", fail=not self.ws.health.connected, msg=str(today))
        if self.disk_every.due():
            st = disk_status(self.s.path(self.s.data_dir))
            self.db.set_kv("disk", st)
            if st["level"] != "OK":
                self.notifier.send(f"{'🛑' if st['level'] == 'CRITICAL' else '⚠️'} collector disk free {st['free_gb']} GB",
                                   key=f"cdisk:{st['level']}", min_interval_s=6 * 3600)
        if len(self.ws.health.gaps) > self.seen_gaps:
            g = self.ws.health.gaps[-1]
            self.notifier.send(f"⚠️ collector gap {g.get('symbol')} {g.get('stream')}: {g.get('reason')}",
                               key=f"cgap:{g.get('symbol')}:{g.get('stream')}", min_interval_s=1800)
            self.seen_gaps = len(self.ws.health.gaps)
        if self.compact_every.due():
            # yesterday's UTC partitions are closed once we are past 01:00 UTC
            cutoff = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d")
            try:
                res = await asyncio.to_thread(self.store.compact, cutoff)
                self.db.set_kv("last_compaction", {**res, "ts": now_ms(), "files_now": self.store.file_count()})
            except Exception as e:  # noqa: BLE001
                self.notifier.send(f"⚠️ compaction failed: {type(e).__name__}: {str(e)[:200]}", key="compact-fail")
