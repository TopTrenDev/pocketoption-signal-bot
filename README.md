# PocketOption Signal Bot

Automated binary-options trading bot for [PocketOption](https://pocketoption.com) with an EMA + RSI signal engine, layered risk controls, and a dual-adapter execution stack (unofficial API with Playwright browser fallback).

[![Telegram](https://img.shields.io/badge/Telegram-@toptrendev_66-2CA5E0?style=for-the-badge&logo=telegram)](https://t.me/TopTrenDev_66)
[![Twitter](https://img.shields.io/badge/Twitter-@toptrendev-1DA1F2?style=for-the-badge&logo=x)](https://x.com/intent/follow?screen_name=toptrendev)
[![Gmail](https://img.shields.io/badge/Gmail-marekdvojak146%40gmail.com-D14836?style=for-the-badge&logo=gmail)](mailto:marekdvojak146@gmail.com)

---

## Features

| Area | Capabilities |
|------|----------------|
| **Signals** | EMA crossover regime + RSI thresholds → `CALL`, `PUT`, or `NO_TRADE`; chop filter, trend streak, momentum gate, optional vote fallback |
| **Execution** | Primary unofficial `pocket-option` SDK; automatic failover to Playwright (candles, payout, orders, settlement) |
| **Quotes** | WebSocket quote parsing on the trading page (canvas-friendly); optional DOM selectors for price/payout |
| **Risk** | Minimum payout, signal freshness, daily loss stop, max trades per day, consecutive loss cap |
| **Safety** | `paper` / `demo` / `live` modes; live blocked unless `PO_LIVE_CONFIRMED=true` |
| **Observability** | JSONL event log, optional console output, in-browser status overlay |

---

## Requirements

- **Python** 3.10+ recommended
- **Dependencies:** `pip install -r requirements.txt`
- **Browser runtime** (demo/live or API fallback): `python -m playwright install chromium`
- **Demo/live credentials:** `PO_SESSION` and `PO_UID` from the PocketOption web auth flow

---

## Quick start

```bash
git clone <repository-url>
cd pocketoption-signal-bot
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env    # Windows
# cp .env.example .env    # Linux / macOS
```

**Paper mode** (no broker connection; synthetic candles):

```bash
# Windows
set PO_MODE=paper
python -m pocket_signal_bot.runner

# Linux / macOS
export PO_MODE=paper
python -m pocket_signal_bot.runner
```

**Demo mode** (PocketOption demo account):

```bash
set PO_MODE=demo
set PO_SESSION=<your_session_token>
set PO_UID=<your_uid>
python -m pocket_signal_bot.runner
```

Logs are written to `logs/pocket_signal_events.jsonl`. Configuration is loaded from environment variables and `.env` (via `python-dotenv`).

---

## Operating modes

| Mode | Broker | Purpose |
|------|--------|---------|
| `paper` | None | Strategy and risk testing with simulated candles and settlement |
| `demo` | PocketOption demo | End-to-end validation with real UI/API, no real money |
| `live` | PocketOption real | Production; requires `PO_LIVE_CONFIRMED=true` and `PO_IS_DEMO=false` |

Live startup is refused unless you explicitly set `PO_LIVE_CONFIRMED=true`. Treat live trading as high risk.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  HybridRunner (runner.py)                                   │
│  poll → candles → strategy → risk → order → settlement      │
└───────────────┬─────────────────────────────┬───────────────┘
                │                             │
        ┌───────▼────────┐            ┌───────▼────────┐
        │  API adapter   │  failover  │ Browser adapter │
        │  (SDK)         │◄──────────►│ (Playwright)    │
        └────────────────┘            └────────────────┘
```

| Module | Role |
|--------|------|
| `config.py` | Environment-driven `BotConfig` and startup validation |
| `strategy.py` | `EmaRsiStrategy` — signal generation and filters |
| `risk.py` | Pre-trade gates and daily counters |
| `paper_simulator.py` | Binary contract settlement in paper mode |
| `adapters/api_adapter.py` | Unofficial PocketOption SDK wrapper |
| `adapters/browser_adapter.py` | Chromium automation, WS quotes, UI orders |
| `logger.py` | Structured JSONL + optional console events |
| `runner.py` | Main async loop, failover, session stats |

---

## Configuration

Copy `.env.example` to `.env` and adjust. All settings use the `PO_` prefix.

### Essential

| Variable | Description |
|----------|-------------|
| `PO_MODE` | `paper` \| `demo` \| `live` |
| `PO_SESSION` | Session token (required for demo/live) |
| `PO_UID` | Account UID (required for demo/live) |
| `PO_SYMBOL` | Asset id, e.g. `EURUSD_otc` |
| `PO_TIMEFRAME_SEC` | Candle period (seconds) |
| `PO_EXPIRY_SEC` | Option expiry (seconds) |
| `PO_TRADE_AMOUNT` | Stake per trade |

### Strategy & signal quality

Tune via `PO_EMA_FAST`, `PO_EMA_SLOW`, `PO_RSI_PERIOD`, `PO_BUY_RSI_MIN`, `PO_SELL_RSI_MAX`, `PO_RSI_NEUTRAL_BAND`, `PO_MIN_TREND_STREAK`, `PO_CHOP_LOOKBACK`, `PO_MIN_RANGE_PCT`, `PO_SIGNAL_CONFIRM_POLLS`, `PO_FLIP_COOLDOWN_SEC`, and related flags in `.env.example`.

### Risk

| Variable | Default | Description |
|----------|---------|-------------|
| `PO_MIN_PAYOUT_PCT` | 70 | Skip trade if payout below threshold |
| `PO_MAX_SIGNAL_AGE_MS` | 1500 | Reject stale signals |
| `PO_MAX_CONSECUTIVE_LOSSES` | 3 | Halt after N losses in a row |
| `PO_MAX_TRADES_PER_DAY` | 20 | Daily trade cap |
| `PO_DAILY_LOSS_STOP_PCT` | 2 | Halt if daily drawdown exceeds % |

### Browser & adapters

| Variable | Description |
|----------|-------------|
| `PO_HEADLESS` | `false` recommended for first demo setup (login, debugging) |
| `PO_BROWSER_STARTUP_WAIT_SEC` | Wait after page load before expecting quotes |
| `PO_USE_WS_QUOTES` | Parse live prices from page WebSockets (default `true`) |
| `PO_PRICE_SELECTOR` | Optional CSS selector(s), `|` separated |
| `PO_SKIP_API_CONNECT` | Browser-only when API hangs |
| `PO_CONNECT_TIMEOUT_SEC` / `PO_DATA_TIMEOUT_SEC` | Prevent stuck connect/API calls |

See `.env.example` for the full list and inline comments.

---

## Troubleshooting (browser / quotes)

If candle or price reads fail in demo/live:

1. Set `PO_HEADLESS=false` and increase `PO_BROWSER_STARTUP_WAIT_SEC` (e.g. `15` on slow machines).
2. Run `python -m pocket_signal_bot.runner` and log in manually if prompted; wait until the live quote is visible.
3. For DOM-based price: copy a CSS selector from DevTools → set `PO_PRICE_SELECTOR` (multiple selectors: separate with `|`).
4. For **canvas charts**, keep `PO_USE_WS_QUOTES=true` (default). The browser adapter ingests WebSocket frames (e.g. `update_quotes`) for `PO_SYMBOL` without a DOM price element.

Transient read failures log `data_error` and retry on the next poll; the process does not exit on a single failure.

---

## Logging

Each event is appended as one JSON line to `logs/pocket_signal_events.jsonl`, including:

- `startup`, `adapter_connect`, `signal`, `order_sent`, `order_result`
- `data_failover`, `data_error`, `no_candles`

Enable human-readable terminal lines with `PO_CONSOLE_LOG=true`. In demo/live with a visible browser, `PO_BROWSER_OVERLAY=true` shows a live status panel on the page.

---

## Pre-live checklist

Before enabling `PO_MODE=live`:

- [ ] 200+ demo trades recorded in JSONL logs
- [ ] Positive expectancy under realistic payout assumptions
- [ ] No recurring API disconnect / browser failover loops
- [ ] Latency and `PO_MAX_SIGNAL_AGE_MS` behavior verified
- [ ] Risk limits exercised (daily stop, max losses, max trades)