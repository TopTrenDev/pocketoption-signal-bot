from __future__ import annotations

import argparse
import asyncio
import csv
import inspect
import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    # Root .env (shared bot config), then optional tool-local files (override), e.g. tools/eurusd_compare/.env.
    load_dotenv(ROOT / ".env")
    _tool_env_paths = (
        ROOT / "tools" / ".env",
        ROOT / "tools" / "eurusd_compare" / ".env",
    )
    for _env_path in _tool_env_paths:
        if _env_path.is_file():
            load_dotenv(_env_path, override=True)
except Exception:
    pass

from pocket_signal_bot.adapters.browser_adapter import BrowserConfig, PocketOptionBrowserAdapter


def utc_iso(ts: float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def parse_rfc3339(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def format_rate_5(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value):.5f}"


def build_live_status_line(
    *,
    wall_ts: float,
    latest: dict[str, QuoteEvent],
    fx_source: str,
    po_symbol: str,
    fx_symbol: str,
    pocket_count: int,
    fx_count: int,
    pair_count: int,
    fx_stream: Any,
) -> str:
    """Human-readable periodic status (latest mids, skew, ages)—not internal 'event' jargon."""
    po = latest.get("pocketoption")
    fx_ev = latest.get(fx_source)
    po_mid = float(po.mid or po.price) if po and (po.mid is not None or po.price is not None) else None
    fx_mid = (
        float(fx_ev.mid or fx_ev.price)
        if fx_ev and (fx_ev.mid is not None or fx_ev.price is not None)
        else None
    )
    diff_pips: float | None = None
    if po_mid is not None and fx_mid is not None:
        diff_pips = (po_mid - fx_mid) * 10000.0

    skew_ms: float | None = None
    if po is not None and fx_ev is not None:
        skew_ms = (po.receive_ts - fx_ev.receive_ts) * 1000.0

    age_po = wall_ts - po.receive_ts if po is not None else None
    age_fx = wall_ts - fx_ev.receive_ts if fx_ev is not None else None

    fx_wire_ms: float | None = None
    if fx_ev is not None and fx_ev.source_ts is not None:
        fx_wire_ms = (fx_ev.receive_ts - fx_ev.source_ts) * 1000.0

    parts = [
        f"time={utc_iso(wall_ts)}",
        f"pocket_option_{po_symbol}={format_rate_5(po_mid)}",
        f"forex_{fx_symbol}={format_rate_5(fx_mid)}",
    ]
    if diff_pips is not None:
        parts.append(f"price_diff_pips={diff_pips:+.2f}")
    if skew_ms is not None:
        parts.append(f"receive_skew_ms={skew_ms:+.0f}")
    if age_po is not None:
        parts.append(f"since_last_PO_update_sec={age_po:.2f}")
    if age_fx is not None:
        parts.append(f"since_last_forex_update_sec={age_fx:.2f}")
    if fx_wire_ms is not None:
        parts.append(f"forex_vendor_to_PC_ms={fx_wire_ms:.0f}")

    parts.append(f"(updates: PO×{pocket_count} forex×{fx_count}; tight_sync_rows={pair_count})")
    fx_status = getattr(fx_stream, "last_status_line", None)
    if fx_status:
        parts.append(f"fx_feed={fx_status!r}")
    fx_err = getattr(fx_stream, "last_error", None)
    if fx_err:
        e = fx_err if len(fx_err) <= 200 else fx_err[:200] + "…"
        parts.append(f"fx_err={e!r}")
    return "live: " + " ".join(parts)


def default_polygon_channel() -> str:
    raw = (os.getenv("POLYGON_SYMBOL") or "EUR/USD").strip().upper()
    cleaned = raw.replace("_", "/").replace("-", "/")
    if "/" not in cleaned and len(cleaned) == 6:
        cleaned = f"{cleaned[:3]}/{cleaned[3:]}"
    return f"C.{cleaned}"


def default_polygon_conversion_ccy() -> tuple[str, str]:
    """(from_ccy, to_ccy) for REST GET /v1/conversion/{from}/{to} (Massive / Polygon)."""
    raw = (os.getenv("POLYGON_SYMBOL") or "EUR/USD").strip().upper()
    cleaned = raw.replace("_", "/").replace("-", "/")
    if "/" not in cleaned and len(cleaned) == 6:
        cleaned = f"{cleaned[:3]}/{cleaned[3:]}"
    if "/" in cleaned:
        a, _, b = cleaned.partition("/")
        return a.strip(), b.strip()
    return "EUR", "USD"


@dataclass
class QuoteEvent:
    source: str
    symbol: str
    receive_ts: float
    price: float | None = None
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    source_ts: float | None = None
    meta: dict[str, Any] | None = None

    def to_json(self) -> str:
        row = asdict(self)
        row["receive_iso"] = utc_iso(self.receive_ts)
        row["source_iso"] = utc_iso(self.source_ts)
        return json.dumps(row, ensure_ascii=True)


class PocketOptionQuoteStream(PocketOptionBrowserAdapter):
    def __init__(self, cfg: BrowserConfig, queue: asyncio.Queue[QuoteEvent]):
        super().__init__(cfg)
        self.queue = queue

    def _record_ws_price(self, p: float) -> None:
        super()._record_ws_price(p)
        self.queue.put_nowait(
            QuoteEvent(
                source="pocketoption",
                symbol=self.cfg.quote_asset,
                receive_ts=self._last_ws_ts,
                price=float(p),
                mid=float(p),
            )
        )


class OandaPriceStream:
    def __init__(
        self,
        *,
        queue: asyncio.Queue[QuoteEvent],
        loop: asyncio.AbstractEventLoop,
        account_id: str,
        api_token: str,
        instrument: str = "EUR_USD",
        host: str = "stream-fxpractice.oanda.com",
    ) -> None:
        self.queue = queue
        self.loop = loop
        self.account_id = account_id
        self.api_token = api_token
        self.instrument = instrument
        self.host = host
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._response = None
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="oanda-price-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._response is not None:
                self._response.close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    @staticmethod
    def _first_price(payload: dict[str, Any], scalar_key: str, list_key: str) -> float | None:
        scalar = payload.get(scalar_key)
        if scalar is not None:
            try:
                return float(scalar)
            except (TypeError, ValueError):
                pass
        values = payload.get(list_key) or []
        if isinstance(values, list) and values:
            first = values[0]
            if isinstance(first, dict):
                try:
                    return float(first.get("price"))
                except (TypeError, ValueError):
                    return None
        return None

    def _emit(self, event: QuoteEvent) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, event)

    def _run(self) -> None:
        params = parse.urlencode({"instruments": self.instrument})
        url = f"https://{self.host}/v3/accounts/{self.account_id}/pricing/stream?{params}"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept-Datetime-Format": "RFC3339",
        }
        req = request.Request(url, headers=headers, method="GET")
        try:
            with request.urlopen(req, timeout=60) as resp:
                self._response = resp
                while not self._stop.is_set():
                    raw = resp.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    receive_ts = time.time()
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") != "PRICE":
                        continue
                    bid = self._first_price(payload, "closeoutBid", "bids")
                    ask = self._first_price(payload, "closeoutAsk", "asks")
                    mid: float | None = None
                    if bid is not None and ask is not None:
                        mid = (bid + ask) / 2.0
                    elif bid is not None:
                        mid = bid
                    elif ask is not None:
                        mid = ask
                    if mid is None:
                        continue
                    source_ts = parse_rfc3339(payload.get("time"))
                    self._emit(
                        QuoteEvent(
                            source="oanda",
                            symbol=str(payload.get("instrument", self.instrument)),
                            receive_ts=receive_ts,
                            price=mid,
                            bid=bid,
                            ask=ask,
                            mid=mid,
                            source_ts=source_ts,
                            meta={"type": payload.get("type")},
                        )
                    )
        except Exception as e:
            self._last_error = str(e)


