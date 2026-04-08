from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Optional

Signal = Literal["CALL", "PUT", "NO_TRADE"]


def ema(values: List[float], period: int) -> List[Optional[float]]:
    """Return EMA list aligned with input values."""
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) < period:
        return [None] * len(values)

    result: List[Optional[float]] = [None] * len(values)
    k = 2 / (period + 1)

    # Seed EMA with SMA for the first valid index.
    seed = sum(values[:period]) / period
    result[period - 1] = seed

    prev = seed
    for i in range(period, len(values)):
        current = (values[i] * k) + (prev * (1 - k))
        result[i] = current
        prev = current
    return result


def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    """Return RSI list aligned with input values (Wilder smoothing)."""
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) <= period:
        return [None] * len(values)

    result: List[Optional[float]] = [None] * len(values)
    gains: List[float] = []
    losses: List[float] = []

    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0))
        losses.append(abs(min(delta, 0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - (100 / (1 + rs))

    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0)
        loss = abs(min(delta, 0))

        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - (100 / (1 + rs))

    return result


@dataclass
class StrategyConfig:
    ema_period: int = 50
    rsi_period: int = 14
    rsi_call_threshold: float = 50.0
    rsi_put_threshold: float = 50.0


class EmaRsiSignalBot:
    """
    Simple signal bot logic:
    - CALL when price > EMA and RSI > threshold
    - PUT when price < EMA and RSI < threshold
    - otherwise NO_TRADE
    """

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()

    def generate_signal(self, close_prices: List[float]) -> Signal:
        if len(close_prices) < max(self.config.ema_period, self.config.rsi_period) + 2:
            return "NO_TRADE"

        ema_values = ema(close_prices, self.config.ema_period)
        rsi_values = rsi(close_prices, self.config.rsi_period)

        last_price = close_prices[-1]
        last_ema = ema_values[-1]
        last_rsi = rsi_values[-1]

        if last_ema is None or last_rsi is None:
            return "NO_TRADE"

        if last_price > last_ema and last_rsi > self.config.rsi_call_threshold:
            return "CALL"

        if last_price < last_ema and last_rsi < self.config.rsi_put_threshold:
            return "PUT"

        return "NO_TRADE"


if __name__ == "__main__":
    # Example closes (replace with real EUR/USD close data from MT5 candles).
    sample_close = [
        1.0800, 1.0804, 1.0802, 1.0808, 1.0811, 1.0810, 1.0814, 1.0817, 1.0812, 1.0818,
        1.0821, 1.0820, 1.0822, 1.0826, 1.0824, 1.0829, 1.0831, 1.0834, 1.0830, 1.0836,
        1.0839, 1.0841, 1.0837, 1.0842, 1.0846, 1.0848, 1.0845, 1.0849, 1.0852, 1.0850,
        1.0854, 1.0857, 1.0855, 1.0859, 1.0862, 1.0860, 1.0864, 1.0867, 1.0865, 1.0868,
        1.0871, 1.0870, 1.0873, 1.0876, 1.0874, 1.0878, 1.0880, 1.0882, 1.0881, 1.0884,
        1.0887, 1.0886, 1.0889, 1.0891, 1.0890, 1.0893, 1.0895, 1.0894, 1.0897, 1.0900,
    ]

    bot = EmaRsiSignalBot()
    signal = bot.generate_signal(sample_close)
    print("Signal:", signal)
