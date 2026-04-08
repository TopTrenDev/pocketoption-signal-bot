# Hybrid Trading Bots (MT5 + PocketOption)

This workspace now contains two independent bot paths:

- `mt5_signal_bot.py`: MT5 EUR/USD signal bot
- `pocket_signal_bot/`: PocketOption hybrid signal bot (`paper` + `demo`)

## PocketOption Hybrid Signal Bot

The PocketOption bot is designed around:

- EMA + RSI signal engine (`CALL` / `PUT` / `NO_TRADE`)
- Risk controls (daily stops, max losses, max trades/day, min payout)
- Hybrid adapters:
  - Unofficial API adapter (primary)
  - Browser adapter fallback
- Structured event logs with timing fields

### Files

- `pocket_signal_bot/config.py`
- `pocket_signal_bot/strategy.py`
- `pocket_signal_bot/risk.py`
- `pocket_signal_bot/paper_simulator.py`
- `pocket_signal_bot/adapters/api_adapter.py`
- `pocket_signal_bot/adapters/browser_adapter.py`
- `pocket_signal_bot/runner.py`
- `pocket_signal_bot/logger.py`

## Install

```bash
pip install -r requirements.txt
```

If using browser fallback, also install browser runtime:

```bash
python -m playwright install chromium
```

### Demo mode: fix “Could not read current price from UI”

1. Set in `.env`: `PO_HEADLESS=false` and `PO_BROWSER_STARTUP_WAIT_SEC=15` (or higher on slow PCs).
2. Run `python -m pocket_signal_bot.runner`. A Chromium window opens on the quick-trading URL.
3. **Log in** if you see a login page; wait until the **live quote** is visible.
4. In normal Chrome on the same page: **F12 → Elements → select tool → click the price number → right‑click element → Copy → Copy selector**.
5. Put that string in `.env` as `PO_PRICE_SELECTOR=...` (use `|` between multiple selectors to try in order).
6. Run the bot again. If API still fails, browser candles will work once the selector matches.

The bot no longer exits the whole process when browser price read fails once; it logs `data_error` and retries on the next poll.

If the chart price is drawn on a **canvas** (no numeric `<span>`), keep **`PO_USE_WS_QUOTES=true`** (default). The browser adapter listens to the page **WebSocket** (e.g. `update_quotes`) and extracts the last price for **`PO_SYMBOL`** without needing `PO_PRICE_SELECTOR`.

## Environment file (optional)

Copy the sample and edit:

```bash
copy .env.example .env
```

`pocket_signal_bot/config.py` loads `.env` automatically when `python-dotenv` is installed.

## PocketOption credentials (demo first)

Set environment variables before running (or put them in `.env`):

- `PO_SESSION` -> session token from PocketOption web auth flow
- `PO_UID` -> your account uid
- `PO_IS_DEMO=true`
- `PO_MODE=paper` | `demo` | `live` (live = real money; requires `PO_LIVE_CONFIRMED=true`)
- Optional symbol/time controls:
  - `PO_SYMBOL=EURUSD_otc`
  - `PO_TIMEFRAME_SEC=60`
  - `PO_EXPIRY_SEC=60`

## Run

Paper mode (safe):

```bash
set PO_MODE=paper
python -m pocket_signal_bot.runner
```

Demo mode (PocketOption demo account):

```bash
set PO_MODE=demo
set PO_SESSION=your_session
set PO_UID=your_uid
python -m pocket_signal_bot.runner
```

Live mode (real money — same code path as demo, but `api_is_demo=false` and real cabinet URL for browser):

```bash
set PO_MODE=live
set PO_LIVE_CONFIRMED=true
set PO_SESSION=your_real_session
set PO_UID=your_real_uid
set PO_IS_DEMO=false
python -m pocket_signal_bot.runner
```

The bot refuses to start `PO_MODE=live` unless `PO_LIVE_CONFIRMED=true`.

Logs are written to `logs/pocket_signal_events.jsonl`.

## Readiness checklist before live

- At least 200+ demo trades logged
- Positive expectancy with realistic payout conditions
- No recurring adapter disconnect loop
- Latency and failover behavior verified in logs
- Risk limits tested (daily stop, max losses, max trades)

## Important

No strategy guarantees profit. Treat this as research/automation infrastructure and validate on demo first.
