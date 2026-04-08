from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Optional

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


class EmaRsiStrategy:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def generate(self, close_prices: List[float]) -> Signal:
        min_len = max(self.cfg.ema_slow, self.cfg.rsi_period) + 2
        if len(close_prices) < min_len:
            return "NO_TRADE"

        f = ema(close_prices, self.cfg.ema_fast)
        s = ema(close_prices, self.cfg.ema_slow)
        r = rsi(close_prices, self.cfg.rsi_period)

        if f[-1] is None or s[-1] is None or r[-1] is None:
            return "NO_TRADE"
        if f[-1] > s[-1] and r[-1] >= self.cfg.buy_rsi_min:
            return "CALL"
        if f[-1] < s[-1] and r[-1] <= self.cfg.sell_rsi_max:
            return "PUT"
        return "NO_TRADE"

