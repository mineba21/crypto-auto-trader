"""Concrete strategies. Every threshold is a parameter; defaults are starting points, not tuned optima."""

from __future__ import annotations

import numpy as np

from crypto_trader.core.models import Position, Signal
from crypto_trader.features import smc
from crypto_trader.features.common import atr, ema, rolling_max, rolling_min, shift, sma
from crypto_trader.features.orderflow import cvd_bullish_divergence, normalized_delta, rolling_delta
from crypto_trader.strategies.base import Features, Strategy


def _base_cols(f: Features, atr_n: int = 14) -> None:
    key = f"atr{atr_n}"
    if key not in f.cols:
        f.cols[key] = atr(f.high, f.low, f.close, atr_n)
    f.cols["atr"] = f.cols[key]


def _swing_cols(f: Features, left: int, right: int) -> smc.SwingState:
    key = f"_sw_{left}_{right}"
    if key not in f.cols:
        f.cols[key] = smc.swings(f.high, f.low, left, right)  # type: ignore[assignment]
    return f.cols[key]  # type: ignore[return-value]


# ============================================================== baselines
class DonchianBreakout(Strategy):
    """OHLCV baseline: N-bar high breakout with long trend filter, ATR stop and M-bar low trailing exit."""

    name, family = "BASE_donchian", "BASE"
    default_params = {"n": 48, "exit_n": 24, "trend": 200, "stop_atr": 2.0, "warmup": 210}

    def prepare(self, f):
        _base_cols(f)
        p = self.params
        f.cols["dc_hi"] = shift(rolling_max(f.high, p["n"]), 1)
        f.cols["dc_lo_exit"] = shift(rolling_min(f.low, p["exit_n"]), 1)
        f.cols["sma_trend"] = sma(f.close, p["trend"])
        return f

    def entry(self, i, f):
        if f.close[i] > f["dc_hi"][i] and f.close[i] > f["sma_trend"][i]:
            return self._sig(i, f, f.close[i] - self.params["stop_atr"] * f["atr"][i], None, "donchian breakout")
        return None

    def exit(self, i, f, pos, bars_held):
        if f.close[i] < f["dc_lo_exit"][i]:
            return Signal(int(f.ts[i]), pos.symbol, self.label, "EXIT", reason="donchian trailing exit")
        return super().exit(i, f, pos, bars_held)


class EmaTrend(Strategy):
    name, family = "BASE_ema_trend", "BASE"
    default_params = {"fast": 20, "slow": 100, "stop_atr": 2.5, "warmup": 110}

    def prepare(self, f):
        _base_cols(f)
        f.cols["ema_f"] = ema(f.close, self.params["fast"])
        f.cols["ema_s"] = ema(f.close, self.params["slow"])
        return f

    def entry(self, i, f):
        ef, es = f["ema_f"], f["ema_s"]
        if ef[i] > es[i] and ef[i - 1] <= es[i - 1]:
            return self._sig(i, f, f.close[i] - self.params["stop_atr"] * f["atr"][i], None, "ema cross up")
        return None

    def exit(self, i, f, pos, bars_held):
        if f["ema_f"][i] < f["ema_s"][i]:
            return Signal(int(f.ts[i]), pos.symbol, self.label, "EXIT", reason="ema cross down")
        return super().exit(i, f, pos, bars_held)


# ============================================================== A: SMC / ICT
class _SmcBase(Strategy):
    family = "A"
    default_params = {"left": 3, "right": 3, "sweep_min_atr": 0.05, "sweep_max_atr": 2.0, "stop_buf_atr": 0.2,
                      "rr": 2.0, "tp_mode": "R", "max_hold": 48, "warmup": 60}

    def prepare(self, f):
        _base_cols(f)
        p = self.params
        sw = _swing_cols(f, p["left"], p["right"])
        f.cols["sweep_low"] = smc.bullish_sweep(f.low, f.close, sw, f["atr"], p["sweep_min_atr"], p["sweep_max_atr"])
        f.cols["sw_last_high"] = sw.last_high
        f.cols["sw_prev_high"] = sw.prev_high
        return f

    def _tp(self, i, f, entry, stop):
        risk = entry - stop
        if self.params["tp_mode"] == "structure":
            # nearest confirmed swing high at least 1R above entry (last, else the one before it)
            for sh in (f["sw_last_high"][i], f["sw_prev_high"][i]):
                if np.isfinite(sh) and sh > entry + risk:
                    return sh
        return entry + self.params["rr"] * risk

    def _recent_sweep(self, i, f, k):
        sl = f["sweep_low"]
        for j in range(i, max(-1, i - k - 1), -1):
            if np.isfinite(sl[j]):
                return j
        return None


