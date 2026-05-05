from __future__ import annotations

import asyncio
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
                daily_profit_stop_pct=cfg.daily_profit_stop_pct,
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

    async def _refresh_browser_overlay(
        self,
        *,
        signal: str,
        payout: float,
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
            f"Symbol: {self.cfg.symbol}  |  amount: {self.cfg.trade_amount}",
            f"Signal: {signal}  |  payout: {payout}%",
            f"Trade allowed: {'YES' if can_trade else 'NO'} ({reason})",
            f"Candles source: {candles_adapter}",
            f"Last close (series): {last_close if last_close is not None else '—'}",
            f"Last WS quote: {lq if lq is not None else '—'}",
            f"Balance (bot): {self.balance}",
        ]
        if extra:
            lines.append(extra)
        try:
            await self.browser.show_status_overlay("\n".join(lines))
        except Exception:
            pass

    def _paper_candles(self) -> list[dict[str, Any]]:
        candles: list[dict[str, Any]] = []
        price = self._paper_price
        for i in range(self.cfg.candle_count):
            drift = 0.00002 if i % 7 < 4 else -0.00001
            price = max(0.5, price + drift)
            candles.append({"close": round(price, 5)})
        self._paper_price = price
        return candles

    async def _safe_connect(self) -> None:
        if not self.cfg.requires_broker:
            return
        try:
            await self.api.connect()
            self._api_connected = True
            self.logger.log("adapter_connect", adapter="api", ok=True)
        except Exception as e:
            self._api_connected = False
            self.logger.log("adapter_connect", adapter="api", ok=False, error=str(e))
        try:
            await self.browser.connect()
            self._browser_connected = True
            self.logger.log("adapter_connect", adapter="browser", ok=True)
        except Exception as e:
            self._browser_connected = False
            self.logger.log("adapter_connect", adapter="browser", ok=False, error=str(e))
        # Initialize day balance from whichever adapter is available.
        for adapter_name, adapter in (("api", self.api), ("browser", self.browser)):
            try:
                bal = await adapter.get_balance()
                if bal > 0:
                    self.balance = bal
                    self.risk.day_start_balance = bal
                    self.logger.log("balance_init", adapter=adapter_name, balance=bal)
                    break
            except Exception:
                continue

    async def _safe_disconnect(self) -> None:
        for adapter_name, adapter in (("api", self.api), ("browser", self.browser)):
            try:
                await adapter.disconnect()
                if adapter_name == "api":
                    self._api_connected = False
                elif adapter_name == "browser":
                    self._browser_connected = False
                self.logger.log("adapter_disconnect", adapter=adapter_name, ok=True)
            except Exception as e:
                self.logger.log("adapter_disconnect", adapter=adapter_name, ok=False, error=str(e))

    async def _get_candles_with_failover(self) -> tuple[list[dict[str, Any]], str]:
        if self.cfg.effective_mode == "paper":
            return self._paper_candles(), "paper"
        if self._api_connected and not self._api_candles_disabled:
            try:
                candles = await self.api.get_candles(self.cfg.symbol, self.cfg.timeframe_sec, self.cfg.candle_count)
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
            candles = await self.browser.get_candles(self.cfg.symbol, self.cfg.timeframe_sec, self.cfg.candle_count)
            return candles, "browser"
        except Exception as e2:
            self.logger.log("data_error", adapter="browser", error=str(e2))
            return [], "error"

    async def _get_payout_with_failover(self) -> tuple[float, str]:
        if self.cfg.effective_mode == "paper":
            return max(self.cfg.min_payout_pct, 80.0), "paper"
        if self._api_connected:
            try:
                payout = await self.api.get_payout_pct(self.cfg.symbol)
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

    async def _place_with_failover(self, direction: str) -> tuple[str, str]:
        if not self.cfg.requires_broker:
            return "paper-order", "paper"
        if self._api_connected:
            try:
                order_id = await self.api.place_order(
                    self.cfg.symbol, self.cfg.trade_amount, direction, self.cfg.expiry_sec
                )
                return order_id, "api"
            except Exception as e:
                self._api_connected = False
                self.logger.log("order_failover", from_adapter="api", to_adapter="browser", error=str(e))
        order_id = await self.browser.place_order(self.cfg.symbol, self.cfg.trade_amount, direction, self.cfg.expiry_sec)
        return order_id, "browser"

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
                can_trade=False,
                reason="starting",
                candles_adapter="—",
                last_close=None,
                extra="Log in here if needed. WS quotes should populate shortly.",
            )
        try:
            while True:
                signal_ts = datetime.now(timezone.utc)
                candles, candles_adapter = await self._get_candles_with_failover()
                if not candles:
                    self.logger.log("no_candles", adapter=candles_adapter)
                    await self._refresh_browser_overlay(
                        signal="NO_TRADE",
                        payout=0.0,
                        can_trade=False,
                        reason="no_candles",
                        candles_adapter=candles_adapter,
                        last_close=None,
                        extra="Waiting for candle data (API or browser/WS)…",
                    )
                    await asyncio.sleep(self.cfg.poll_seconds)
                    continue
                closes = [float(c["close"]) for c in candles]
                signal_info = self.strategy.generate_details(closes)
                signal = str(signal_info["signal"])

                payout, payout_adapter = await self._get_payout_with_failover()
                signal_age_ms = int((datetime.now(timezone.utc) - signal_ts).total_seconds() * 1000)
                can_trade, reason = self.risk.can_trade(
                    payout_pct=payout,
                    signal_age_ms=signal_age_ms,
                    current_balance=self.balance,
                )
                self.logger.log(
                    "signal",
                    signal=signal,
                    payout_pct=payout,
                    can_trade=can_trade,
                    reason=reason,
                    candles_adapter=candles_adapter,
                    payout_adapter=payout_adapter,
                    ema_diff=round(float(signal_info.get("ema_diff", 0.0)), 8),
                    rsi=round(float(signal_info.get("rsi", 50.0)), 4),
                    momentum=round(float(signal_info.get("momentum", 0.0)), 8),
                )
                await self._refresh_browser_overlay(
                    signal=signal,
                    payout=payout,
                    can_trade=can_trade,
                    reason=reason,
                    candles_adapter=candles_adapter,
                    last_close=closes[-1] if closes else None,
                )

                if signal in {"CALL", "PUT"} and can_trade:
                    order_id, adapter_used = await self._place_with_failover(signal)
                    send_ts = datetime.now(timezone.utc)
                    self.logger.log("order_sent", order_id=order_id, adapter=adapter_used, signal=signal)
                    await self._refresh_browser_overlay(
                        signal=signal,
                        payout=payout,
                        can_trade=True,
                        reason="order_sent",
                        candles_adapter=candles_adapter,
                        last_close=closes[-1] if closes else None,
                        extra=f"ORDER → {adapter_used} id={order_id}",
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
                            amount=self.cfg.trade_amount,
                            payout_pct=payout,
                            entry_price=entry,
                            exit_price=exit_price,
                        )
                        self.balance += result.pnl
                        self.risk.register_result(result.won)
                        self.logger.log(
                            "order_result",
                            mode="paper",
                            order_id=order_id,
                            won=result.won,
                            pnl=result.pnl,
                            balance=self.balance,
                            signal_ts=signal_ts.isoformat(),
                            send_ts=send_ts.isoformat(),
                            result_ts=datetime.now(timezone.utc).isoformat(),
                        )
                    else:
                        result = await (self.api.check_result(order_id, self.cfg.expiry_sec) if adapter_used == "api" else self.browser.check_result(order_id, self.cfg.expiry_sec))
                        won = bool(result.get("won", False))
                        self.risk.register_result(won)
                        self.logger.log(
                            "order_result",
                            mode=self.cfg.effective_mode,
                            order_id=order_id,
                            won=won,
                            balance=self.balance,
                            raw_result=result,
                            signal_ts=signal_ts.isoformat(),
                            send_ts=send_ts.isoformat(),
                            result_ts=datetime.now(timezone.utc).isoformat(),
                        )
                        await self._refresh_browser_overlay(
                            signal=signal,
                            payout=payout,
                            can_trade=True,
                            reason="order_done",
                            candles_adapter=candles_adapter,
                            last_close=closes[-1] if closes else None,
                            extra=f"RESULT won={won} | {result}",
                        )

                await asyncio.sleep(self.cfg.poll_seconds)
        finally:
            await self._safe_disconnect()


async def main() -> None:
    cfg = BotConfig()
    validate_config(cfg)
    runner = HybridRunner(cfg)
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())

