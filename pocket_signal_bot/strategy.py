from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Literal, Optional

Signal = Literal["CALL", "PUT", "NO_TRADE"]


def ema(values: List[float], period: int) -> List[Optional[float]]:
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) < period:
        return [None] * len(values)
    out: List[Optional[float]] = [None] * len(values)
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        val = values[i] * k + prev * (1 - k)
        out[i] = val
        prev = val
    return out


def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) <= period:
        return [None] * len(values)

    out: List[Optional[float]] = [None] * len(values)
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(abs(min(d, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = max(d, 0)
        loss = abs(min(d, 0))
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    return out


@dataclass
class StrategyConfig:
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    buy_rsi_min: float = 52.0
    sell_rsi_max: float = 48.0
    # RSI within (50 - band, 50 + band) → NO_TRADE (neutral zone, chop filter)
    rsi_neutral_band: float = 1.5
    min_ema_gap: float = 0.0
    require_momentum_confirm: bool = True
    # >0: vote-fallback only fires when |fast_ema - slow_ema| >= this (reduces chop entries)
    min_abs_ema_diff: float = 0.0
    # If false, fallback vote is disabled; only strict triggers may trade.
    allow_fallback_vote: bool = True
    # Require EMA-diff sign consistency for this many recent points before any CALL/PUT.
    min_trend_streak: int = 2
    # Chop filter parameters (percentage range over lookback candles).
    chop_lookback: int = 20
    min_range_pct: float = 0.05


class EmaRsiStrategy:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def generate(self, close_prices: List[float]) -> Signal:
        return self.generate_details(close_prices)["signal"]

    def generate_details(self, close_prices: List[float]) -> dict[str, Any]:
        if len(close_prices) < 3:
            return {"signal": "NO_TRADE", "ema_diff": 0.0, "rsi": 50.0, "momentum": 0.0}

        available = len(close_prices)
        ema_slow_period = min(self.cfg.ema_slow, max(2, available - 1))
        ema_fast_period = min(self.cfg.ema_fast, max(1, ema_slow_period // 2))
        rsi_period = min(self.cfg.rsi_period, max(2, available - 1))

        f = ema(close_prices, ema_fast_period)
        s = ema(close_prices, ema_slow_period)
        r = rsi(close_prices, rsi_period)

        if f[-1] is None or s[-1] is None or r[-1] is None:
            return {"signal": "NO_TRADE", "ema_diff": 0.0, "rsi": 50.0, "momentum": 0.0}

        ema_diff = float(f[-1] - s[-1])
        rsi_val = float(r[-1])
        momentum = float(close_prices[-1] - close_prices[-3])

        # Regime filter: skip narrow ranges (typical whipsaw/chop zone).
        lookback = max(5, int(self.cfg.chop_lookback))
        if len(close_prices) >= lookback:
            recent = close_prices[-lookback:]
            hi = max(recent)
            lo = min(recent)
            base = abs(recent[-1]) if recent[-1] != 0 else 1.0
            range_pct = ((hi - lo) / base) * 100.0
            if range_pct < max(0.0, self.cfg.min_range_pct):
                return {"signal": "NO_TRADE", "ema_diff": ema_diff, "rsi": rsi_val, "momentum": momentum}

        # Trend-streak filter: require stable EMA-diff sign across recent points.
        need = max(1, int(self.cfg.min_trend_streak))
        if need > 1:
            diffs = [float(fi - si) for fi, si in zip(f, s) if fi is not None and si is not None]
            tail = diffs[-need:] if len(diffs) >= need else []
            if len(tail) < need:
                return {"signal": "NO_TRADE", "ema_diff": ema_diff, "rsi": rsi_val, "momentum": momentum}
            if not (all(x > 0 for x in tail) or all(x < 0 for x in tail)):
                return {"signal": "NO_TRADE", "ema_diff": ema_diff, "rsi": rsi_val, "momentum": momentum}

        # ── Primary strict triggers ──────────────────────────────────────────
        if ema_diff > self.cfg.min_ema_gap and rsi_val >= self.cfg.buy_rsi_min:
            if not self.cfg.require_momentum_confirm or momentum >= 0:
                return {"signal": "CALL", "ema_diff": ema_diff, "rsi": rsi_val, "momentum": momentum}

        if ema_diff < -self.cfg.min_ema_gap and rsi_val <= self.cfg.sell_rsi_max:
            if not self.cfg.require_momentum_confirm or momentum <= 0:
                return {"signal": "PUT", "ema_diff": ema_diff, "rsi": rsi_val, "momentum": momentum}

        # ── RSI neutral zone — stay flat in choppy / indecisive conditions ───
        neutral_band = max(0.0, self.cfg.rsi_neutral_band)
        if abs(rsi_val - 50.0) <= neutral_band:
            return {"signal": "NO_TRADE", "ema_diff": ema_diff, "rsi": rsi_val, "momentum": momentum}

        # ── Trend-strength gate — block weak-trend fallback entries ──────────
        min_diff = max(0.0, self.cfg.min_abs_ema_diff)
        if min_diff > 0.0 and abs(ema_diff) < min_diff:
            return {"signal": "NO_TRADE", "ema_diff": ema_diff, "rsi": rsi_val, "momentum": momentum}

        if not self.cfg.allow_fallback_vote:
            return {"signal": "NO_TRADE", "ema_diff": ema_diff, "rsi": rsi_val, "momentum": momentum}

        # ── Weighted vote fallback (only reached when trend IS present) ──────
        vote = 0
        vote += 1 if ema_diff >= 0 else -1
        vote += 1 if rsi_val >= 50 else -1
        vote += 1 if momentum >= 0 else -1
        signal: Signal = "CALL" if vote >= 0 else "PUT"
        return {"signal": signal, "ema_diff": ema_diff, "rsi": rsi_val, "momentum": momentum}
