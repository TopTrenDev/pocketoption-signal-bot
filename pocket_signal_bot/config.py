from __future__ import annotations

from dataclasses import dataclass
import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class BotConfig:
    symbol: str = os.getenv("PO_SYMBOL", "EURUSD_otc")
    timeframe_sec: int = int(os.getenv("PO_TIMEFRAME_SEC", "60"))
    expiry_sec: int = int(os.getenv("PO_EXPIRY_SEC", "60"))
    candle_count: int = int(os.getenv("PO_CANDLE_COUNT", "300"))

    # Strategy
    ema_fast: int = int(os.getenv("PO_EMA_FAST", "20"))
    ema_slow: int = int(os.getenv("PO_EMA_SLOW", "50"))
    rsi_period: int = int(os.getenv("PO_RSI_PERIOD", "14"))
    buy_rsi_min: float = float(os.getenv("PO_BUY_RSI_MIN", "52"))
    sell_rsi_max: float = float(os.getenv("PO_SELL_RSI_MAX", "48"))

    # Risk controls
    trade_amount: float = float(os.getenv("PO_TRADE_AMOUNT", "1.0"))
    min_payout_pct: float = float(os.getenv("PO_MIN_PAYOUT_PCT", "70"))
    max_signal_age_ms: int = int(os.getenv("PO_MAX_SIGNAL_AGE_MS", "1500"))
    max_consecutive_losses: int = int(os.getenv("PO_MAX_CONSECUTIVE_LOSSES", "3"))
    max_trades_per_day: int = int(os.getenv("PO_MAX_TRADES_PER_DAY", "20"))
    daily_profit_stop_pct: float = float(os.getenv("PO_DAILY_PROFIT_STOP_PCT", "2"))
    daily_loss_stop_pct: float = float(os.getenv("PO_DAILY_LOSS_STOP_PCT", "2"))

    # Runtime mode: paper | demo | live
    mode: str = os.getenv("PO_MODE", "paper")
    po_live_confirmed: bool = _env_bool("PO_LIVE_CONFIRMED", False)
    adapter_priority: str = os.getenv("PO_ADAPTER_PRIORITY", "api_then_browser")
    poll_seconds: int = int(os.getenv("PO_POLL_SECONDS", "2"))

    # Browser cabinet URLs (locale may differ; adjust if your site uses /pt/ etc.)
    po_browser_url_demo: str = os.getenv(
        "PO_BROWSER_URL_DEMO",
        "https://pocketoption.com/en/cabinet/demo-quick-high-low/",
    )
    po_browser_url_live: str = os.getenv(
        "PO_BROWSER_URL_LIVE",
        "https://pocketoption.com/en/cabinet/quick-high-low/",
    )

    # Credentials / integration
    po_session: str = os.getenv("PO_SESSION", "")
    po_uid: str = os.getenv("PO_UID", "")
    po_is_demo: bool = _env_bool("PO_IS_DEMO", True)
    po_region: str = os.getenv("PO_REGION", "DEMO")

    # Browser fallback (Playwright): visible window helps login + finding selectors
    po_headless: bool = _env_bool("PO_HEADLESS", True)
    # One or more CSS selectors for the live quote number; use | between multiple tries
    po_price_selectors: str = os.getenv("PO_PRICE_SELECTOR", "")
    # Seconds to wait after opening cabinet URL (login / chart load)
    po_browser_startup_wait_sec: int = int(os.getenv("PO_BROWSER_STARTUP_WAIT_SEC", "5"))
    # When chart price is canvas-only, read quotes from page WebSocket (e.g. update_quotes)
    po_use_ws_quotes: bool = _env_bool("PO_USE_WS_QUOTES", True)

    @property
    def price_selector_list(self) -> list[str]:
        raw = (self.po_price_selectors or "").strip()
        if not raw:
            return []
        return [p.strip() for p in raw.split("|") if p.strip()]

    @property
    def effective_mode(self) -> str:
        m = (self.mode or "paper").strip().lower()
        if m not in ("paper", "demo", "live"):
            return "paper"
        return m

    @property
    def requires_broker(self) -> bool:
        return self.effective_mode in ("demo", "live")

    @property
    def api_is_demo(self) -> bool:
        """False for live mode (real account); otherwise follows PO_IS_DEMO."""
        if self.effective_mode == "live":
            return False
        return self.po_is_demo

    @property
    def browser_base_url(self) -> str:
        if self.effective_mode == "live":
            return self.po_browser_url_live
        return self.po_browser_url_demo


def validate_config(cfg: BotConfig) -> None:
    """Refuse unsafe live start unless explicitly confirmed."""
    if cfg.effective_mode == "live" and not cfg.po_live_confirmed:
        print(
            "Refusing to start: PO_MODE=live requires PO_LIVE_CONFIRMED=true (real money). "
            "Set it only if you accept full risk.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if cfg.requires_broker and (not cfg.po_session or not cfg.po_uid):
        print(
            "Refusing to start: PO_MODE=demo or live requires PO_SESSION and PO_UID.",
            file=sys.stderr,
        )
        raise SystemExit(2)

