#!/usr/bin/env bash
# Reproduce Phase 1 + 2 end to end (public data only, no keys).
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync
uv run cat probe
uv run cat collect-history --sources upbit --intervals 1h 15m 1d --start 2022-01-01
uv run cat collect-history --sources binance_archive --start 2022-01-01
uv run cat collect-history --sources okx --start 2026-01-01
uv run cat collect-history --sources upbit_trades --markets KRW-BTC KRW-ETH KRW-XRP --trade-days 7
uv run cat collect-live --duration 1800
uv run cat validate
uv run cat quality-report
uv run pytest -m "not network"
uv run cat backtest
