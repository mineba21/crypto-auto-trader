# AGENTS.md

Instructions for coding agents (Codex, Claude, etc.) working in this repository.

**Read `HANDOFF.md` first** — current state, what is running on the operator's Mac, rules, and the next tasks.

## Non-negotiable
- Never enable LIVE trading or create/modify `config/live.yaml`, `config/live_approval.json`, or `CAT_ENABLE_LIVE`.
- Long-only Upbit KRW spot. Foreign futures data is read-only (filters only). No shorting, no leverage.
- No look-ahead: features must be causal; external data joins by `available_ts` (as-of backward).
- Do not bypass geo-blocked APIs. Do not fabricate market data; record gaps instead.
- Never print or commit secrets (`.env` is gitignored).
- Keep the running 4-week PAPER experiment backward compatible (instance mode names, ops DB keys, parquet schema).

## Before pushing
```bash
uv sync
uv run ruff check src tests
uv run pytest -m "not network"
```

## Layout
Source in `src/crypto_trader/`, tests in `tests/`, docs in `docs/` (ARCHITECTURE, API_ACCESS, PHASE_REPORT, MAC_SETUP, LIVE_RUNBOOK).
CLI entry point: `uv run cat <command>` (`src/crypto_trader/cli.py`). Explanations to the operator in Korean.