class A1LiquiditySweep(_SmcBase):
    """Sweep of the last confirmed swing low with a close back above it (structure reclaim) on a bullish bar."""

    name = "A1_sweep_reversal"

    def entry(self, i, f):
        sl = f["sweep_low"][i]
        if np.isfinite(sl) and f.close[i] > f.open[i]:
            stop = sl - self.params["stop_buf_atr"] * f["atr"][i]
            return self._sig(i, f, stop, self._tp(i, f, f.close[i], stop), "sweep+reclaim")
        return None


class A2SweepChoch(_SmcBase):
    """Sweep within the last K bars, then a bullish change of character (close through last lower-high)."""

    name = "A2_sweep_choch"
    default_params = {**_SmcBase.default_params, "k": 12}

    def prepare(self, f):
        super().prepare(f)
        sw = _swing_cols(f, self.params["left"], self.params["right"])
        f.cols["choch_up"] = smc.bullish_choch(f.close, sw)
        return f

    def entry(self, i, f):
        if not f["choch_up"][i]:
            return None
        j = self._recent_sweep(i, f, self.params["k"])
        if j is None:
            return None
        low_since = f.low[j:i + 1].min()
        stop = low_since - self.params["stop_buf_atr"] * f["atr"][i]
        return self._sig(i, f, stop, self._tp(i, f, f.close[i], stop), f"sweep@{i - j} bars + CHoCH")


class A3SweepFvg(_SmcBase):
    """Sweep, then a bullish FVG (displacement) within K bars, then first retrace into the gap holds."""

    name = "A3_sweep_fvg"
    default_params = {**_SmcBase.default_params, "k": 12, "min_gap_atr": 0.15, "fvg_ttl": 24}

    def prepare(self, f):
        super().prepare(f)
        b, t = smc.bullish_fvg(f.high, f.low, f["atr"], self.params["min_gap_atr"])
        f.cols["fvg_bot"], f.cols["fvg_top"] = b, t
        return f

    def entry(self, i, f):
        p = self.params
        bot, top = f["fvg_bot"], f["fvg_top"]
        for j in range(i - 1, max(1, i - p["fvg_ttl"]) - 1, -1):  # FVG must be completed before bar i
            if not np.isfinite(bot[j]):
                continue
            s = self._recent_sweep(j - 1, f, p["k"])
            if s is None:
                return None
            # invalidated or already traded into between j+1 and i-1?
            seg_low = f.low[j + 1:i]
            if len(seg_low) and (seg_low.min() <= top[j]):
                return None
            if f.low[i] <= top[j] and f.close[i] >= bot[j]:
                stop = min(bot[j], f.low[s:i + 1].min()) - p["stop_buf_atr"] * f["atr"][i]
                return self._sig(i, f, stop, self._tp(i, f, f.close[i], stop), f"FVG retrace (fvg@{i - j}, sweep@{i - s})")
            return None
        return None


# ============================================================== B: derivatives
class B1ShortSqueeze(Strategy):
    """Rising OI + negative funding (shorts paying) + futures taker buying + Upbit breakout.

    OI rising alone does not imply new shorts; we require the negative-funding and taker-buy legs too.
    """

    name, family = "B1_short_squeeze", "B"
    required_columns = ("oi_chg_4h", "funding_z", "fut_taker_buy_frac")
    default_params = {"oi_chg_min": 0.01, "funding_z_max": -1.0, "taker_buy_min": 0.52, "breakout_n": 24,
                      "stop_atr": 2.0, "rr": 2.0, "max_hold": 24, "warmup": 60}

    def prepare(self, f):
        _base_cols(f)
        f.cols["bo_hi"] = shift(rolling_max(f.high, self.params["breakout_n"]), 1)
        return f

    def entry(self, i, f):
        p = self.params
        oi, fz, tb = f["oi_chg_4h"][i], f["funding_z"][i], f["fut_taker_buy_frac"][i]
        if not (np.isfinite(oi) and np.isfinite(fz) and np.isfinite(tb)):
            return None
        if oi > p["oi_chg_min"] and fz < p["funding_z_max"] and tb > p["taker_buy_min"] and f.close[i] > f["bo_hi"][i]:
            stop = f.close[i] - p["stop_atr"] * f["atr"][i]
            return self._sig(i, f, stop, f.close[i] + p["rr"] * (f.close[i] - stop), "short squeeze setup")
        return None


