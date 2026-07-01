from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def format_time_pair(timeframe_sec: int, expiry_sec: int) -> str:
    """Human label for comparing setups, e.g. 15/15, M1/M1, M5/M3."""

    def _part(sec: int) -> str:
        if sec < 60:
            return str(sec)
        if sec % 60 == 0:
            return f"M{sec // 60}"
        return f"{sec}s"

    return f"{_part(timeframe_sec)}/{_part(expiry_sec)}"


class TradeStore:
    """Persist bot sessions and trades to JSON for charting and A/B tests."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = {"active_session": None, "sessions": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(raw, dict):
            self._data = raw
            if "sessions" not in self._data:
                self._data["sessions"] = []
            if "active_session" not in self._data:
                self._data["active_session"] = None

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=True), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _empty_summary() -> dict[str, Any]:
        return {
            "total_pnl": 0.0,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pushes": 0,
            "win_rate_pct": None,
        }

    def _recompute_summary(self, session: dict[str, Any]) -> None:
        trades: list[dict[str, Any]] = session.get("trades") or []
        pnl = 0.0
        wins = losses = pushes = 0
        for t in trades:
            v = float(t.get("pnl") or 0.0)
            pnl += v
            if v > 0:
                wins += 1
            elif v < 0:
                losses += 1
            else:
                pushes += 1
        decided = wins + losses
        session["summary"] = {
            "total_pnl": round(pnl, 4),
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "win_rate_pct": round(wins / decided * 100.0, 2) if decided else None,
        }

    def begin_session(self, config: dict[str, Any]) -> str:
        session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._data["active_session"] = {
            "session_id": session_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ended_at": None,
            "config": config,
            "trades": [],
            "summary": self._empty_summary(),
        }
        self._save()
        return session_id

    def record_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        session = self._data.get("active_session")
        if not session:
            raise RuntimeError("No active trade session — call begin_session() first")
        trades: list[dict[str, Any]] = session.setdefault("trades", [])
        pnl = float(trade.get("pnl") or 0.0)
        wins = sum(1 for t in trades if float(t.get("pnl") or 0.0) > 0)
        losses = sum(1 for t in trades if float(t.get("pnl") or 0.0) < 0)
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        decided = wins + losses
        cumulative = sum(float(t.get("pnl") or 0.0) for t in trades) + pnl
        row = {
            **trade,
            "trade_num": len(trades) + 1,
            "cumulative_pnl": round(cumulative, 4),
            "win_rate_pct": round(wins / decided * 100.0, 2) if decided else None,
        }
        trades.append(row)
        self._recompute_summary(session)
        self._save()
        return row

    def end_session(self) -> dict[str, Any] | None:
        session = self._data.get("active_session")
        if not session:
            return None
        session["ended_at"] = datetime.now(timezone.utc).isoformat()
        self._recompute_summary(session)
        self._data.setdefault("sessions", []).append(session)
        self._data["active_session"] = None
        self._save()
        return session

    def all_sessions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = list(self._data.get("sessions") or [])
        active = self._data.get("active_session")
        if active:
            out = out + [active]
        return out

    def active_session(self) -> dict[str, Any] | None:
        s = self._data.get("active_session")
        return s if isinstance(s, dict) else None