class PolygonPriceStream:
    """Polygon forex websocket; subscribes only after ``auth_success`` (required by their cluster)."""

    def __init__(
        self,
        *,
        queue: asyncio.Queue[QuoteEvent],
        api_key: str,
        channel: str = "C.EUR/USD",
        endpoint: str = "wss://socket.polygon.io/forex",
    ) -> None:
        self.queue = queue
        self.api_key = api_key
        self.channel = channel
        self.endpoint = endpoint
        self._task: asyncio.Task[None] | None = None
        self._last_error: str | None = None
        self._last_status_line: str | None = None
        self._unknown_ev_logged = 0

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def last_status_line(self) -> str | None:
        return self._last_status_line

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="polygon-price-stream")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def _emit(self, event: QuoteEvent) -> None:
        self.queue.put_nowait(event)

    @staticmethod
    def _format_status_payload(payload: dict[str, Any]) -> str:
        status = str(payload.get("status") or "")
        message = str(payload.get("message") or "")
        if status and message:
            return f"{status}: {message}"
        return status or message or json.dumps(payload, ensure_ascii=True)[:400]

    def _record_polygon_status(self, payload: dict[str, Any]) -> None:
        line = self._format_status_payload(payload)
        self._last_status_line = line
        print(f"[polygon] {line}", flush=True)
        st = str(payload.get("status") or "").lower()
        msg = str(payload.get("message") or "")
        combined = f"{st} {msg}".lower()
        if (
            st in ("auth_failed", "error", "failed")
            or "authentication failed" in combined
            or "not authorized" in combined
            or "not entitled" in combined
        ):
            self._last_error = line

    @staticmethod
    def _is_auth_success(payload: dict[str, Any]) -> bool:
        return str(payload.get("status") or "").lower() == "auth_success"

    async def _run(self) -> None:
        try:
            import websockets
        except ImportError as e:
            self._last_error = "Missing dependency: pip install websockets"
            raise RuntimeError(self._last_error) from e

        try:
            async with websockets.connect(self.endpoint, ping_interval=20, ping_timeout=20, max_queue=None) as ws:
                await ws.send(json.dumps({"action": "auth", "params": self.api_key}))
                subscribed = False
                auth_ok = False
                connect_mono = time.monotonic()
                while True:
                    recv_to = 25.0 if auth_ok else min(2.0, max(0.2, 30.0 - (time.monotonic() - connect_mono)))
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=recv_to)
                    except asyncio.TimeoutError:
                        if not auth_ok and time.monotonic() - connect_mono > 30.0:
                            self._last_error = self._last_error or (
                                "Timed out waiting for Polygon auth_success (check API key and plan)."
                            )
                            break
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        break
                    receive_ts = time.time()
                    try:
                        payloads = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payloads, dict):
                        payloads = [payloads]
                    if not isinstance(payloads, list):
                        continue
                    for payload in payloads:
                        if not isinstance(payload, dict):
                            continue
                        ev = payload.get("ev")
                        if ev == "status":
                            self._record_polygon_status(payload)
                            if not auth_ok and self._is_auth_success(payload):
                                auth_ok = True
                            if auth_ok and not subscribed:
                                await ws.send(json.dumps({"action": "subscribe", "params": self.channel}))
                                subscribed = True
                                print(f"[polygon] subscribe sent: {self.channel}", flush=True)
                            continue
                        if not auth_ok:
                            continue
                        if ev != "C":
                            if self._unknown_ev_logged < 5:
                                self._unknown_ev_logged += 1
                                snippet = json.dumps(payload, ensure_ascii=True)
                                if len(snippet) > 280:
                                    snippet = snippet[:280] + "..."
                                print(f"[polygon] non-quote event ev={ev!r}: {snippet}", flush=True)
                            continue
                        try:
                            ask = float(payload.get("a"))
                            bid = float(payload.get("b"))
                        except (TypeError, ValueError):
                            continue
                        mid = (bid + ask) / 2.0
                        source_ts_ms = payload.get("t")
                        try:
                            source_ts = float(source_ts_ms) / 1000.0 if source_ts_ms is not None else None
                        except (TypeError, ValueError):
                            source_ts = None
                        self._emit(
                            QuoteEvent(
                                source="polygon",
                                symbol=str(payload.get("p") or "EUR/USD"),
                                receive_ts=receive_ts,
                                price=mid,
                                bid=bid,
                                ask=ask,
                                mid=mid,
                                source_ts=source_ts,
                                meta={"exchange": payload.get("x"), "event": payload.get("ev")},
                            )
                        )
                if auth_ok and not subscribed:
                    self._last_error = self._last_error or "Polygon connection closed before subscribe completed."
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._last_error = str(e)


