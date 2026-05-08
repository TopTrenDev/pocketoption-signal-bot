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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        print(f"[config] WARNING: {name}={raw!r} is not a valid integer; using default {default}", file=sys.stderr)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        print(f"[config] WARNING: {name}={raw!r} is not a valid number; using default {default}", file=sys.stderr)
        return default


@dataclass
class BotConfig:
    symbol: str = os.getenv("PO_SYMBOL", "EURUSD_otc")
    timeframe_sec: int = _env_int("PO_TIMEFRAME_SEC", 60)
    expiry_sec: int = _env_int("PO_EXPIRY_SEC", 60)
    candle_count: int = _env_int("PO_CANDLE_COUNT", 300)

    # Strategy
    ema_fast: int = _env_int("PO_EMA_FAST", 20)
    ema_slow: int = _env_int("PO_EMA_SLOW", 50)
    rsi_period: int = _env_int("PO_RSI_PERIOD", 14)
    buy_rsi_min: float = _env_float("PO_BUY_RSI_MIN", 52.0)
    sell_rsi_max: float = _env_float("PO_SELL_RSI_MAX", 48.0)
    rsi_neutral_band: float = _env_float("PO_RSI_NEUTRAL_BAND", 1.5)
    min_ema_gap: float = _env_float("PO_MIN_EMA_GAP", 0.0)
    require_momentum_confirm: bool = _env_bool("PO_REQUIRE_MOMENTUM_CONFIRM", True)
    # >0: require |fast_ema - slow_ema| >= this to allow the vote-fallback CALL/PUT (reduces chop)
    min_abs_ema_diff: float = _env_float("PO_MIN_ABS_EMA_DIFF", 0.0)
    # If false, strategy will never use vote fallback (strict EMA/RSI triggers only).
    allow_fallback_vote: bool = _env_bool("PO_ALLOW_FALLBACK_VOTE", True)
    # Require this many recent EMA-diff samples to keep same sign before any directional signal.
    min_trend_streak: int = max(1, _env_int("PO_MIN_TREND_STREAK", 2))
    # Chop filter lookback and minimum range-percent over that window.
    chop_lookback: int = max(5, _env_int("PO_CHOP_LOOKBACK", 20))
    min_range_pct: float = max(0.0, _env_float("PO_MIN_RANGE_PCT", 0.05))
    # Require this many consecutive polls with the same CALL/PUT before placing (1 = no confirmation)
    signal_confirm_polls: int = max(1, _env_int("PO_SIGNAL_CONFIRM_POLLS", 2))
    # After raw signal flips CALL↔PUT, skip directional trades for this many seconds (0 = off)
    flip_cooldown_sec: int = max(0, _env_int("PO_FLIP_COOLDOWN_SEC", 0))

    # Risk controls
    trade_amount: float = _env_float("PO_TRADE_AMOUNT", 1.0)
    min_payout_pct: float = _env_float("PO_MIN_PAYOUT_PCT", 70.0)
    max_signal_age_ms: int = _env_int("PO_MAX_SIGNAL_AGE_MS", 1500)
    max_consecutive_losses: int = _env_int("PO_MAX_CONSECUTIVE_LOSSES", 3)
    max_trades_per_day: int = _env_int("PO_MAX_TRADES_PER_DAY", 20)
    daily_loss_stop_pct: float = _env_float("PO_DAILY_LOSS_STOP_PCT", 2.0)
    # Dynamic sizing / volatility controls
    dynamic_size_enabled: bool = _env_bool("PO_DYNAMIC_SIZE_ENABLED", True)
    strong_trend_max_mult: float = max(1.0, _env_float("PO_STRONG_TREND_MAX_MULT", 5.0))
    strong_trend_ema_diff_min: float = max(0.0, _env_float("PO_STRONG_TREND_EMA_DIFF_MIN", 0.00003))
    strong_trend_rsi_bias_min: float = max(0.0, _env_float("PO_STRONG_TREND_RSI_BIAS_MIN", 6.0))
    strong_trend_momentum_min: float = max(0.0, _env_float("PO_STRONG_TREND_MOMENTUM_MIN", 0.00002))
    volatility_lookback: int = max(5, _env_int("PO_VOLATILITY_LOOKBACK", 12))
    volatility_range_pct: float = max(0.0, _env_float("PO_VOLATILITY_RANGE_PCT", 0.12))
    volatile_action: str = os.getenv("PO_VOLATILE_ACTION", "base")
    volatile_pause_sec: int = max(0, _env_int("PO_VOLATILE_PAUSE_SEC", 90))

    # Runtime mode: paper | demo | live
    mode: str = os.getenv("PO_MODE", "paper")
    po_live_confirmed: bool = _env_bool("PO_LIVE_CONFIRMED", False)
    adapter_priority: str = os.getenv("PO_ADAPTER_PRIORITY", "api_then_browser")
    poll_seconds: int = _env_int("PO_POLL_SECONDS", 2)

    # Per-adapter connect timeout (seconds). SDK / Playwright can hang; this limits damage.
    connect_timeout_sec: float = _env_float("PO_CONNECT_TIMEOUT_SEC", 120.0)
    # Per-call API timeout (candles, orders). Without this, one stuck SDK call freezes the bot.
    data_timeout_sec: float = _env_float("PO_DATA_TIMEOUT_SEC", 90.0)
    # true = skip pocket-option SDK entirely; rely only on Playwright (use when API hangs)
    skip_api_connect: bool = _env_bool("PO_SKIP_API_CONNECT", False)

    # Browser cabinet URLs
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

    # Browser (Playwright) options
    po_headless: bool = _env_bool("PO_HEADLESS", True)
    po_price_selectors: str = os.getenv("PO_PRICE_SELECTOR", "")
    po_payout_selectors: str = os.getenv("PO_PAYOUT_SELECTOR", "")
    po_browser_startup_wait_sec: int = _env_int("PO_BROWSER_STARTUP_WAIT_SEC", 5)
    po_use_ws_quotes: bool = _env_bool("PO_USE_WS_QUOTES", True)
    po_console_log: bool = _env_bool("PO_CONSOLE_LOG", True)
    po_browser_overlay: bool = _env_bool("PO_BROWSER_OVERLAY", True)
    po_ws_debug: bool = _env_bool("PO_WS_DEBUG", False)

    @property
    def price_selector_list(self) -> list[str]:
        raw = (self.po_price_selectors or "").strip()
        if not raw:
            return []
        return [p.strip() for p in raw.split("|") if p.strip()]

    @property
    def payout_selector_list(self) -> list[str]:
        raw = (self.po_payout_selectors or "").strip()
        if not raw:
            return []
        return [p.strip() for p in raw.split("|") if p.strip()]

    @property
    def effective_mode(self) -> str:
        m = (self.mode or "paper").strip().lower()
        if m not in ("paper", "demo", "live"):
            print(
                f"[config] WARNING: PO_MODE={self.mode!r} is not valid (paper|demo|live); running as paper.",
                file=sys.stderr,
            )
            return "paper"
        return m

    @property
    def requires_broker(self) -> bool:
        return self.effective_mode in ("demo", "live")

    @property
    def api_is_demo(self) -> bool:
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
