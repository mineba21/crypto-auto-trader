"""Operator tool: create a signed, time-limited LIVE approval (allowed capital, strategies, markets, period).

    uv run python scripts/approve_live.py --by "name" --max-krw 300000 --strategies A2_sweep_choch --markets KRW-BTC --days 14

This does NOT start trading. LIVE additionally needs config mode LIVE + live.enabled + CAT_ENABLE_LIVE.
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_trader.brokers.upbit_live import write_approval  # noqa: E402
from crypto_trader.core.config import ROOT, load_settings  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--by", required=True)
    p.add_argument("--max-krw", type=float, required=True)
    p.add_argument("--strategies", nargs="+", required=True)
    p.add_argument("--markets", nargs="+", required=True)
    p.add_argument("--days", type=int, default=14)
    a = p.parse_args()
    s = load_settings()
    if a.max_krw > s.capital.max_trading_capital_krw:
        print("max-krw exceeds max_trading_capital_krw", file=sys.stderr)
        return 1
    secret = os.environ.get("CAT_LIVE_APPROVAL_SECRET") or getpass.getpass("approval secret: ")
    print(f"About to approve LIVE trading: {a.max_krw:,.0f} KRW, strategies={a.strategies}, markets={a.markets}, {a.days} days")
    if input("type APPROVE to continue: ").strip() != "APPROVE":
        print("aborted")
        return 1
    out = ROOT / s.live.approval_file
    write_approval(out, secret, a.by, a.max_krw, a.strategies, a.markets, a.days)
    print(f"written {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
