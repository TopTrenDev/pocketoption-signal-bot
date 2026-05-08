from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class RiskConfig:
    min_payout_pct: float
    max_signal_age_ms: int
    max_consecutive_losses: int
    max_trades_per_day: int
    daily_loss_stop_pct: float


class RiskManager:
    def __init__(self, cfg: RiskConfig, start_balance: float) -> None:
        self.cfg = cfg
        self.day = date.today()
        self.day_start_balance = start_balance
        self.consecutive_losses = 0
        self.trades_today = 0
        self.halted_reason = ""

    def _roll_day_if_needed(self, current_balance: float) -> None:
        today = date.today()
        if today != self.day:
            self.day = today
            self.day_start_balance = current_balance
            self.consecutive_losses = 0
            self.trades_today = 0
            self.halted_reason = ""

    def can_trade(self, payout_pct: float, signal_age_ms: int, current_balance: float) -> tuple[bool, str]:
        self._roll_day_if_needed(current_balance)

        pnl_pct = ((current_balance - self.day_start_balance) / self.day_start_balance) * 100 if self.day_start_balance > 0 else 0
        if pnl_pct <= -self.cfg.daily_loss_stop_pct:
            self.halted_reason = "daily_loss_stop"
            return False, self.halted_reason
        if self.trades_today >= self.cfg.max_trades_per_day:
            return False, "max_trades_per_day"
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return False, "max_consecutive_losses"
        if payout_pct < self.cfg.min_payout_pct:
            return False, "payout_below_min"
        if signal_age_ms > self.cfg.max_signal_age_ms:
            return False, "signal_too_old"
        return True, "ok"

    def register_result(self, won: bool) -> None:
        self.trades_today += 1
        if won:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1

