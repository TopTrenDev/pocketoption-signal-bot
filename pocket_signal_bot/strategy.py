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
    rsi_neutral_band: float = 1.5
    min_ema_gap: float = 0.0
    require_momentum_confirm: bool = True


class EmaRsiStrategy:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def generate(self, close_prices: List[float]) -> Signal:
        return self.generate_details(close_prices)["signal"]

    def generate_details(self, close_prices: List[float]) -> dict[str, Any]:
        if len(close_prices) < 3:
            return {
                "signal": "NO_TRADE",
                "ema_diff": 0.0,
                "rsi": 50.0,
                "momentum": 0.0,
            }

        # Adapt to short history from broker/browser so signal engine becomes active quickly.
        available = len(close_prices)
        ema_slow_period = min(self.cfg.ema_slow, max(2, available - 1))
        ema_fast_period = min(self.cfg.ema_fast, max(1, ema_slow_period // 2))
        rsi_period = min(self.cfg.rsi_period, max(2, available - 1))

        f = ema(close_prices, ema_fast_period)
        s = ema(close_prices, ema_slow_period)
        r = rsi(close_prices, rsi_period)

        if f[-1] is None or s[-1] is None or r[-1] is None:
            return {
                "signal": "NO_TRADE",
                "ema_diff": 0.0,
                "rsi": 50.0,
                "momentum": 0.0,
            }

        ema_diff = float(f[-1] - s[-1])
        rsi_val = float(r[-1])
        momentum = float(close_prices[-1] - close_prices[-3]) if len(close_prices) >= 3 else 0.0

        signal: Signal
        # Primary strict triggers.
        if ema_diff > self.cfg.min_ema_gap and rsi_val >= self.cfg.buy_rsi_min:
            if not self.cfg.require_momentum_confirm or momentum >= 0:
                signal = "CALL"
                return {"signal": signal, "ema_diff": ema_diff, "rsi": rsi_val, "momentum": momentum}
        if ema_diff < -self.cfg.min_ema_gap and rsi_val <= self.cfg.sell_rsi_max:
            if not self.cfg.require_momentum_confirm or momentum <= 0:
                signal = "PUT"
                return {"signal": signal, "ema_diff": ema_diff, "rsi": rsi_val, "momentum": momentum}

        # Weighted fallback to keep bot active in demo: trend + momentum vote.
        vote = 0
        vote += 1 if ema_diff >= 0 else -1
        vote += 1 if rsi_val >= 50 else -1
        vote += 1 if momentum >= 0 else -1
        signal = "CALL" if vote >= 0 else "PUT"
        return {"signal": signal, "ema_diff": ema_diff, "rsi": rsi_val, "momentum": momentum}