class B2LiquidationReversal(Strategy):
    """Large drop + OI flush (deleveraging) + taker selling fading + Upbit structure recovery.

    ``require_liquidations=True`` additionally needs observed long liquidations (live capture only). Historical
    backtests run with it False and are labelled accordingly: no liquidation events are synthesised.
    """

    name, family = "B2_liq_reversal", "B"
    required_columns = ("oi_chg_24h", "fut_taker_buy_frac")
    default_params = {"drop_n": 24, "drop_pct": 0.05, "oi_flush": -0.03, "recover_n": 3, "stop_buf_atr": 0.3,
                      "rr": 2.0, "max_hold": 24, "require_liquidations": False, "liq_min_usd": 5e6, "warmup": 60}

    def prepare(self, f):
        _base_cols(f)
        n = self.params["drop_n"]
        f.cols["ret_n"] = f.close / shift(f.close, n) - 1
        f.cols["low_n"] = rolling_min(f.low, n)
        sw = _swing_cols(f, 2, 2)
        f.cols["bos_up_2"] = smc.break_of_structure_up(f.close, sw)
        tb = f["fut_taker_buy_frac"].astype(float)
        f.cols["tb_rise"] = tb - shift(rolling_min(np.nan_to_num(tb, nan=0.5), 6), 1)
        return f

    def entry(self, i, f):
        p = self.params
        lo = max(0, i - p["recover_n"])
        rets, ois = f["ret_n"][lo:i + 1], f["oi_chg_24h"][lo:i + 1].astype(float)
        if np.all(np.isnan(rets)) or np.all(np.isnan(ois)):
            return None
        ret, oi = np.nanmin(rets), np.nanmin(ois)
        if not (ret < -p["drop_pct"] and oi < p["oi_flush"]):
            return None
        if p["require_liquidations"]:
            if not f.has("long_liq_usd") or not np.nansum(f["long_liq_usd"][max(0, i - p["recover_n"]):i + 1]) >= p["liq_min_usd"]:
                return None
        recovered = bool(f["bos_up_2"][max(0, i - p["recover_n"] + 1):i + 1].any()) and f.close[i] > f.open[i]
        if not recovered or not (f["tb_rise"][i] > 0):
            return None
        stop = f["low_n"][i] - p["stop_buf_atr"] * f["atr"][i]
        return self._sig(i, f, stop, f.close[i] + p["rr"] * (f.close[i] - stop), "flush + recovery")


