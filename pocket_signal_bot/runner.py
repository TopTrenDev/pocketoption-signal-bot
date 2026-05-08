from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from pocket_signal_bot.adapters.api_adapter import PocketApiConfig, PocketOptionApiAdapter
from pocket_signal_bot.adapters.browser_adapter import BrowserConfig, PocketOptionBrowserAdapter
from pocket_signal_bot.config import BotConfig, validate_config
from pocket_signal_bot.logger import JsonEventLogger
from pocket_signal_bot.paper_simulator import PocketPaperSimulator
from pocket_signal_bot.risk import RiskConfig, RiskManager
from pocket_signal_bot.strategy import EmaRsiStrategy, StrategyConfig


class HybridRunner:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.logger = JsonEventLogger("logs/pocket_signal_events.jsonl", console=cfg.po_console_log)
        self.strategy = EmaRsiStrategy(
            StrategyConfig(
                ema_fast=cfg.ema_fast,
                ema_slow=cfg.ema_slow,
                rsi_period=cfg.rsi_period,
                buy_rsi_min=cfg.buy_rsi_min,
                sell_rsi_max=cfg.sell_rsi_max,
                rsi_neutral_band=cfg.rsi_neutral_band,
                min_ema_gap=cfg.min_ema_gap,
                require_momentum_confirm=cfg.require_momentum_confirm,
                min_abs_ema_diff=cfg.min_abs_ema_diff,
                allow_fallback_vote=cfg.allow_fallback_vote,
                min_trend_streak=cfg.min_trend_streak,
                chop_lookback=cfg.chop_lookback,
                min_range_pct=cfg.min_range_pct,
            )
        )
        self.paper = PocketPaperSimulator()
        self.api = PocketOptionApiAdapter(
            PocketApiConfig(
                session=cfg.po_session,
                uid=cfg.po_uid,
                is_demo=cfg.api_is_demo,
                region=cfg.po_region,
            )
        )
        self.browser = PocketOptionBrowserAdapter(
            BrowserConfig(
                base_url=cfg.browser_base_url,
                is_real_account=(cfg.effective_mode == "live"),
                headless=cfg.po_headless,
                price_selectors=tuple(cfg.price_selector_list),
                payout_selectors=tuple(cfg.payout_selector_list),
                startup_wait_ms=max(1000, cfg.po_browser_startup_wait_sec * 1000),
                quote_asset=cfg.symbol,
                use_ws_quotes=cfg.po_use_ws_quotes,
                show_overlay=cfg.po_browser_overlay,
                ws_debug=cfg.po_ws_debug,
            )
        )
        self.risk = RiskManager(
            RiskConfig(
                min_payout_pct=cfg.min_payout_pct,
                max_signal_age_ms=cfg.max_signal_age_ms,
                max_consecutive_losses=cfg.max_consecutive_losses,
                max_trades_per_day=cfg.max_trades_per_day,
                daily_loss_stop_pct=cfg.daily_loss_stop_pct,
            ),
            start_balance=1000.0,
        )
        self.balance = 1000.0
        self._paper_price = 1.0800
        self._api_candles_disabled = False
        self._api_candles_fail_count = 0
        self._api_connected = False
        self._browser_connected = False
        # Signal confirmation / flip-cooldown state
        self._prev_raw_signal: str | None = None
        self._flip_cooldown_until: float = 0.0
        self._confirm_streak_side: str | None = None
        self._confirm_streak_count: int = 0
        # Session stats (since process start): realized PnL from settled trades + win rate
        self._session_pnl: float = 0.0
        self._session_trades: int = 0
        # Wins/losses for stats: profit > 0 / profit < 0 (pushes excluded from win rate)
        self._session_wins: int = 0
        self._session_losses: int = 0
        self._session_pushes: int = 0
        self._volatile_pause_until: float = 0.0

    @staticmethod
    def _normalize_settled_pnl(result: dict[str, Any], trade_amount: float, payout_pct: float) -> float:
        """Single realized P&L number per trade (stake loss = negative, win = payout on stake, push = 0)."""
        amt = float(trade_amount)
        pay = float(payout_pct)
        status = str(result.get("result", "") or "").lower()
        if status == "draw":
            return 0.0
        raw = result.get("pnl")
        if raw is not None and isinstance(raw, (int, float)):
            pnl = float(raw)
            # Some paths report profit=0 on a loss; infer full stake loss.
            if pnl == 0.0 and status == "loss":
                return -amt
            return pnl
        won = bool(result.get("won", False))
        if won:
            return amt * (pay / 100.0)
        return -amt

    def _record_session_trade(self, *, pnl: float) -> dict[str, Any]:
        pnl = float(pnl)
        self._session_pnl += pnl
        self._session_trades += 1
        if pnl > 0:
            self._session_wins += 1
        elif pnl < 0:
            self._session_losses += 1
        else:
            self._session_pushes += 1
        decided = self._session_wins + self._session_losses
        wr: float | None
        if decided > 0:
            wr = round((self._session_wins / decided) * 100.0, 2)
        else:
            wr = None
        return {
            "session_pnl": round(self._session_pnl, 4),
            "session_trades": self._session_trades,
            "session_wins": self._session_wins,
            "session_losses": self._session_losses,
            "session_pushes": self._session_pushes,
            "win_rate_pct": wr,
        }

    def _session_overlay_line(self) -> str:
        n = self._session_trades
        if n == 0:
            return "Session PnL: +0.00  |  trades: 0  |  win rate: —"
        decided = self._session_wins + self._session_losses
        wr_s = f"{self._session_wins / decided * 100:.1f}%" if decided else "—"
        return (
            f"Session PnL: {self._session_pnl:+.2f}  |  trades: {n}  "
            f"(W{self._session_wins}/L{self._session_losses}/P{self._session_pushes})  |  win rate: {wr_s}"
        )

    @staticmethod
    def _range_pct(close_prices: list[float], lookback: int) -> float:
        if len(close_prices) < 2:
            return 0.0
        lb = max(2, min(len(close_prices), lookback))
        recent = close_prices[-lb:]
        hi = max(recent)
        lo = min(recent)
        base = abs(recent[-1]) if recent[-1] != 0 else 1.0
        return ((hi - lo) / base) * 100.0

    def _choose_trade_amount(self, signal: str, signal_info: dict[str, Any], closes: list[float]) -> dict[str, Any]:
        base = max(0.0, float(self.cfg.trade_amount))
        if signal not in ("CALL", "PUT") or not self.cfg.dynamic_size_enabled:
            return {"amount": base, "mult": 1.0, "market_state": "normal", "volatile": False}

        range_pct = self._range_pct(closes, int(self.cfg.volatility_lookback))
        is_volatile = range_pct >= float(self.cfg.volatility_range_pct)
        action = (self.cfg.volatile_action or "base").strip().lower()
        if action not in ("base", "pause"):
            action = "base"

        if is_volatile and action == "pause":
            now = time.time()
            self._volatile_pause_until = max(self._volatile_pause_until, now + float(self.cfg.volatile_pause_sec))
            return {
                "amount": base,
                "mult": 1.0,
                "market_state": "volatile_pause",
                "volatile": True,
                "range_pct": round(range_pct, 5),
            }
        if is_volatile:
            return {
                "amount": base,
                "mult": 1.0,
                "market_state": "volatile_base",
                "volatile": True,
                "range_pct": round(range_pct, 5),
            }

        ema_diff = abs(float(signal_info.get("ema_diff", 0.0)))
        rsi = float(signal_info.get("rsi", 50.0))
        momentum = float(signal_info.get("momentum", 0.0))
        rsi_bias = abs(rsi - 50.0)
        dir_ok = (signal == "CALL" and momentum >= 0.0) or (signal == "PUT" and momentum <= 0.0)
        strong = (
            ema_diff >= float(self.cfg.strong_trend_ema_diff_min)
            and rsi_bias >= float(self.cfg.strong_trend_rsi_bias_min)
            and abs(momentum) >= float(self.cfg.strong_trend_momentum_min)
            and dir_ok
        )
        if not strong:
            return {
                "amount": base,
                "mult": 1.0,
                "market_state": "normal",
                "volatile": False,
                "range_pct": round(range_pct, 5),
            }

        score = 1
        if ema_diff >= float(self.cfg.strong_trend_ema_diff_min) * 1.8:
            score += 1
        if rsi_bias >= float(self.cfg.strong_trend_rsi_bias_min) * 1.4:
            score += 1
        if abs(momentum) >= float(self.cfg.strong_trend_momentum_min) * 1.8:
            score += 1
        max_mult = max(1.0, float(self.cfg.strong_trend_max_mult))
        mult = min(max_mult, float(1 + score))
        amount = base * mult
        return {
            "amount": round(amount, 8),
            "mult": round(mult, 3),
            "market_state": "strong_trend",
            "volatile": False,
            "range_pct": round(range_pct, 5),
        }

    # ── Signal post-processing ───────────────────────────────────────────────

    def _finalize_signal(self, raw: str) -> tuple[str, dict[str, Any]]:
        """Apply flip-cooldown and consecutive-poll confirmation on top of strategy output."""
        meta: dict[str, Any] = {"raw_signal": raw}
        now = time.time()

        if raw in ("CALL", "PUT"):
            if (
                self.cfg.flip_cooldown_sec > 0
                and self._prev_raw_signal in ("CALL", "PUT")
                and self._prev_raw_signal != raw
            ):
                self._flip_cooldown_until = now + float(self.cfg.flip_cooldown_sec)
            self._prev_raw_signal = raw

            if raw == self._confirm_streak_side:
                self._confirm_streak_count += 1
            else:
                self._confirm_streak_side = raw
                self._confirm_streak_count = 1

            need = self.cfg.signal_confirm_polls
            confirmed: str = raw if self._confirm_streak_count >= need else "NO_TRADE"
            meta["confirm_streak"] = self._confirm_streak_count
            meta["confirm_need"] = need
        else:
            self._confirm_streak_side = None
            self._confirm_streak_count = 0
            confirmed = "NO_TRADE"
            meta["confirm_streak"] = 0
            meta["confirm_need"] = self.cfg.signal_confirm_polls

        meta["flip_cooldown_until"] = self._flip_cooldown_until
        if confirmed in ("CALL", "PUT") and now < self._flip_cooldown_until:
            meta["flip_cooldown_active"] = True
            return "NO_TRADE", meta
        meta["flip_cooldown_active"] = now < self._flip_cooldown_until
        return confirmed, meta

    # ── Browser overlay ──────────────────────────────────────────────────────

    async def _refresh_browser_overlay(
        self,
        *,
        signal: str,
        payout: float,
        trade_amount: float,
        can_trade: bool,
        reason: str,
        candles_adapter: str,
        last_close: float | None,
        extra: str = "",
    ) -> None:
        if not self.cfg.requires_broker or not self.cfg.po_browser_overlay:
            return
        lq = getattr(self.browser, "last_ws_quote", None)
        lines = [
            f"PocketOption bot  |  {self.cfg.effective_mode.upper()}",
            f"Symbol: {self.cfg.symbol}  |  amount: {trade_amount}",
            f"Signal: {signal}  |  payout: {payout}%",
            f"Trade allowed: {'YES' if can_trade else 'NO'} ({reason})",
            f"Candles source: {candles_adapter}",
            f"Last close (series): {last_close if last_close is not None else '—'}",
            f"Last WS quote: {lq if lq is not None else '—'}",
            f"Balance (bot): {self.balance:.2f}",
            self._session_overlay_line(),
        ]
        if extra:
            lines.append(extra)
        try:
            await self.browser.show_status_overlay("\n".join(lines))
        except Exception:
            pass

    # ── Paper candle generator ───────────────────────────────────────────────

    def _paper_candles(self) -> list[dict[str, Any]]:
        candles: list[dict[str, Any]] = []
        price = self._paper_price
        for i in range(self.cfg.candle_count):
            drift = 0.00002 if i % 7 < 4 else -0.00001
            price = max(0.5, price + drift)
            candles.append({"close": round(price, 5)})
        self._paper_price = price
        return candles

    # ── Adapter connect / disconnect ─────────────────────────────────────────

    async def _safe_connect(self) -> None:
        if not self.cfg.requires_broker:
            return
        timeout = float(self.cfg.connect_timeout_sec)
        if self.cfg.skip_api_connect:
            print(
                "pocket_signal_bot: PO_SKIP_API_CONNECT=true — skipping API (browser-only mode).",
                flush=True,
            )
        else:
            print(f"pocket_signal_bot: connecting API (max {timeout:g}s)…", flush=True)
            try:
                await asyncio.wait_for(self.api.connect(), timeout=timeout)
                self._api_connected = True
                self.logger.log("adapter_connect", adapter="api", ok=True)
            except asyncio.TimeoutError:
                self._api_connected = False
                self.logger.log(
                    "adapter_connect",
                    adapter="api",
                    ok=False,
                    error=f"timeout after {timeout}s — set PO_SKIP_API_CONNECT=true to skip",
                )
                try:
                    await asyncio.wait_for(self.api.disconnect(), timeout=5.0)
                except Exception:
                    pass
            except Exception as e:
                self._api_connected = False
                self.logger.log("adapter_connect", adapter="api", ok=False, error=str(e))

        print(f"pocket_signal_bot: launching Chromium (max {timeout:g}s)…", flush=True)
        try:
            await asyncio.wait_for(self.browser.connect(), timeout=timeout)
            self._browser_connected = True
            self.logger.log("adapter_connect", adapter="browser", ok=True)
        except asyncio.TimeoutError:
            self._browser_connected = False
            self.logger.log(
                "adapter_connect",
                adapter="browser",
                ok=False,
                error=f"timeout after {timeout}s",
            )
        except Exception as e:
            self._browser_connected = False
            self.logger.log("adapter_connect", adapter="browser", ok=False, error=str(e))

        bal_timeout = min(15.0, max(5.0, timeout / 4))
        print(f"pocket_signal_bot: reading balance (max {bal_timeout:g}s)…", flush=True)
        for adapter_name, adapter in (("api", self.api), ("browser", self.browser)):
            try:
                bal = await asyncio.wait_for(adapter.get_balance(), timeout=bal_timeout)
                if bal > 0:
                    self.balance = bal
                    self.risk.day_start_balance = bal
                    self.logger.log("balance_init", adapter=adapter_name, balance=bal)
                    break
            except Exception:
                continue
        print("pocket_signal_bot: connect phase done — entering main loop.", flush=True)

    async def _safe_disconnect(self) -> None:
        for adapter_name, adapter in (("api", self.api), ("browser", self.browser)):
            try:
                await adapter.disconnect()
                if adapter_name == "api":
                    self._api_connected = False
                else:
                    self._browser_connected = False
                self.logger.log("adapter_disconnect", adapter=adapter_name, ok=True)
            except Exception as e:
                self.logger.log("adapter_disconnect", adapter=adapter_name, ok=False, error=str(e))

    # ── Data helpers ─────────────────────────────────────────────────────────

    async def _api_call(self, coro: Any, *, what: str) -> Any:
        """Wrap any SDK awaitable with a hard timeout so a stuck call can't freeze the loop."""
        try:
            return await asyncio.wait_for(coro, timeout=float(self.cfg.data_timeout_sec))
        except asyncio.TimeoutError as e:
            self.logger.log("api_timeout", what=what, sec=self.cfg.data_timeout_sec)
            raise RuntimeError(f"API {what} timed out after {self.cfg.data_timeout_sec}s") from e

    async def _get_candles_with_failover(self) -> tuple[list[dict[str, Any]], str]:
        if self.cfg.effective_mode == "paper":
            return self._paper_candles(), "paper"
        if self._api_connected and not self._api_candles_disabled:
            try:
                candles = await self._api_call(
                    self.api.get_candles(self.cfg.symbol, self.cfg.timeframe_sec, self.cfg.candle_count),
                    what="get_candles",
                )
                self._api_candles_fail_count = 0
                return candles, "api"
            except Exception as e:
                self._api_connected = False
                self._api_candles_fail_count += 1
                self.logger.log("data_failover", from_adapter="api", to_adapter="browser", error=str(e))
                if self._api_candles_fail_count >= 3:
                    self._api_candles_disabled = True
                    self.logger.log(
                        "api_candles_disabled",
                        reason="repeated_api_candle_failures",
                        fail_count=self._api_candles_fail_count,
                    )
        try:
            browser_cap = float(self.cfg.data_timeout_sec) + 50.0
            candles = await asyncio.wait_for(
                self.browser.get_candles(self.cfg.symbol, self.cfg.timeframe_sec, self.cfg.candle_count),
                timeout=browser_cap,
            )
            return candles, "browser"
        except Exception as e2:
            self.logger.log("data_error", adapter="browser", error=str(e2))
            return [], "error"

    async def _get_payout_with_failover(self) -> tuple[float, str]:
        if self.cfg.effective_mode == "paper":
            return max(self.cfg.min_payout_pct, 80.0), "paper"
        if self._api_connected:
            try:
                payout = await self._api_call(self.api.get_payout_pct(self.cfg.symbol), what="get_payout_pct")
                return payout, "api"
            except Exception as e:
                self._api_connected = False
                self.logger.log("payout_failover", from_adapter="api", to_adapter="browser", error=str(e))
        try:
            payout = await self.browser.get_payout_pct(self.cfg.symbol)
            return payout, "browser"
        except Exception as e2:
            self.logger.log("payout_error", adapter="browser", error=str(e2))
            return max(self.cfg.min_payout_pct, 80.0), "fallback"

    async def _place_with_failover(self, direction: str, amount: float) -> tuple[str, str]:
        if not self.cfg.requires_broker:
            return "paper-order", "paper"
        if self._api_connected:
            try:
                order_id = await self._api_call(
                    self.api.place_order(self.cfg.symbol, amount, direction, self.cfg.expiry_sec),
                    what="place_order",
                )
                return order_id, "api"
            except Exception as e:
                self._api_connected = False
                self.logger.log("order_failover", from_adapter="api", to_adapter="browser", error=str(e))
        order_id = await self.browser.place_order(
            self.cfg.symbol, amount, direction, self.cfg.expiry_sec
        )
        return order_id, "browser"

    async def _refresh_balance(self) -> None:
        """Fetch live balance from broker after each trade so risk math stays accurate."""
        bal_timeout = 10.0
        for adapter, name in ((self.browser, "browser"), (self.api, "api")):
            try:
                bal = await asyncio.wait_for(adapter.get_balance(), timeout=bal_timeout)
                if bal > 0:
                    self.balance = bal
                    return
            except Exception:
                continue

    # ── Main loop ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.logger.log(
            "startup",
            effective_mode=self.cfg.effective_mode,
            api_is_demo=self.cfg.api_is_demo,
            requires_broker=self.cfg.requires_broker,
        )
        await self._safe_connect()
        if self.cfg.requires_broker and self.cfg.po_browser_overlay:
            await self._refresh_browser_overlay(
                signal="—",
                payout=0.0,
                trade_amount=self.cfg.trade_amount,
                can_trade=False,
                reason="starting",
                candles_adapter="—",
                last_close=None,
                extra="Log in if needed. WS quotes will populate shortly.",
            )
        try:
            while True:
                candles, candles_adapter = await self._get_candles_with_failover()
                if not candles:
                    self.logger.log("no_candles", adapter=candles_adapter)
                    await self._refresh_browser_overlay(
                        signal="NO_TRADE",
                        payout=0.0,
                        trade_amount=self.cfg.trade_amount,
                        can_trade=False,
                        reason="no_candles",
                        candles_adapter=candles_adapter,
                        last_close=None,
                        extra="Waiting for candle data…",
                    )
                    await asyncio.sleep(self.cfg.poll_seconds)
                    continue

                # Safe close extraction — skip malformed candles
                closes: list[float] = []
                for c in candles:
                    try:
                        closes.append(float(c["close"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                if not closes:
                    await asyncio.sleep(self.cfg.poll_seconds)
                    continue

                signal_info = self.strategy.generate_details(closes)
                raw_signal = str(signal_info["signal"])
                signal, sig_meta = self._finalize_signal(raw_signal)

                # signal_ts measured AFTER data fetch — reflects true freshness for risk gate
                signal_ts = datetime.now(timezone.utc)
                payout, payout_adapter = await self._get_payout_with_failover()
                sizing = self._choose_trade_amount(signal, signal_info, closes)
                trade_amount = float(sizing["amount"])
                signal_age_ms = int((datetime.now(timezone.utc) - signal_ts).total_seconds() * 1000)
                can_trade, reason = self.risk.can_trade(
                    payout_pct=payout,
                    signal_age_ms=signal_age_ms,
                    current_balance=self.balance,
                )
                now_ts = time.time()
                if now_ts < self._volatile_pause_until:
                    can_trade = False
                    reason = "volatility_pause"
                self.logger.log(
                    "signal",
                    signal=signal,
                    raw_signal=sig_meta.get("raw_signal"),
                    confirm_streak=sig_meta.get("confirm_streak"),
                    confirm_need=sig_meta.get("confirm_need"),
                    flip_cooldown_active=sig_meta.get("flip_cooldown_active"),
                    payout_pct=payout,
                    can_trade=can_trade,
                    reason=reason,
                    candles_adapter=candles_adapter,
                    payout_adapter=payout_adapter,
                    ema_diff=round(float(signal_info.get("ema_diff", 0.0)), 8),
                    rsi=round(float(signal_info.get("rsi", 50.0)), 4),
                    momentum=round(float(signal_info.get("momentum", 0.0)), 8),
                    amount=trade_amount,
                    amount_mult=sizing.get("mult", 1.0),
                    market_state=sizing.get("market_state", "normal"),
                    range_pct=sizing.get("range_pct"),
                )
                await self._refresh_browser_overlay(
                    signal=signal,
                    payout=payout,
                    trade_amount=trade_amount,
                    can_trade=can_trade,
                    reason=reason,
                    candles_adapter=candles_adapter,
                    last_close=closes[-1],
                    extra=(
                        f"State={sizing.get('market_state')} | "
                        f"mult={sizing.get('mult', 1.0)}x | "
                        f"range={sizing.get('range_pct', 0.0)}%"
                    ),
                )

                if signal in {"CALL", "PUT"} and can_trade:
                    try:
                        order_id, adapter_used = await self._place_with_failover(signal, trade_amount)
                    except Exception as e:
                        self.logger.log("order_error", signal=signal, error=str(e))
                        await asyncio.sleep(self.cfg.poll_seconds)
                        continue

                    send_ts = datetime.now(timezone.utc)
                    self.logger.log(
                        "order_sent",
                        order_id=order_id,
                        adapter=adapter_used,
                        signal=signal,
                        amount=trade_amount,
                        amount_mult=sizing.get("mult", 1.0),
                        market_state=sizing.get("market_state", "normal"),
                    )
                    await self._refresh_browser_overlay(
                        signal=signal,
                        payout=payout,
                        trade_amount=trade_amount,
                        can_trade=True,
                        reason="order_sent",
                        candles_adapter=candles_adapter,
                        last_close=closes[-1],
                        extra=f"ORDER → {adapter_used} id={order_id} amount={trade_amount}",
                    )

                    if self.cfg.effective_mode == "paper":
                        entry = closes[-1]
                        await asyncio.sleep(self.cfg.expiry_sec)
                        candles2, _ = await self._get_candles_with_failover()
                        if not candles2:
                            await asyncio.sleep(self.cfg.poll_seconds)
                            continue
                        exit_price = float(candles2[-1]["close"])
                        result = self.paper.settle(
                            direction=signal,
                            amount=trade_amount,
                            payout_pct=payout,
                            entry_price=entry,
                            exit_price=exit_price,
                        )
                        pnl = float(result.pnl)
                        self.balance += pnl
                        self.risk.register_result(pnl >= 0)
                        sess = self._record_session_trade(pnl=pnl)
                        self.logger.log(
                            "order_result",
                            mode="paper",
                            order_id=order_id,
                            won=pnl > 0,
                            push=pnl == 0,
                            pnl=round(pnl, 4),
                            balance=round(self.balance, 2),
                            signal_ts=signal_ts.isoformat(),
                            send_ts=send_ts.isoformat(),
                            result_ts=datetime.now(timezone.utc).isoformat(),
                            amount=trade_amount,
                            amount_mult=sizing.get("mult", 1.0),
                            market_state=sizing.get("market_state", "normal"),
                            **sess,
                        )
                        await self._refresh_browser_overlay(
                            signal=signal,
                            payout=payout,
                            trade_amount=trade_amount,
                            can_trade=True,
                            reason="order_done",
                            candles_adapter=candles_adapter,
                            last_close=closes[-1],
                            extra=f"RESULT outcome={'win' if pnl > 0 else 'loss' if pnl < 0 else 'push'} pnl={pnl:+.2f}",
                        )
                    else:
                        try:
                            if adapter_used == "api":
                                result = await self._api_call(
                                    self.api.check_result(order_id, self.cfg.expiry_sec),
                                    what="check_result",
                                )
                            else:
                                result = await self.browser.check_result(order_id, self.cfg.expiry_sec)
                        except Exception as e:
                            self.logger.log("result_error", order_id=order_id, error=str(e))
                            await asyncio.sleep(self.cfg.poll_seconds)
                            continue

                        pnl = self._normalize_settled_pnl(result, trade_amount, payout)
                        self.balance += float(pnl)
                        self.risk.register_result(float(pnl) >= 0)
                        sess = self._record_session_trade(pnl=float(pnl))
                        # Refresh live balance from broker to correct any drift
                        await self._refresh_balance()
                        self.logger.log(
                            "order_result",
                            mode=self.cfg.effective_mode,
                            order_id=order_id,
                            won=float(pnl) > 0,
                            push=float(pnl) == 0,
                            pnl=round(float(pnl), 4),
                            balance=round(self.balance, 2),
                            raw_result=result,
                            signal_ts=signal_ts.isoformat(),
                            send_ts=send_ts.isoformat(),
                            result_ts=datetime.now(timezone.utc).isoformat(),
                            amount=trade_amount,
                            amount_mult=sizing.get("mult", 1.0),
                            market_state=sizing.get("market_state", "normal"),
                            **sess,
                        )
                        await self._refresh_browser_overlay(
                            signal=signal,
                            payout=payout,
                            trade_amount=trade_amount,
                            can_trade=True,
                            reason="order_done",
                            candles_adapter=candles_adapter,
                            last_close=closes[-1],
                            extra=(
                                f"RESULT outcome={'win' if float(pnl) > 0 else 'loss' if float(pnl) < 0 else 'push'} "
                                f"pnl={float(pnl):+.2f}"
                            ),
                        )

                await asyncio.sleep(self.cfg.poll_seconds)
        finally:
            await self._safe_disconnect()


async def main() -> None:
    print("pocket_signal_bot: loading config…", flush=True)
    cfg = BotConfig()
    validate_config(cfg)
    print(
        f"pocket_signal_bot: starting in {cfg.effective_mode.upper()} mode "
        f"(connect timeout {cfg.connect_timeout_sec:g}s)…",
        flush=True,
    )
    runner = HybridRunner(cfg)
    await runner.run()


if __name__ == "__main__":
    print("pocket_signal_bot: launch", flush=True)
    asyncio.run(main())
