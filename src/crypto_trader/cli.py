"""Command line entry point: ``uv run cat <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from crypto_trader.core.config import ROOT, load_settings
from crypto_trader.core.timeutils import now_ms, parse_utc
from crypto_trader.storage.parquet_store import ParquetStore


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    import os

    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def _store(settings) -> ParquetStore:
    return ParquetStore(settings.path(settings.data_dir))


def cmd_probe(args, settings) -> int:
    from crypto_trader.collectors.jobs import check_blocked_sources, probe_apis

    res = asyncio.run(probe_apis(ROOT / "docs" / "generated"))
    for r in res:
        print(f"{r['verdict']:<14} {r['exchange']:<8} {r['transport']:<4} {str(r['status']):<5} {r['latency_ms']:>5}ms  {r['endpoint']}")
    print(json.dumps(asyncio.run(check_blocked_sources()), indent=2))
    return 0


def cmd_collect_history(args, settings) -> int:
    from crypto_trader.collectors import jobs

    store = _store(settings)
    start_ms = parse_utc(args.start)
    end_ms = parse_utc(args.end) if args.end else now_ms()
    out: dict = {}
    if "upbit" in args.sources:
        for iv in args.intervals:
            out[f"upbit_{iv}"] = asyncio.run(jobs.collect_upbit_history(store, args.markets or settings.markets, iv, start_ms, end_ms))
    if "bithumb" in args.sources:
        out["bithumb_1h"] = asyncio.run(jobs.collect_upbit_history(store, args.markets or settings.markets, "1h",
                                                                   start_ms, end_ms, exchange="bithumb"))
    if "binance_archive" in args.sources:
        syms = list(settings.futures_symbols.values())
        out["binance_archive"] = asyncio.run(jobs.collect_binance_archive(
            store, syms, datetime.fromtimestamp(start_ms / 1000, UTC).date(),
            datetime.fromtimestamp(end_ms / 1000, UTC).date(), settings.path(settings.data_dir) / "cache" / "binance"))
    if "upbit_trades" in args.sources:
        out["upbit_trades"] = asyncio.run(jobs.collect_upbit_trades_backfill(store, args.markets or settings.markets, args.trade_days))
    if "okx" in args.sources:
        out["okx"] = asyncio.run(jobs.collect_okx_recent(store, list(settings.futures_symbols.values()), start_ms, end_ms))
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_collect_live(args, settings) -> int:
    from crypto_trader.collectors import jobs

    store = _store(settings)
    duration = args.duration if args.duration and args.duration > 0 else None  # 0 -> run forever (service)
    stats = asyncio.run(jobs.collect_live(store, args.markets or settings.markets, list(settings.futures_symbols.values()),
                                          duration, args.poll, settings=settings))
    print(json.dumps(stats, indent=2, default=str))
    return 0


def cmd_validate(args, settings) -> int:
    rep = _store(settings).validate(repair=not args.dry_run, compact=args.compact)
    print(json.dumps(rep.__dict__, indent=2))
    return 0


def cmd_quality(args, settings) -> int:
    from crypto_trader.reports.quality_report import build_quality_report

    path = build_quality_report(_store(settings), settings, Path(args.out))
    print(path)
    return 0


def cmd_backtest(args, settings) -> int:
    from crypto_trader.backtest.research import run_research

    path = run_research(settings, _store(settings), Path(args.out), interval=args.interval, markets=args.markets,
                        fast=args.fast)
    print(path)
    return 0


def cmd_paper(args, settings) -> int:
    from crypto_trader.execution.runner import run_paper

    return asyncio.run(run_paper(settings, duration_s=args.duration, strategy_name=args.strategy))


def cmd_paper_fleet(args, settings) -> int:
    from crypto_trader.execution.runner import run_paper_fleet

    inst = [(i["strategy"], i["symbol"]) for i in settings.paper_instances]
    if args.only:
        inst = [x for x in inst if x[0] in args.only or x[1] in args.only]
    duration = args.duration if args.duration and args.duration > 0 else None
    return asyncio.run(run_paper_fleet(settings, inst, duration))


def cmd_paper_report(args, settings) -> int:
    from crypto_trader.core.timeutils import now_ms
    from crypto_trader.reports.paper_report import build_paper_report

    end = now_ms()
    path, headline = asyncio.run(build_paper_report(settings, end - int(args.days * 86_400_000), end, Path(args.out)))
    print(headline)
    print(path)
    return 0


def cmd_compact(args, settings) -> int:
    from datetime import timedelta

    cutoff = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%d")
    store = _store(settings)
    before = store.file_count()
    res = store.compact(cutoff)
    print(json.dumps({**res, "files_before_total": before, "files_after_total": store.file_count()}, indent=2))
    return 0


def cmd_notify_test(args, settings) -> int:
    from crypto_trader.ops.notify import describe_config, get_notifier, ping

    cfg = describe_config()
    print(json.dumps(cfg, indent=2))  # key names only, never values
    ok = get_notifier().send("✅ notify test from crypto-auto-trader")
    print("telegram:", "sent" if ok else "NOT sent (check TELEGRAM_* in .env)")
    for k in ("HEALTHCHECK_URL_COLLECTOR", "HEALTHCHECK_URL_PAPER"):
        print(k, "ping ok" if ping(k, msg="test") else "not configured / failed")
    return 0 if ok else 1


def cmd_live(args, settings) -> int:
    from crypto_trader.execution.runner import run_live

    return asyncio.run(run_live(settings, duration_s=args.duration, strategy_name=args.strategy))


def cmd_dashboard(args, settings) -> int:
    import uvicorn

    from crypto_trader.dashboard.app import create_app

    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_kill(args, settings) -> int:
    from crypto_trader.risk.kill_switch import KillSwitch
    from crypto_trader.storage.ops_db import OpsDB

    db = OpsDB(settings.path(settings.ops_db), args.mode)
    ks = KillSwitch(db)
    if args.action == "on":
        ks.trigger("MANUAL", args.reason or "operator request")
    elif args.action == "off":
        ks.reset(operator=args.operator or "operator", note=args.reason or "")
    print(json.dumps(ks.status(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # never log request URLs (Telegram token is in the URL)
    _load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(prog="cat", description="crypto-auto-trader")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="check API reachability from this network")

    h = sub.add_parser("collect-history")
    h.add_argument("--sources", nargs="+", default=["upbit", "binance_archive", "okx"])
    h.add_argument("--markets", nargs="*")
    h.add_argument("--intervals", nargs="+", default=["1h"])
    h.add_argument("--start", default="2022-01-01")
    h.add_argument("--end", default=None)
    h.add_argument("--trade-days", type=int, default=7)

    lv = sub.add_parser("collect-live")
    lv.add_argument("--markets", nargs="*")
    lv.add_argument("--duration", type=float, default=600, help="seconds; 0 = run forever (service)")
    lv.add_argument("--poll", type=float, default=60)

    v = sub.add_parser("validate")
    v.add_argument("--dry-run", action="store_true")
    v.add_argument("--compact", action="store_true")

    q = sub.add_parser("quality-report")
    q.add_argument("--out", default=str(ROOT / "reports" / "data_quality.html"))

    b = sub.add_parser("backtest")
    b.add_argument("--out", default=str(ROOT / "reports" / "backtest_report.html"))
    b.add_argument("--interval", default="1h")
    b.add_argument("--markets", nargs="*", default=None)
    b.add_argument("--fast", action="store_true", help="skip bootstrap/sensitivity (smoke run)")

    pp = sub.add_parser("paper")
    pp.add_argument("--duration", type=float, default=None)
    pp.add_argument("--strategy", default="A2_sweep_choch")

    pf = sub.add_parser("paper-fleet", help="run all configured paper_instances (no real orders)")
    pf.add_argument("--duration", type=float, default=0, help="seconds; 0 = run forever")
    pf.add_argument("--only", nargs="*", help="filter by strategy or market name")

    pr = sub.add_parser("paper-report", help="paper vs backtest comparison report")
    pr.add_argument("--days", type=float, default=7)
    pr.add_argument("--out", default=str(ROOT / "reports" / "paper_report.html"))

    sub.add_parser("compact", help="merge small parquet part files of closed days")
    sub.add_parser("notify-test", help="send a Telegram test message and ping health checks")

    ll = sub.add_parser("live")
    ll.add_argument("--duration", type=float, default=None)
    ll.add_argument("--strategy", default="A2_sweep_choch")

    d = sub.add_parser("dashboard")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8501)

    k = sub.add_parser("kill-switch")
    k.add_argument("action", choices=["on", "off", "status"])
    k.add_argument("--mode", default="PAPER")
    k.add_argument("--reason", default="")
    k.add_argument("--operator", default="")

    args = p.parse_args(argv)
    settings = load_settings(args.config)
    handlers = {"probe": cmd_probe, "collect-history": cmd_collect_history, "collect-live": cmd_collect_live,
                "validate": cmd_validate, "quality-report": cmd_quality, "backtest": cmd_backtest, "paper": cmd_paper,
                "live": cmd_live, "dashboard": cmd_dashboard, "kill-switch": cmd_kill, "paper-fleet": cmd_paper_fleet,
                "paper-report": cmd_paper_report, "compact": cmd_compact, "notify-test": cmd_notify_test}
    return handlers[args.cmd](args, settings)


if __name__ == "__main__":
    raise SystemExit(main())

