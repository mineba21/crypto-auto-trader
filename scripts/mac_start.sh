#!/usr/bin/env bash
# One-shot start of the 4-week PAPER run on a Mac (no real orders, no exchange keys needed).
#   scripts/mac_start.sh [/path/to/other/.env-with-telegram]
# Safe to re-run: it rebuilds and restarts the services, keeping all collected data and paper state.
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker/compose.yaml"
TG_SRC="${1:-/Users/soohan/Desktop/claude/test1/myStockApp-scanner-vm/.env}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

step "1/7 Docker"
if ! docker info >/dev/null 2>&1; then
  open -a Docker 2>/dev/null || true
  echo "waiting for Docker Desktop to start..."
  for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
fi
docker info >/dev/null 2>&1 || { echo "Docker Desktop is not running. Install/start it and re-run."; exit 1; }

step "2/7 .env (Telegram keys only; values are never printed)"
[ -f .env ] || cp .env.example .env
chmod 600 .env
if [ -f "$TG_SRC" ]; then scripts/import_telegram_env.sh "$TG_SRC"; else echo "skip: $TG_SRC not found"; fi
grep -qE '^HEALTHCHECK_URL_(COLLECTOR|PAPER)=.+' .env || \
  echo "note: no HEALTHCHECK_URL_* in .env -> you will not be alerted if this Mac sleeps (see docs/MAC_SETUP.md §1-4)"

step "3/7 Prevent sleep while on power (asks for your password once)"
if [ "$(pmset -g custom 2>/dev/null | awk '/AC Power/{f=1} f&&/ sleep /{print $2; exit}')" != "0" ]; then
  sudo pmset -c sleep 0 disksleep 0 || echo "could not change sleep settings; set them in System Settings"
fi
pmset -g custom | awk '/AC Power/{f=1} f&&/ sleep /{print "AC sleep =", $2; exit}'

step "4/7 Build image"
$COMPOSE build

step "5/7 Checks: Telegram + exchange reachability from this network"
$COMPOSE run --rm paper notify-test || echo "Telegram not configured yet (fill TELEGRAM_* in .env and re-run)"
$COMPOSE run --rm paper probe | tee reports/probe_latest.txt || true

step "6/7 Binance derivatives history for B1 (only if Binance futures is reachable)"
if grep -q '"binance_fapi": "OK"' reports/probe_latest.txt 2>/dev/null; then
  since=$(date -v-120d +%Y-%m-%d 2>/dev/null || date -d '-120 days' +%Y-%m-%d)
  $COMPOSE run --rm paper collect-history --sources binance_archive --start "$since"
else
  echo "Binance futures not reachable -> B1 instance will be skipped automatically"
fi

step "7/7 Start collector + paper fleet + dashboard"
$COMPOSE up -d
sleep 20
$COMPOSE ps
echo
echo "Dashboard: http://127.0.0.1:8501   (open it now: open http://127.0.0.1:8501)"
echo "Logs:      $COMPOSE logs -f paper"
echo "Stop:      $COMPOSE down"