class B3CrowdingFilter(Strategy):
    """Pure filter: vetoes new buys under crowded-long conditions. Used inside combos / as overlay on baselines."""

    name, family = "B3_crowding_filter", "B"
    is_filter = True
    required_columns = ("funding_z", "oi_chg_24h_pctile")
    default_params = {"funding_z_max": 2.0, "oi_pctile_max": 0.95, "ret24_max": 0.05, "premium_z_max": 2.5, "warmup": 30}

    def prepare(self, f):
        f.cols["ret24"] = f.close / shift(f.close, max(1, int(86_400_000 // max(1, int(np.median(np.diff(f.ts[:100]))))))) - 1
        return f

    def block(self, i, f):
        p = self.params
        fz = f["funding_z"][i] if f.has("funding_z") else np.nan
        if np.isfinite(fz) and fz > p["funding_z_max"]:
            return f"crowded: funding_z={fz:.2f}"
        op = f["oi_chg_24h_pctile"][i] if f.has("oi_chg_24h_pctile") else np.nan
        if np.isfinite(op) and op > p["oi_pctile_max"] and f["ret24"][i] > p["ret24_max"]:
            return f"crowded: OI surge pct={op:.2f} & 24h ret={f['ret24'][i]:.1%}"
        pz = f["premium_z"][i] if f.has("premium_z") else np.nan
        if np.isfinite(pz) and pz > p["premium_z_max"]:
            return f"crowded: premium_z={pz:.2f}"
        return None

    def entry(self, i, f):
        return None


# ============================================================== C: order flow (needs Upbit trades)
class C1CvdBreakout(Strategy):
    name, family = "C1_cvd_breakout", "C"
    required_columns = ("delta", "valid")
    default_params = {"n": 12, "delta_n": 6, "nd_min": 0.15, "stop_atr": 1.5, "rr": 2.0, "max_hold": 24, "warmup": 30}

    def prepare(self, f):
        _base_cols(f)
        p = self.params
        f.cols["bo_hi"] = shift(rolling_max(f.high, p["n"]), 1)
        f.cols["rd"], f.cols["rd_ok"] = rolling_delta(f["delta"], f["valid"], p["delta_n"])
        f.cols["nd"] = normalized_delta(f["delta"], f.volume, p["delta_n"])
        return f

    def entry(self, i, f):
        p = self.params
        if not f["rd_ok"][i]:
            return None
        if f.close[i] > f["bo_hi"][i] and f["rd"][i] > 0 and f["nd"][i] > p["nd_min"]:
            stop = f.close[i] - p["stop_atr"] * f["atr"][i]
            return self._sig(i, f, stop, f.close[i] + p["rr"] * (f.close[i] - stop), "CVD-confirmed breakout")
        return None


class C2CvdDivergence(Strategy):
    name, family = "C2_cvd_divergence", "C"
    required_columns = ("cvd", "valid")
    default_params = {"lookback": 24, "confirm_n": 3, "stop_buf_atr": 0.3, "rr": 2.0, "max_hold": 24, "warmup": 30}

    def prepare(self, f):
        _base_cols(f)
        f.cols["div"] = cvd_bullish_divergence(f.low, f["cvd"], self.params["lookback"])
        f.cols["c2_ok"] = rolling_delta(f["delta"], f["valid"], self.params["lookback"])[1]
        return f

    def entry(self, i, f):
        p = self.params
        if not f["c2_ok"][i]:
            return None
        lo = max(0, i - p["confirm_n"])
        if f["div"][lo:i].any() and f.close[i] > f.open[i] and f.close[i] > f.high[i - 1]:
            stop = f.low[lo:i + 1].min() - p["stop_buf_atr"] * f["atr"][i]
            return self._sig(i, f, stop, f.close[i] + p["rr"] * (f.close[i] - stop), "CVD bullish divergence")
        return None


class C3Absorption(Strategy):
    """Heavy aggressive selling bar, then price recovers above that bar's open within a few bars."""

    name, family = "C3_absorption", "C"
    required_columns = ("delta", "valid")
    default_params = {"z": 2.5, "win": 48, "recover_n": 3, "stop_buf_atr": 0.3, "rr": 2.0, "max_hold": 24, "warmup": 50}

    def prepare(self, f):
        from crypto_trader.features.common import rolling_zscore

        _base_cols(f)
        f.cols["dz"] = rolling_zscore(f["delta"].astype(float), self.params["win"], min_periods=24)
        return f

    def entry(self, i, f):
        p = self.params
        for j in range(i - 1, max(0, i - p["recover_n"]) - 1, -1):
            if f["dz"][j] < -p["z"] and f["valid"][j:i + 1].all():
                if f.close[i] > f.open[j] and f.close[i - 1] <= f.open[j]:
                    stop = f.low[j:i + 1].min() - p["stop_buf_atr"] * f["atr"][i]
                    return self._sig(i, f, stop, f.close[i] + p["rr"] * (f.close[i] - stop), "sell absorption")
        return None


class C4ObiFlow(Strategy):
    """Bid-side book imbalance + aggressive buying. Requires recorded orderbook snapshots (live/paper only)."""

    name, family = "C4_obi_flow", "C"
    required_columns = ("obi", "obi_valid", "delta", "valid")
    default_params = {"obi_min": 0.2, "delta_n": 3, "stop_atr": 1.5, "rr": 1.5, "max_hold": 12, "max_spread_bps": 10,
                      "warmup": 20}

    def prepare(self, f):
        _base_cols(f)
        f.cols["rd"], f.cols["rd_ok"] = rolling_delta(f["delta"], f["valid"], self.params["delta_n"])
        return f

    def entry(self, i, f):
        p = self.params
        if not (f["obi_valid"][i] and f["rd_ok"][i]):
            return None
        if f.has("spread_bps") and f["spread_bps"][i] > p["max_spread_bps"]:
            return None
        if f["obi"][i] > p["obi_min"] and f["rd"][i] > 0:
            stop = f.close[i] - p["stop_atr"] * f["atr"][i]
            return self._sig(i, f, stop, f.close[i] + p["rr"] * (f.close[i] - stop), "OBI + buy flow")
        return None


# ============================================================== combined
class Combined(Strategy):
    """Combine component strategies.

    mode="and":    the primary's entry must coincide with every other component's entry within ``window`` bars.
    mode="filter": primary entries pass only if no filter component vetoes (``block``) at bar i.
    mode="score":  weighted sum of component votes (entry=1, block=-1) >= threshold; primary sets stop/target.
    """

    name, family = "COMBO", "COMBO"
    default_params = {"mode": "filter", "window": 6, "weights": None, "threshold": 1.0}

    def __init__(self, primary: Strategy, others: list[Strategy], label: str, **params):
        super().__init__(**params)
        self.primary, self.others = primary, others
        self.label = label
        self.required_columns = tuple({c for s in [primary, *others] for c in s.required_columns})  # type: ignore[misc]

    @property
    def warmup(self) -> int:
        return max(s.warmup for s in [self.primary, *self.others])

    def prepare(self, f):
        for s in [self.primary, *self.others]:
            s.prepare(f)
        return f

    def exit(self, i, f, pos, bars_held):
        return self.primary.exit(i, f, pos, bars_held)

    def block(self, i, f):
        if self.params["mode"] in ("filter", "and"):
            for s in self.others:
                r = s.block(i, f)
                if r:
                    return f"{s.name}: {r}"
        return None

    def entry(self, i, f):
        sig = self.primary.entry(i, f)
        mode, w = self.params["mode"], self.params["window"]
        if mode == "filter":
            return sig
        if mode == "and":
            if sig is None:
                return None
            for s in self.others:
                if getattr(s, "is_filter", False):
                    continue  # pure filters act through block()
                if not any(s.entry(j, f) is not None for j in range(max(s.warmup, i - w), i + 1)):
                    return None
            return sig
        # score
        if sig is None:
            return None
        weights = self.params["weights"] or {}
        score = weights.get(self.primary.name, 1.0)
        for s in self.others:
            wt = weights.get(s.name, 0.5)
            if s.block(i, f):
                score -= wt
            elif not getattr(s, "is_filter", False) and any(
                    s.entry(j, f) is not None for j in range(max(s.warmup, i - w), i + 1)):
                score += wt
        if score >= self.params["threshold"]:
            return Signal(sig.ts, sig.symbol, self.label, "ENTER", sig.stop_price, sig.take_profit,
                          f"{sig.reason} | score={score:.2f}", score)
        return None


class FlowConfirm(Strategy):
    """Component used inside combos: recent net aggressive buying on Upbit spot (needs trades)."""

    name, family = "C_flow_confirm", "C"
    required_columns = ("delta", "valid")
    default_params = {"delta_n": 6, "warmup": 10}

    def prepare(self, f):
        f.cols["fc_rd"], f.cols["fc_ok"] = rolling_delta(f["delta"], f["valid"], self.params["delta_n"])
        return f

    def entry(self, i, f):
        if f["fc_ok"][i] and f["fc_rd"][i] > 0:
            return Signal(int(f.ts[i]), "", self.name, "ENTER", reason="net buy flow")
        return None


class DerivConfirm(Strategy):
    """Component: OI not collapsing and funding not extreme-positive -> healthy participation."""

    name, family = "B_deriv_confirm", "B"
    required_columns = ("oi_chg_4h", "funding_z")
    default_params = {"oi_min": 0.0, "funding_z_max": 1.5, "warmup": 10}

    def entry(self, i, f):
        oi, fz = f["oi_chg_4h"][i], f["funding_z"][i]
        if np.isfinite(oi) and np.isfinite(fz) and oi > self.params["oi_min"] and fz < self.params["funding_z_max"]:
            return Signal(int(f.ts[i]), "", self.name, "ENTER", reason="deriv confirm")
        return None


REGISTRY: dict[str, type[Strategy]] = {c.name: c for c in (
    DonchianBreakout, EmaTrend, A1LiquiditySweep, A2SweepChoch, A3SweepFvg, B1ShortSqueeze, B2LiquidationReversal,
    B3CrowdingFilter, C1CvdBreakout, C2CvdDivergence, C3Absorption, C4ObiFlow, FlowConfirm, DerivConfirm)}


def build(name: str, **params) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(f"unknown strategy {name}; known: {sorted(REGISTRY)}")
    return REGISTRY[name](**params)


__all__ = ["REGISTRY", "build", "Combined", "FlowConfirm", "DerivConfirm", "Position"]
