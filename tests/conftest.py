import numpy as np
import polars as pl
import pytest

from crypto_trader.core.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.data_dir = str(tmp_path / "data")
    s.ops_db = str(tmp_path / "ops.sqlite")
    return s


def make_bars(close: np.ndarray, step_ms: int = 3_600_000, start: int = 1_700_000_000_000 // 3_600_000 * 3_600_000,
              spread: float = 0.004, seed: int = 1) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(close)
    open_ = np.concatenate([[close[0]], close[:-1]])
    hi = np.maximum(open_, close) * (1 + rng.uniform(0, spread, n))
    lo = np.minimum(open_, close) * (1 - rng.uniform(0, spread, n))
    ts = start + np.arange(n) * step_ms
    vol = rng.uniform(5, 15, n)
    return pl.DataFrame({"open_ts": ts, "close_ts": ts + step_ms, "open": open_, "high": hi, "low": lo, "close": close,
                         "volume": vol, "quote_volume": vol * close, "available_ts": ts + step_ms})


def random_walk(n: int = 3000, seed: int = 0, start: float = 50_000_000) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(0, 0.006, n)))


@pytest.fixture
def bars() -> pl.DataFrame:
    return make_bars(random_walk())