class PolygonConversionPollStream:
    """Poll Massive/Polygon REST `GET /v1/conversion/{from}/{to}` (bid/ask + timestamp)."""

    def __init__(
        self,
        *,
        queue: asyncio.Queue[QuoteEvent],
        api_key: str,
        from_ccy: str,
        to_ccy: str,
        base_url: str = "https://api.polygon.io",
        interval_sec: float = 1.5,
    ) -> None:
        self.queue = queue
        self.api_key = api_key
        self.from_ccy = from_ccy.strip().upper()
        self.to_ccy = to_ccy.strip().upper()
        self.base_url = base_url.rstrip("/")
        self.interval_sec = max(0.2, float(interval_sec))
        self._task: asyncio.Task[None] | None = None
        self._last_error: str | None = None
        self._last_status_line: str | None = None
        self._not_authorized_warned = False

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def last_status_line(self) -> str | None:
        return self._last_status_line

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="polygon-conversion-poll")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def _emit(self, event: QuoteEvent) -> None:
        self.queue.put_nowait(event)

    def _http_get_conversion(self) -> tuple[int, dict[str, Any]]:
        q = parse.urlencode({"apiKey": self.api_key})
        path = f"/v1/conversion/{parse.quote(self.from_ccy, safe='')}/{parse.quote(self.to_ccy, safe='')}"
        url = f"{self.base_url}{path}?{q}"
        req = request.Request(url, method="GET")
        try:
            with request.urlopen(req, timeout=20) as resp:
                status = int(resp.status)
                body = resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            status = int(e.code)
            body = (e.read().decode("utf-8", errors="replace") if getattr(e, "fp", None) else "") or ""
        try:
            parsed: Any = json.loads(body)
        except json.JSONDecodeError:
            return status, {"status": "INVALID_JSON", "message": body[:800]}
        if not isinstance(parsed, dict):
            return status, {"status": "INVALID_SHAPE", "message": str(parsed)[:400]}
        return status, parsed

    async def _poll_once(self) -> str:
        """Process one REST response. Returns sleep hint: 'backoff' or 'normal'."""
        receive_ts = time.time()
        status, data = await asyncio.to_thread(self._http_get_conversion)
        api_status = str(data.get("status") or "")

        if status != 200:
            msg = str(data.get("message") or json.dumps(data, ensure_ascii=True)[:600])
            self._last_status_line = f"HTTP {status}; {api_status or 'error'}"
            self._last_error = msg
            if api_status == "NOT_AUTHORIZED" and not self._not_authorized_warned:
                self._not_authorized_warned = True
                print(
                    "[polygon_rest] NOT_AUTHORIZED: this key/plan cannot use GET /v1/conversion/{from}/{to}. "
                    "Massive docs list Currencies Basic as not included; Starter/Business include it. "
                    "See https://massive.com/pricing — or use --fx-provider oanda for a broker stream.",
                    flush=True,
                )
            return "backoff" if api_status == "NOT_AUTHORIZED" else "normal"

        sym = str(data.get("symbol") or f"{self.from_ccy}/{self.to_ccy}")
        self._last_status_line = f"HTTP {status}; API {api_status or 'ok'}; {sym}"
        if api_status and api_status.lower() != "success":
            self._last_error = json.dumps(data, ensure_ascii=True)[:800]
            return "normal"
        last = data.get("last")
        if not isinstance(last, dict):
            self._last_error = f"Missing last quote object: {json.dumps(data, ensure_ascii=True)[:400]}"
            return "normal"
        try:
            bid = float(last["bid"])
            ask = float(last["ask"])
        except (KeyError, TypeError, ValueError) as e:
            self._last_error = f"Bad bid/ask in response: {e}"
            return "normal"
        mid = (bid + ask) / 2.0
        ts_ms = last.get("timestamp")
        try:
            source_ts = float(ts_ms) / 1000.0 if ts_ms is not None else None
        except (TypeError, ValueError):
            source_ts = None
        self._last_error = None
        self._emit(
            QuoteEvent(
                source="polygon_rest",
                symbol=sym,
                receive_ts=receive_ts,
                price=mid,
                bid=bid,
                ask=ask,
                mid=mid,
                source_ts=source_ts,
                meta={
                    "exchange": last.get("exchange"),
                    "endpoint": "conversion",
                },
            )
        )
        return "normal"

    async def _run(self) -> None:
        try:
            while True:
                try:
                    hint = await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._last_error = str(e)
                    hint = "normal"
                sleep_sec = self.interval_sec
                if hint == "backoff":
                    sleep_sec = max(self.interval_sec, 60.0)
                await asyncio.sleep(sleep_sec)
        except asyncio.CancelledError:
            raise


