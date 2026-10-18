"""Typed configuration loaded from YAML (config/*.yaml) with environment overrides for secrets."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from crypto_trader.core.models import Mode

ROOT = Path(__file__).resolve().parents[3]


class CapitalConfig(BaseModel):
    total_capital_krw: float = 10_000_000
    protected_external_krw: float = 7_000_000
    max_trading_capital_krw: float = 3_000_000
    initial_live_allocation_krw: float = 300_000
    max_total_loss_krw: float = 3_000_000
    emergency_cum_loss_krw: float = 1_500_000


class RiskConfig(BaseModel):
    per_trade_risk_cap_krw: float = 30_000
    per_trade_risk_pct: float = 0.015
    daily_loss_cap_krw: float = 60_000
    daily_loss_pct: float = 0.03
    max_exposure_pct: float = 1.0  # of strategy allocation, incl. open orders
    max_positions: int = 3
    max_orders_per_minute: int = 6
    consecutive_loss_scale_after: int = 3  # after N straight stop-outs, scale size
    consecutive_loss_scale: float = 0.5
    drawdown_scale_start_pct: float = 0.05  # equity below peak by this -> start shrinking
    min_size_scale: float = 0.25
    market_data_stale_ms: int = 15_000
    derivatives_stale_ms: int = 15 * 60_000
    max_clock_skew_ms: int = 2_000
    max_spread_bps: float = 30.0
    price_jump_sigma: float = 8.0
    min_order_krw: float = 5_000


class CostConfig(BaseModel):
    # Round-trip total cost scenarios (fee+spread+slippage), as fraction of notional.
    scenarios: dict[str, float] = Field(default_factory=lambda: {"optimistic": 0.0016, "base": 0.0030, "stress": 0.0050})
    upbit_fee_rate: float = 0.0005  # per side; verify at runtime with /v1/orders/chance (bid_fee/ask_fee)
    max_bar_participation: float = 0.05  # backtest partial-fill cap: fraction of bar volume


class LiveApprovalConfig(BaseModel):
    """LIVE requires BOTH this file block AND an approval file signed with the operator token (see docs/LIVE_RUNBOOK.md)."""

    enabled: bool = False
    approval_file: str = "config/live_approval.json"


class Settings(BaseModel):
    mode: Mode = Mode.PAPER
    data_dir: str = "data"
    ops_db: str = "data/ops/ops.sqlite"
    markets: list[str] = Field(default_factory=lambda: ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-USDT"])
    futures_symbols: dict[str, str] = Field(
        default_factory=lambda: {"KRW-BTC": "BTCUSDT", "KRW-ETH": "ETHUSDT", "KRW-XRP": "XRPUSDT"}
    )
    capital: CapitalConfig = Field(default_factory=CapitalConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    costs: CostConfig = Field(default_factory=CostConfig)
    live: LiveApprovalConfig = Field(default_factory=LiveApprovalConfig)
    strategy_allocation_krw: float = 300_000
    # PAPER fleet: each (strategy, market) runs as an isolated virtual account of strategy_allocation_krw
    paper_instances: list[dict] = Field(default_factory=lambda: [
        {"strategy": st, "symbol": sym}
        for st in ("A2_sweep_choch", "BASE_ema_trend", "BASE_donchian")
        for sym in ("KRW-BTC", "KRW-ETH", "KRW-XRP")
    ])
    config_version: str = ""

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path or os.environ.get("CAT_CONFIG", ROOT / "config" / "default.yaml"))
    raw = path.read_bytes() if path.exists() else b""
    data = yaml.safe_load(raw) or {}
    s = Settings(**data)
    s.config_version = hashlib.sha256(raw).hexdigest()[:12] if raw else "defaults"
    return s


def upbit_keys() -> tuple[str | None, str | None]:
    return os.environ.get("UPBIT_ACCESS_KEY"), os.environ.get("UPBIT_SECRET_KEY")
