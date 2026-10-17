"""Strategy interface shared by BACKTEST, PAPER and LIVE.

A strategy is *stateless with respect to time*: ``prepare`` computes causal feature arrays for a bar frame and
``on_bar(i, ...)`` decides using only index ``<= i``. Live execution calls ``prepare`` on a rolling window and
``on_bar`` on the last closed bar, so the decision function is literally the same code as in the backtest.
Entries are long-only; ``EXIT`` closes an existing long; ``BLOCK`` vetoes new buys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np
import polars as pl

from crypto_trader.core.models import Position, Signal


@dataclass
class Features:
    ts: np.ndarray  # bar close_ts (decision time)
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    cols: dict[str, np.ndarray] = field(default_factory=dict)

    def __getitem__(self, k: str) -> np.ndarray:
        return self.cols[k]

    def has(self, k: str) -> bool:
        return k in self.cols

    def __len__(self) -> int:
        return len(self.close)

    @classmethod
    def from_frame(cls, df: pl.DataFrame) -> Features:
        base = {"close_ts", "open", "high", "low", "close", "volume"}
        cols = {c: df[c].to_numpy() for c in df.columns
                if c not in base and (df[c].dtype.is_numeric() or df[c].dtype == pl.Boolean)}
        return cls(df["close_ts"].to_numpy(), df["open"].to_numpy().astype(float), df["high"].to_numpy().astype(float),
                   df["low"].to_numpy().astype(float), df["close"].to_numpy().astype(float),
                   df["volume"].to_numpy().astype(float), cols)


class Strategy:
    name: ClassVar[str] = "base"
    family: ClassVar[str] = "base"  # A / B / C / BASE / COMBO
    required_columns: ClassVar[tuple[str, ...]] = ()
    default_params: ClassVar[dict[str, Any]] = {}
    is_filter: ClassVar[bool] = False

    def __init__(self, **params: Any):
        self.params = {**self.default_params, **params}
        self.label = self.name if not params else f"{self.name}({','.join(f'{k}={v}' for k, v in params.items())})"

    @property
    def warmup(self) -> int:
        return int(self.params.get("warmup", 50))

    def missing_inputs(self, f: Features) -> list[str]:
        return [c for c in self.required_columns if not f.has(c) or np.all(np.isnan(f[c].astype(float)))]

    def prepare(self, f: Features) -> Features:
        """Add strategy-specific causal columns to ``f.cols`` (in place) and return f."""
        return f

    def entry(self, i: int, f: Features) -> Signal | None:
        raise NotImplementedError

    def exit(self, i: int, f: Features, pos: Position, bars_held: int) -> Signal | None:
        mh = self.params.get("max_hold")
        if mh and bars_held >= mh:
            return Signal(int(f.ts[i]), pos.symbol, self.label, "EXIT", reason=f"time stop {bars_held} bars")
        return None

    def block(self, i: int, f: Features) -> str | None:
        """Return a reason to veto new entries at bar i (filters)."""
        return None

    def on_bar(self, i: int, f: Features, symbol: str, pos: Position | None, bars_held: int = 0) -> list[Signal]:
        if i < self.warmup:
            return []
        if pos is not None and pos.is_open:
            s = self.exit(i, f, pos, bars_held)
            return [s] if s else []
        reason = self.block(i, f)
        if reason:
            return [Signal(int(f.ts[i]), symbol, self.label, "BLOCK", reason=reason)]
        s = self.entry(i, f)
        if s is None:
            return []
        return [Signal(s.ts, symbol, self.label, s.action, s.stop_price, s.take_profit, s.reason, s.score, s.meta)]

    def _sig(self, i: int, f: Features, stop: float, tp: float | None, reason: str, score: float = 1.0) -> Signal | None:
        entry_ref = f.close[i]
        if not np.isfinite(stop) or stop <= 0 or stop >= entry_ref:
            return None
        return Signal(int(f.ts[i]), "", self.label, "ENTER", float(stop), float(tp) if tp else None, reason, score)