class AlphaVantageFxPollStream:
    """Poll Alpha Vantage ``CURRENCY_EXCHANGE_RATE`` (see https://www.alphavantage.co/documentation/#fx)."""

    _RT_KEY = "Realtime Currency Exchange Rate"

    def __init__(
        self,
        *,
        queue: asyncio.Queue[QuoteEvent],
        api_key: str,
        from_ccy: str,
        to_ccy: str,
        base_url: str = "https://www.alphavantage.co/query",
        interval_sec: float = 12.5,
    ) -> None:
        self.queue = queue
        self.api_key = api_key.strip()
        self.from_ccy = from_ccy.strip().upper()
        self.to_ccy = to_ccy.strip().upper()
        self.base_url = base_url.rstrip("/")
        self.interval_sec = max(1.0, float(interval_sec))
        self._task: asyncio.Task[None] | None = None
        self._last_error: str | None = None
        self._last_status_line: str | None = None
        self._rate_note_warned = False

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def last_status_line(self) -> str | None:
        return self._last_status_line

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="alphavantage-fx-poll")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def _emit(self, event: QuoteEvent) -> None:
        self.queue.put_nowait(event)

    def _http_get(self) -> dict[str, Any]:
        params = parse.urlencode(
            {
                "function": "CURRENCY_EXCHANGE_RATE",
                "from_currency": self.from_ccy,
                "to_currency": self.to_ccy,
                "apikey": self.api_key,
            }
        )
        url = f"{self.base_url}?{params}"
        req = request.Request(url, method="GET")
        try:
            with request.urlopen(req, timeout=25) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            err_body = (e.read().decode("utf-8", errors="replace") if getattr(e, "fp", None) else "") or ""
            return {"_http_error": int(e.code), "_raw": err_body[:1200]}
        try:
            data: Any = json.loads(body)
        except json.JSONDecodeError:
            return {"_parse_error": body[:800]}
        if not isinstance(data, dict):
            return {"_invalid_shape": True}
        return data

    @staticmethod
    def _parse_last_refreshed(refreshed: str | None, _tz_hint: str | None) -> float | None:
        if not refreshed:
            return None
        try:
            dt_naive = datetime.strptime(refreshed.strip(), "%Y-%m-%d %H:%M:%S")
            return dt_naive.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None

    async def _poll_once(self) -> str:
        """Returns sleep hint: 'backoff' for rate limits, else 'normal'."""
        receive_ts = time.time()
        data = await asyncio.to_thread(self._http_get)

        if "_http_error" in data:
            self._last_status_line = f"HTTP {data['_http_error']}"
            self._last_error = data.get("_raw", str(data))[:800]
            return "normal"

        if "_parse_error" in data:
            self._last_status_line = "invalid JSON"
            self._last_error = str(data["_parse_error"])
            return "normal"

        note = data.get("Note") or data.get("Information")
        if isinstance(note, str) and note.strip():
            self._last_error = note.strip()[:800]
            self._last_status_line = "rate_limit_or_note"
            if not self._rate_note_warned and (
                "frequency" in note.lower() or "api calls" in note.lower() or "thank you" in note.lower()
            ):
                self._rate_note_warned = True
                print(
                    "[alphavantage] Rate limit / throttling message from API. "
                    "Free tier allows ~5 calls/min; increase --alphavantage-interval-sec (e.g. 15).",
                    flush=True,
                )
            return "backoff"

        err_msg = data.get("Error Message")
        if isinstance(err_msg, str) and err_msg.strip():
            self._last_error = err_msg.strip()[:800]
            self._last_status_line = "error_message"
            return "normal"

        block = data.get(self._RT_KEY)
        if not isinstance(block, dict):
            self._last_error = json.dumps(data, ensure_ascii=True)[:800]
            self._last_status_line = "missing_Realtime_Currency_Exchange_Rate"
            return "normal"

        try:
            rate = float(block.get("5. Exchange Rate"))
            bid = float(block.get("8. Bid Price", rate))
            ask = float(block.get("9. Ask Price", rate))
        except (TypeError, ValueError):
            self._last_error = f"Unparseable prices in Alpha Vantage response: {json.dumps(block)[:400]}"
            self._last_status_line = "bad_prices"
            return "normal"

        mid = (bid + ask) / 2.0
        refreshed = block.get("6. Last Refreshed")
        tz_hint = block.get("7. Time Zone")
        source_ts = self._parse_last_refreshed(
            str(refreshed) if refreshed is not None else None,
            str(tz_hint) if tz_hint is not None else None,
        )
        sym = f"{self.from_ccy}/{self.to_ccy}"
        self._last_error = None
        self._last_status_line = f"ok {sym} rate={rate}"
        self._emit(
            QuoteEvent(
                source="alphavantage",
                symbol=sym,
                receive_ts=receive_ts,
                price=mid,
                bid=bid,
                ask=ask,
                mid=mid,
                source_ts=source_ts,
                meta={"function": "CURRENCY_EXCHANGE_RATE", "exchange_rate": rate},
            )
        )
        return "normal"

    async def _run(self) -> None:
        try:
            while True:
                try:
                    hint = await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._last_error = str(e)
                    hint = "normal"
                sleep_sec = self.interval_sec
                if hint == "backoff":
                    sleep_sec = max(self.interval_sec, 60.0)
                await asyncio.sleep(sleep_sec)
        except asyncio.CancelledError:
            raise


