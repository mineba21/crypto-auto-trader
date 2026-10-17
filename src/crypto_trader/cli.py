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
    stats = asyncio.run(jobs.collect_live(store, args.markets or settings.markets, list(settings.futures_symbols.values()),
                                          args.duration, args.poll))
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
    logging.getLogger("httpx").setLevel(logging.WARNING)
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
    lv.add_argument("--duration", type=float, default=600)
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
                "live": cmd_live, "dashboard": cmd_dashboard, "kill-switch": cmd_kill}
    return handlers[args.cmd](args, settings)


if __name__ == "__main__":
    raise SystemExit(main())

