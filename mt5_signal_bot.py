from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Literal, Optional
import time

try:
    import MetaTrader5 as mt5
except Exception:  # pragma: no cover - runtime dependency check
    mt5 = None

Signal = Literal["BUY", "SELL", "NO_TRADE"]


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
        current = values[i] * k + prev * (1 - k)
        out[i] = current
        prev = current
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
class BotConfig:
    symbol: str = "EURUSD"
    timeframe = mt5.TIMEFRAME_M5 if mt5 else 5
    candles_needed: int = 300
    lot: float = 0.01
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    buy_rsi_min: float = 52.0
    sell_rsi_max: float = 48.0
    max_spread_points: int = 25
    max_daily_loss_pct: float = 2.0
    max_daily_profit_pct: float = 2.0
    poll_seconds: int = 5
    execute_live: bool = False


class MT5SignalBot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.day_anchor = datetime.now().date()
        self.day_start_balance: Optional[float] = None

    def connect(self) -> None:
        if mt5 is None:
            raise RuntimeError("MetaTrader5 package is not installed.")
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        info = mt5.account_info()
        if info is None:
            raise RuntimeError("Cannot read MT5 account info.")
        self.day_start_balance = float(info.balance)
        print(f"[INIT] Connected. Balance={info.balance:.2f}")

    def shutdown(self) -> None:
        if mt5:
            mt5.shutdown()

    def _reset_day_if_needed(self) -> None:
        today = datetime.now().date()
        if today != self.day_anchor:
            self.day_anchor = today
            info = mt5.account_info()
            self.day_start_balance = float(info.balance) if info else self.day_start_balance
            print(f"[DAY] New day. Start balance={self.day_start_balance:.2f}")

    def _daily_stop_hit(self) -> bool:
        self._reset_day_if_needed()
        if self.day_start_balance is None:
            return False
        info = mt5.account_info()
        if info is None:
            return False
        pnl_pct = ((float(info.balance) - self.day_start_balance) / self.day_start_balance) * 100
        if pnl_pct <= -self.cfg.max_daily_loss_pct:
            print(f"[STOP] Daily loss reached: {pnl_pct:.2f}%")
            return True
        if pnl_pct >= self.cfg.max_daily_profit_pct:
            print(f"[STOP] Daily profit reached: {pnl_pct:.2f}%")
            return True
        return False

    def _spread_ok(self) -> bool:
        tick = mt5.symbol_info_tick(self.cfg.symbol)
        symbol = mt5.symbol_info(self.cfg.symbol)
        if tick is None or symbol is None or symbol.point <= 0:
            return False
        spread_points = int((tick.ask - tick.bid) / symbol.point)
        if spread_points > self.cfg.max_spread_points:
            print(f"[SKIP] Spread too high: {spread_points} points")
            return False
        return True

    def _has_position(self) -> bool:
        positions = mt5.positions_get(symbol=self.cfg.symbol)
        return bool(positions)

    def _get_closes(self) -> List[float]:
        rates = mt5.copy_rates_from_pos(self.cfg.symbol, self.cfg.timeframe, 0, self.cfg.candles_needed)
        if rates is None or len(rates) == 0:
            return []
        return [float(r["close"]) for r in rates]

    def generate_signal(self, closes: List[float]) -> Signal:
        if len(closes) < max(self.cfg.ema_slow, self.cfg.rsi_period) + 2:
            return "NO_TRADE"
        fast = ema(closes, self.cfg.ema_fast)
        slow = ema(closes, self.cfg.ema_slow)
        r = rsi(closes, self.cfg.rsi_period)
        if fast[-1] is None or slow[-1] is None or r[-1] is None:
            return "NO_TRADE"
        if fast[-1] > slow[-1] and r[-1] >= self.cfg.buy_rsi_min:
            return "BUY"
        if fast[-1] < slow[-1] and r[-1] <= self.cfg.sell_rsi_max:
            return "SELL"
        return "NO_TRADE"

    def _send_order(self, side: Signal) -> None:
        if side not in ("BUY", "SELL"):
            return
        tick = mt5.symbol_info_tick(self.cfg.symbol)
        if tick is None:
            return
        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side == "BUY" else tick.bid
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.cfg.symbol,
            "volume": self.cfg.lot,
            "type": order_type,
            "price": price,
            "deviation": 10,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
            "comment": "EMA_RSI_SIGNAL_BOT",
        }
        result = mt5.order_send(request)
        print(f"[ORDER] {side} -> retcode={getattr(result, 'retcode', None)}")

    def run(self) -> None:
        print(f"[RUN] {self.cfg.symbol} on timeframe={self.cfg.timeframe}")
        while True:
            try:
                if self._daily_stop_hit():
                    time.sleep(self.cfg.poll_seconds)
                    continue
                if not self._spread_ok():
                    time.sleep(self.cfg.poll_seconds)
                    continue
                closes = self._get_closes()
                signal = self.generate_signal(closes)
                print(f"[SIGNAL] {datetime.now().isoformat(timespec='seconds')} -> {signal}")
                if signal in ("BUY", "SELL") and not self._has_position():
                    if self.cfg.execute_live:
                        self._send_order(signal)
                    else:
                        print(f"[PAPER] Would place {signal} {self.cfg.symbol}")
                time.sleep(self.cfg.poll_seconds)
            except KeyboardInterrupt:
                print("[EXIT] Stopped by user.")
                break
            except Exception as e:
                print(f"[ERROR] {e}")
                time.sleep(self.cfg.poll_seconds)


if __name__ == "__main__":
    config = BotConfig(
        symbol="EURUSD",
        execute_live=False,  # Keep False for safety tests.
    )
    bot = MT5SignalBot(config)
    bot.connect()
    try:
        bot.run()
    finally:
        bot.shutdown()