async def maybe_await(call_result: Any) -> Any:
    if inspect.isawaitable(call_result):
        return await call_result
    return call_result


def build_parser() -> argparse.ArgumentParser:
    mode = (os.getenv("PO_MODE", "demo") or "demo").strip().lower()
    default_browser_url = os.getenv(
        "PO_BROWSER_URL_LIVE" if mode == "live" else "PO_BROWSER_URL_DEMO",
        "https://pocketoption.com/en/cabinet/demo-quick-high-low/",
    )
    parser = argparse.ArgumentParser(
        description="Compare Pocket Option EUR/USD websocket prices against a real FX API feed."
    )
    parser.add_argument("--duration-sec", type=int, default=300, help="Run duration in seconds.")
    parser.add_argument(
        "--match-window-ms",
        type=int,
        default=1500,
        help="Max local receive-time gap when pairing Pocket Option and FX quotes.",
    )
    parser.add_argument("--po-symbol", default=os.getenv("PO_SYMBOL", "EURUSD"), help="Pocket Option symbol.")
    parser.add_argument(
        "--po-url",
        default=default_browser_url,
        help="Pocket Option trading page URL to open in Playwright.",
    )
    parser.add_argument(
        "--po-headless",
        action="store_true",
        default=(os.getenv("PO_HEADLESS", "false").strip().lower() in {"1", "true", "yes", "on"}),
        help="Run Pocket Option browser headless.",
    )
    parser.add_argument(
        "--po-startup-wait-sec",
        type=int,
        default=int(os.getenv("PO_BROWSER_STARTUP_WAIT_SEC", "20")),
        help="Seconds to wait after opening Pocket Option before expecting quotes.",
    )
    parser.add_argument(
        "--fx-provider",
        default=os.getenv("FX_PROVIDER", "alphavantage"),
        choices=["alphavantage", "polygon", "polygon_rest", "oanda"],
        help="External FX: alphavantage (CURRENCY_EXCHANGE_RATE poll), polygon (WS), polygon_rest, oanda.",
    )
    parser.add_argument(
        "--alphavantage-api-key",
        default=os.getenv("ALPHAVANTAGE_API_KEY", ""),
        help="Alpha Vantage API key (https://www.alphavantage.co/support/#api-key).",
    )
    parser.add_argument(
        "--alphavantage-base-url",
        default=os.getenv("ALPHAVANTAGE_BASE_URL", "https://www.alphavantage.co/query"),
        help="Alpha Vantage query endpoint.",
    )
    parser.add_argument(
        "--alphavantage-interval-sec",
        type=float,
        default=float(os.getenv("ALPHAVANTAGE_INTERVAL_SEC", "12.5")),
        help="Seconds between Alpha Vantage polls (free tier ~5 req/min; use >= 12).",
    )
    av_from, av_to = default_polygon_conversion_ccy()
    parser.add_argument(
        "--alphavantage-from",
        default=os.getenv("ALPHAVANTAGE_FROM_CURRENCY", av_from),
        help="from_currency for CURRENCY_EXCHANGE_RATE (e.g. EUR).",
    )
    parser.add_argument(
        "--alphavantage-to",
        default=os.getenv("ALPHAVANTAGE_TO_CURRENCY", av_to),
        help="to_currency for CURRENCY_EXCHANGE_RATE (e.g. USD).",
    )
    parser.add_argument(
        "--polygon-api-key",
        default=os.getenv("POLYGON_API_KEY", ""),
        help="Polygon API key for forex websocket access.",
    )
    parser.add_argument(
        "--polygon-endpoint",
        default=os.getenv("POLYGON_ENDPOINT", "wss://socket.polygon.io/forex"),
        help="Polygon websocket endpoint.",
    )
    parser.add_argument(
        "--polygon-channel",
        default=os.getenv("POLYGON_CHANNEL", default_polygon_channel()),
        help="Polygon forex websocket subscription channel, e.g. C.EUR/USD",
    )
    parser.add_argument(
        "--polygon-rest-base-url",
        default=os.getenv("POLYGON_REST_BASE_URL", "https://api.polygon.io"),
        help="REST base URL for polygon_rest (Massive/Polygon conversion API).",
    )
    parser.add_argument(
        "--polygon-conversion-interval-sec",
        type=float,
        default=float(os.getenv("POLYGON_CONVERSION_INTERVAL_SEC", "1.5")),
        help="Seconds between conversion REST polls when using --fx-provider polygon_rest.",
    )
    conv_from, conv_to = default_polygon_conversion_ccy()
    parser.add_argument(
        "--polygon-conversion-from",
        default=os.getenv("POLYGON_CONVERSION_FROM", conv_from),
        help='REST conversion "from" currency (path segment), e.g. EUR',
    )
    parser.add_argument(
        "--polygon-conversion-to",
        default=os.getenv("POLYGON_CONVERSION_TO", conv_to),
        help='REST conversion "to" currency (path segment), e.g. USD',
    )
    parser.add_argument(
        "--oanda-account-id",
        default=os.getenv("OANDA_ACCOUNT_ID", ""),
        help="OANDA v20 account id.",
    )
    parser.add_argument(
        "--oanda-api-token",
        default=os.getenv("OANDA_API_TOKEN", ""),
        help="OANDA v20 API token.",
    )
    parser.add_argument(
        "--oanda-host",
        default=os.getenv("OANDA_STREAM_HOST", "stream-fxpractice.oanda.com"),
        help="OANDA stream host: stream-fxpractice.oanda.com or stream-fxtrade.oanda.com",
    )
    parser.add_argument(
        "--oanda-instrument",
        default=os.getenv("OANDA_INSTRUMENT", "EUR_USD"),
        help="OANDA instrument code.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "tools" / "eurusd_compare" / "output"),
        help="Directory for JSONL/CSV output files.",
    )
    return parser


