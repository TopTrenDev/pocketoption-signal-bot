from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Direction = Literal["CALL", "PUT"]


@dataclass
class PaperTradeResult:
    won: bool
    pnl: float
    payout_pct: float
    entry_price: float
    exit_price: float


class PocketPaperSimulator:
    """
    PocketOption-like settlement semantics for binary contracts:
    - CALL wins when exit > entry
    - PUT wins when exit < entry
    - tie returns stake (pnl = 0)
    """

    def settle(
        self,
        direction: Direction,
        amount: float,
        payout_pct: float,
        entry_price: float,
        exit_price: float,
    ) -> PaperTradeResult:
        if direction == "CALL":
            won = exit_price > entry_price
            tie = exit_price == entry_price
        else:
            won = exit_price < entry_price
            tie = exit_price == entry_price

        if tie:
            pnl = 0.0
            # Treat tie as neither win nor loss — don't penalise the losing-streak counter.
            won = True
        elif won:
            pnl = amount * (payout_pct / 100.0)
        else:
            pnl = -amount

        return PaperTradeResult(
            won=won,
            pnl=pnl,
            payout_pct=payout_pct,
            entry_price=entry_price,
            exit_price=exit_price,
        )

