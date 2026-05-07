from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonEventLogger:
    """Writes one JSON line per event to a file; optionally prints readable lines to the console."""

    def __init__(self, log_path: str, *, console: bool = True) -> None:
        self.path = Path(log_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.console = console

    def _print_console(self, event_type: str, payload: dict[str, Any]) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if event_type == "startup":
            print(
                f"[{ts}] START mode={payload.get('effective_mode')} "
                f"api_demo={payload.get('api_is_demo')} broker={payload.get('requires_broker')}",
                flush=True,
            )
        elif event_type == "signal":
            ok = payload.get("can_trade")
            print(
                f"[{ts}] SIGNAL {payload.get('signal')} | payout={payload.get('payout_pct')}% "
                f"| trade={'YES' if ok else 'NO'} ({payload.get('reason')}) "
                f"| candles={payload.get('candles_adapter')} payout_src={payload.get('payout_adapter')} "
                f"| ema_diff={payload.get('ema_diff')} rsi={payload.get('rsi')} momentum={payload.get('momentum')}",
                flush=True,
            )
        elif event_type == "order_sent":
            print(
                f"[{ts}] ORDER SENT {payload.get('signal')} adapter={payload.get('adapter')} id={payload.get('order_id')}",
                flush=True,
            )
        elif event_type == "order_result":
            st = int(payload.get("session_trades") or 0)
            spnl = payload.get("session_pnl")
            sw = payload.get("session_wins")
            sl = payload.get("session_losses")
            wr = payload.get("win_rate_pct")
            extra = ""
            if st > 0 and spnl is not None and sw is not None and sl is not None and wr is not None:
                extra = (
                    f" | session PnL={spnl:+.4f} trades={st} "
                    f"W/L={sw}/{sl} winrate={wr}%"
                )
            print(
                f"[{ts}] RESULT mode={payload.get('mode')} won={payload.get('won')} "
                f"pnl={payload.get('pnl', '—')} balance={payload.get('balance', '—')} "
                f"id={payload.get('order_id')}{extra}",
                flush=True,
            )
        elif event_type == "no_candles":
            print(f"[{ts}] NO CANDLES adapter={payload.get('adapter')} (waiting…)", flush=True)
        elif event_type == "data_error":
            err = str(payload.get("error", ""))[:120]
            print(f"[{ts}] DATA ERROR {payload.get('adapter')}: {err}", flush=True)
        elif event_type == "adapter_connect":
            ok = payload.get("ok")
            ad = payload.get("adapter")
            if ok:
                print(f"[{ts}] CONNECT OK {ad}", flush=True)
            else:
                err = str(payload.get("error", ""))[:100]
                print(f"[{ts}] CONNECT FAIL {ad}: {err}", flush=True)
        elif event_type == "balance_init":
            print(f"[{ts}] BALANCE {payload.get('adapter')} = {payload.get('balance')}", flush=True)
        else:
            short = {k: v for k, v in payload.items() if k != "raw_result"}
            print(f"[{ts}] {event_type} {short}", flush=True)

    def log(self, event_type: str, **payload: Any) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")
        if self.console:
            try:
                self._print_console(event_type, dict(payload))
            except Exception as e:
                print(f"[logger] console print error: {e}", file=sys.stderr, flush=True)
