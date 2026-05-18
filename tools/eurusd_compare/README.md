# EUR/USD Feed Compare

This folder contains a standalone test tool for comparing:

- Pocket Option browser websocket quotes
- an external EUR/USD FX API feed

The goal is to measure:

- local receive-time skew between feeds
- price difference in pips
- external feed transport lag if the provider exposes timestamps

## What the script supports now

Implemented provider:

- **Alpha Vantage** [FX — `CURRENCY_EXCHANGE_RATE`](https://www.alphavantage.co/documentation/#fx) polled bid/ask (`--fx-provider alphavantage`, **default**)
- `Polygon / Massive` forex websocket quotes (`--fx-provider polygon`)
- `Polygon / Massive` [currency conversion REST](https://massive.com/docs/rest/forex/currency-conversion) polled mid/bid/ask (`--fx-provider polygon_rest`)
- `OANDA` pricing stream (`EUR_USD`)

Pocket Option side:

- uses the existing Playwright/browser websocket hook already used by this repo
- captures the local receive timestamp when a Pocket Option quote is parsed

## Important limitation

This script can compare:

- Pocket Option local websocket receive time
- external feed local receive time
- Pocket Option price vs external mid price

It does **not** guarantee true upstream Pocket Option latency unless Pocket Option frames expose a reliable source timestamp for the quote. The current parser records local receipt time, which is still useful for practical feed comparison.

## Realistic EUR/USD API options

If you want a "real market" comparison feed, these are reasonable choices:

- `Alpha Vantage`
  - free key; [realtime FX](https://www.alphavantage.co/documentation/#fx) via `CURRENCY_EXCHANGE_RATE` (polled, not streamed)
  - respect free-tier rate limits (~5 calls/min); this tool defaults to ~12.5s between requests

- `OANDA`
  - good practical broker-style EUR/USD stream
  - documented pricing stream API
  - this script supports it now

- `Polygon / Massive`
  - real-time forex bid/ask websocket
  - good if you already have a paid market-data plan

- `Twelve Data`
  - easier retail API
  - offers forex websocket access
  - useful for simpler testing, but not ideal if you want the closest thing to a broker stream

- specialist FX data vendors like `iTick`, `FXWebAPI`, `TickDB`
  - can be lower latency / more institutional
  - usually require separate commercial onboarding

For quick comparisons without a paid market-data plan, **Alpha Vantage** is the default. Use **Polygon** when you have Starter+ and care about streaming latency, or **OANDA** for a broker-style stream.

## Setup

You need:

1. Pocket Option login access in the opened browser page
2. an external feed credential, depending on provider:
   - **Alpha Vantage** API key ([get a key](https://www.alphavantage.co/support/#api-key)), or
   - **Polygon** API key, or
   - **OANDA** account id and API token

Environment variables the script reads:

- **Dotenv load order** (later files override earlier): repository root `.env`, then if present `tools/.env`, then `tools/eurusd_compare/.env`. Put `ALPHAVANTAGE_API_KEY` or `POLYGON_API_KEY` there if you like.
- `FX_PROVIDER`
  - `alphavantage` (default), `polygon`, `polygon_rest`, or `oanda`
- `ALPHAVANTAGE_API_KEY`
- `ALPHAVANTAGE_INTERVAL_SEC`
  - default: `12.5` (free tier ~5 requests/min—do not set much lower)
- `ALPHAVANTAGE_FROM_CURRENCY` / `ALPHAVANTAGE_TO_CURRENCY`
  - default from `POLYGON_SYMBOL` style or `EUR` / `USD`
- `ALPHAVANTAGE_BASE_URL`
  - default: `https://www.alphavantage.co/query`
- `POLYGON_API_KEY`
- `POLYGON_ENDPOINT`
  - default: `wss://socket.polygon.io/forex`
- `POLYGON_CHANNEL`
  - example: `C.EUR/USD` (websocket only)
- `POLYGON_REST_BASE_URL`
  - default: `https://api.polygon.io` (conversion REST)
- `POLYGON_CONVERSION_INTERVAL_SEC`
  - default: `1.5` (poll interval for `polygon_rest`)
- `POLYGON_CONVERSION_FROM` / `POLYGON_CONVERSION_TO`
  - path segments for `GET /v1/conversion/{from}/{to}`; default from `POLYGON_SYMBOL` / `EUR/USD`
- `OANDA_ACCOUNT_ID`
- `OANDA_API_TOKEN`
- `OANDA_STREAM_HOST`
  - demo/practice: `stream-fxpractice.oanda.com`
  - live: `stream-fxtrade.oanda.com`

It also reuses some repo `.env` values:

- `PO_SYMBOL`
- `PO_MODE`
- `PO_BROWSER_URL_DEMO`
- `PO_BROWSER_URL_LIVE`
- `PO_HEADLESS`
- `PO_BROWSER_STARTUP_WAIT_SEC`

## Recommended usage

### Alpha Vantage (default)

Uses [`function=CURRENCY_EXCHANGE_RATE`](https://www.alphavantage.co/documentation/#fx). **This is not a live tick stream**: you only get a new price when the script performs the next HTTP request (default ~every 12.5s, limited by Alpha Vantage’s free-tier rate cap). **If you need price changes continuously**, use **`--fx-provider oanda`** (streaming) or **`--fx-provider polygon`** (websocket, needs a plan that includes forex).

Because quotes arrive only on each poll, **widen the match window** vs streaming feeds, e.g. `--match-window-ms 20000`.

```bash
python tools/eurusd_compare/compare_feeds.py --po-symbol EURUSD --alphavantage-from EUR --alphavantage-to USD --match-window-ms 20000
```

Or set `ALPHAVANTAGE_API_KEY` in `tools/eurusd_compare/.env` and run without flags.

### Polygon (websocket)

If you want to compare Pocket Option's regular forex market against Polygon:

```bash
python tools/eurusd_compare/compare_feeds.py --fx-provider polygon --po-symbol EURUSD --polygon-channel C.EUR/USD
```

If you want to compare Pocket Option OTC instead:

```bash
python tools/eurusd_compare/compare_feeds.py --fx-provider polygon --po-symbol EURUSD_otc --polygon-channel C.EUR/USD
```

### Polygon REST (conversion poll)

Same API key as websocket mode. Uses Massive/Polygon [`GET /v1/conversion/{from}/{to}`](https://massive.com/docs/rest/forex/currency-conversion) (bid/ask and `last.timestamp`). Plan access in their docs matches the forex quote stream: **Currencies Basic — not included**; **Starter / Business — included** (real-time). Polling is coarser than websocket streaming but avoids WebSocket setup. If the API returns **`NOT_AUTHORIZED`** (HTTP 403), your key/plan does not include this product—upgrade on [Massive pricing](https://massive.com/pricing) or use `--fx-provider oanda`.

```bash
python tools/eurusd_compare/compare_feeds.py --fx-provider polygon_rest --po-symbol EURUSD --polygon-conversion-from EUR --polygon-conversion-to USD
```

### OANDA

If you want to use OANDA instead:

```bash
python tools/eurusd_compare/compare_feeds.py --fx-provider oanda --po-symbol EURUSD --oanda-instrument EUR_USD
```

You can also set duration and pairing window with either provider:

```bash
python tools/eurusd_compare/compare_feeds.py --po-symbol EURUSD --duration-sec 600 --match-window-ms 1000
```

## What happens when you run it

1. Opens Pocket Option in Playwright
2. Starts listening for websocket quote frames
3. Connects to the selected external FX feed
4. Saves:
   - Pocket Option raw parsed quotes
   - external FX raw quotes
   - matched quote pairs with price difference and timing columns
   - a summary JSON
5. Prints a **`live:`** line about every 5 seconds: latest **Pocket Option** mid, latest **forex feed** mid, **price_diff_pips**, **receive_skew_ms** (who arrived first on your PC), seconds since each side last updated, and optional **forex_vendor_to_PC_ms** when the feed exposes a quote time. A short legend is printed once at startup.

Output goes to:

`tools/eurusd_compare/output/<timestamp>/`

Files created:

- `pocketoption_quotes.jsonl`
- `fx_quotes.jsonl`
- `paired_quotes.csv`
- `summary.json`

## Notes

- **Polygon and `fx_events=0`:** for `--fx-provider polygon`, the tool prints every Polygon cluster status line as `[polygon] ...` and adds `fx_status` / `fx_err` on the periodic `status:` lines. If you never see `auth_success` followed by quote events, confirm your Massive/Polygon account is entitled to **real-time forex websocket** quotes (docs list **Currencies Starter** and **Business**; **Currencies Basic** does not include that stream). For HTTP-only checks, try `--fx-provider polygon_rest` against the [conversion endpoint](https://massive.com/docs/rest/forex/currency-conversion) (same plan table in their docs). Polygon sometimes offers **time-limited trials** that may include streaming—check your dashboard or current pricing for what your key can access. The websocket client waits for `auth_success` before sending `subscribe`, which avoids silent subscribe failures.
- The script does not force Pocket Option to switch symbols for you. If the page opens on another asset, select `EUR/USD` or `EURUSD_otc` manually in the browser.
- If Pocket Option is not logged in yet, log in after the browser opens and wait for websocket quotes.
- `po_minus_fx_mid_pips` is:
  - positive when Pocket Option is above the external mid
  - negative when Pocket Option is below the external mid
- `po_minus_fx_receive_ms` is:
  - positive when the Pocket Option quote was received later than the FX quote
  - negative when it was received earlier