async def run_compare(args: argparse.Namespace) -> int:
    if args.fx_provider == "oanda" and (not args.oanda_account_id or not args.oanda_api_token):
        print("Missing OANDA credentials. Set --oanda-account-id and --oanda-api-token.", flush=True)
        return 2
    if args.fx_provider == "alphavantage" and not args.alphavantage_api_key:
        print("Missing Alpha Vantage API key. Set --alphavantage-api-key or ALPHAVANTAGE_API_KEY.", flush=True)
        return 2
    if args.fx_provider in ("polygon", "polygon_rest") and not args.polygon_api_key:
        print("Missing Polygon API key. Set --polygon-api-key or POLYGON_API_KEY.", flush=True)
        return 2
    if args.fx_provider == "polygon":
        try:
            import websockets  # type: ignore # noqa: F401
        except ImportError:
            print("Missing dependency for Polygon mode. Install it with: pip install websockets", flush=True)
            return 2

    output_root = Path(args.output_dir)
    run_dir = output_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    pair_rows_path = run_dir / "paired_quotes.csv"
    pocket_path = run_dir / "pocketoption_quotes.jsonl"
    fx_path = run_dir / "fx_quotes.jsonl"
    summary_path = run_dir / "summary.json"

    queue: asyncio.Queue[QuoteEvent] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    po = PocketOptionQuoteStream(
        BrowserConfig(
            headless=bool(args.po_headless),
            base_url=str(args.po_url),
            quote_asset=str(args.po_symbol),
            use_ws_quotes=True,
            show_overlay=False,
            startup_wait_ms=max(1000, int(args.po_startup_wait_sec) * 1000),
        ),
        queue,
    )
    if args.fx_provider == "polygon":
        fx: Any = PolygonPriceStream(
            queue=queue,
            api_key=str(args.polygon_api_key),
            channel=str(args.polygon_channel),
            endpoint=str(args.polygon_endpoint),
        )
        fx_symbol = str(args.polygon_channel)
    elif args.fx_provider == "polygon_rest":
        fx = PolygonConversionPollStream(
            queue=queue,
            api_key=str(args.polygon_api_key),
            from_ccy=str(args.polygon_conversion_from),
            to_ccy=str(args.polygon_conversion_to),
            base_url=str(args.polygon_rest_base_url),
            interval_sec=float(args.polygon_conversion_interval_sec),
        )
        fx_symbol = f"{args.polygon_conversion_from}/{args.polygon_conversion_to}"
    elif args.fx_provider == "alphavantage":
        fx = AlphaVantageFxPollStream(
            queue=queue,
            api_key=str(args.alphavantage_api_key),
            from_ccy=str(args.alphavantage_from),
            to_ccy=str(args.alphavantage_to),
            base_url=str(args.alphavantage_base_url),
            interval_sec=float(args.alphavantage_interval_sec),
        )
        fx_symbol = f"{args.alphavantage_from}/{args.alphavantage_to}"
    else:
        fx = OandaPriceStream(
            queue=queue,
            loop=loop,
            account_id=str(args.oanda_account_id),
            api_token=str(args.oanda_api_token),
            instrument=str(args.oanda_instrument),
            host=str(args.oanda_host),
        )
        fx_symbol = str(args.oanda_instrument)

    latest: dict[str, QuoteEvent] = {}
    pair_count = 0
    diff_pips_values: list[float] = []
    abs_diff_pips_values: list[float] = []
    receive_skew_values: list[float] = []
    fx_transport_lag_values: list[float] = []
    pocket_count = 0
    fx_count = 0
    start_ts = time.time()
    next_status_ts = start_ts + 5.0

    print(f"Output directory: {run_dir}", flush=True)
    print(
        f"Opening Pocket Option at {args.po_url} for symbol {args.po_symbol}. "
        "If needed, log in and select that asset in the UI.",
        flush=True,
    )
    print(f"Starting {args.fx_provider} external feed for {fx_symbol}.", flush=True)
    if args.fx_provider == "alphavantage":
        print(
            "Alpha Vantage only updates on each HTTP poll (see --alphavantage-interval-sec); "
            "it does not stream every price change. For continuous FX ticks use "
            "--fx-provider oanda (pricing stream) or --fx-provider polygon (websocket) if your plan allows. "
            "If paired samples stay at 0, widen --match-window-ms (e.g. 15000–30000).",
            flush=True,
        )

    await po.connect()
    await maybe_await(fx.start())

    print(
        "How to read each 'live:' line (wall clock = your PC when the line prints): "
        "pocket_option_* = latest PO price seen here; forex_* = latest reference price; "
        "price_diff_pips = PO minus forex (in pips); receive_skew_ms = PO_receive_time minus forex_receive_time "
        "(+ means the PO tick arrived later on this PC); since_last_* = seconds since that side last updated; "
        "forex_vendor_to_PC_ms = forex quote timestamp to local receive (when the feed gives a time); "
        "tight_sync_rows = rows written to CSV when both ticks fell within --match-window-ms (for stats).",
        flush=True,
    )

    with (
        pair_rows_path.open("w", encoding="utf-8", newline="") as pair_file,
        pocket_path.open("w", encoding="utf-8") as pocket_file,
        fx_path.open("w", encoding="utf-8") as fx_file,
    ):
        pair_writer = csv.DictWriter(
            pair_file,
            fieldnames=[
                "pair_id",
                "paired_at_iso",
                "po_symbol",
                "po_receive_iso",
                "po_price",
                "fx_symbol",
                "fx_receive_iso",
                "fx_source_iso",
                "fx_bid",
                "fx_ask",
                "fx_mid",
                "po_minus_fx_mid_pips",
                "po_minus_fx_receive_ms",
                "fx_transport_lag_ms",
            ],
        )
        pair_writer.writeheader()

        try:
            while time.time() - start_ts < int(args.duration_sec):
                timeout = max(0.1, min(1.0, start_ts + int(args.duration_sec) - time.time()))
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    event = None
                if event is not None:
                    latest[event.source] = event
                    if event.source == "pocketoption":
                        pocket_count += 1
                        pocket_file.write(event.to_json() + "\n")
                    else:
                        fx_count += 1
                        fx_file.write(event.to_json() + "\n")

                    other_source = args.fx_provider if event.source == "pocketoption" else "pocketoption"
                    other = latest.get(other_source)
                    if other is not None:
                        po_event = event if event.source == "pocketoption" else other
                        fx_event = event if event.source != "pocketoption" else other
                        skew_ms = (po_event.receive_ts - fx_event.receive_ts) * 1000.0
                        if abs(skew_ms) <= float(args.match_window_ms):
                            pair_count += 1
                            po_price = float(po_event.price or 0.0)
                            fx_mid = float(fx_event.mid or fx_event.price or 0.0)
                            diff_pips = (po_price - fx_mid) * 10000.0
                            diff_pips_values.append(diff_pips)
                            abs_diff_pips_values.append(abs(diff_pips))
                            receive_skew_values.append(skew_ms)
                            transport_lag_ms = None
                            if fx_event.source_ts is not None:
                                transport_lag_ms = (fx_event.receive_ts - fx_event.source_ts) * 1000.0
                                fx_transport_lag_values.append(transport_lag_ms)

                            pair_writer.writerow(
                                {
                                    "pair_id": pair_count,
                                    "paired_at_iso": utc_iso(time.time()),
                                    "po_symbol": po_event.symbol,
                                    "po_receive_iso": utc_iso(po_event.receive_ts),
                                    "po_price": f"{po_price:.8f}",
                                    "fx_symbol": fx_event.symbol,
                                    "fx_receive_iso": utc_iso(fx_event.receive_ts),
                                    "fx_source_iso": utc_iso(fx_event.source_ts),
                                    "fx_bid": "" if fx_event.bid is None else f"{fx_event.bid:.8f}",
                                    "fx_ask": "" if fx_event.ask is None else f"{fx_event.ask:.8f}",
                                    "fx_mid": f"{fx_mid:.8f}",
                                    "po_minus_fx_mid_pips": f"{diff_pips:.4f}",
                                    "po_minus_fx_receive_ms": f"{skew_ms:.2f}",
                                    "fx_transport_lag_ms": ""
                                    if transport_lag_ms is None
                                    else f"{transport_lag_ms:.2f}",
                                }
                            )

                now = time.time()
                if now >= next_status_ts:
                    print(
                        build_live_status_line(
                            wall_ts=now,
                            latest=latest,
                            fx_source=str(args.fx_provider),
                            po_symbol=str(args.po_symbol),
                            fx_symbol=str(fx_symbol),
                            pocket_count=pocket_count,
                            fx_count=fx_count,
                            pair_count=pair_count,
                            fx_stream=fx,
                        ),
                        flush=True,
                    )
                    next_status_ts = now + 5.0
        finally:
            await maybe_await(fx.stop())
            await po.disconnect()

    summary = {
        "run_dir": str(run_dir),
        "duration_sec": int(args.duration_sec),
        "po_symbol": args.po_symbol,
        "fx_provider": args.fx_provider,
        "fx_symbol": fx_symbol,
        "pocketoption_event_count": pocket_count,
        "fx_event_count": fx_count,
        "paired_sample_count": pair_count,
        "mean_po_minus_fx_mid_pips": (
            sum(diff_pips_values) / len(diff_pips_values) if diff_pips_values else None
        ),
        "median_abs_price_diff_pips": percentile(abs_diff_pips_values, 50.0),
        "p95_abs_price_diff_pips": percentile(abs_diff_pips_values, 95.0),
        "median_po_minus_fx_receive_ms": percentile(receive_skew_values, 50.0),
        "p95_abs_po_minus_fx_receive_ms": percentile([abs(v) for v in receive_skew_values], 95.0),
        "median_fx_transport_lag_ms": percentile(fx_transport_lag_values, 50.0),
        "p95_fx_transport_lag_ms": percentile(fx_transport_lag_values, 95.0),
        "fx_error": getattr(fx, "last_error", None),
        "fx_last_status": getattr(fx, "last_status_line", None),
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=True), encoding="utf-8")

    print(json.dumps(summary, indent=2, ensure_ascii=True), flush=True)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(run_compare(args))
    except KeyboardInterrupt:
        print("Interrupted.", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
