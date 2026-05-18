from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from collections import deque
import json
import re
import time


@dataclass
class BrowserConfig:
    headless: bool = True
    base_url: str = "https://pocketoption.com/en/cabinet/demo-quick-high-low/"
    is_real_account: bool = False
    price_selectors: tuple[str, ...] = ()
    payout_selectors: tuple[str, ...] = ()
    startup_wait_ms: int = 5000
    quote_asset: str = "EURUSD_otc"
    use_ws_quotes: bool = True
    show_overlay: bool = True
    ws_debug: bool = False
    ws_debug_max_lines: int = 120


class PocketOptionBrowserAdapter:
    """
    Browser fallback adapter.
    Intentionally minimal until selectors are validated on your account UI.
    """

    def __init__(self, cfg: BrowserConfig):
        self.cfg = cfg
        self._playwright = None
        self._browser = None
        self._page = None
        self._ticks: deque[tuple[float, float]] = deque(maxlen=3000)
        self._orders: dict[str, dict[str, Any]] = {}
        self._last_ws_price: float | None = None
        self._last_ws_ts: float = 0.0
        self._ws_debug_lines = 0
        self._ws_seen_urls: set[str] = set()

    @property
    def last_ws_quote(self) -> float | None:
        return self._last_ws_price

    @staticmethod
    def _normalize_pair(asset: str) -> str:
        a = (asset or "").upper().replace("_OTC", "").replace("-OTC", "").replace("/", "").replace("-", "")
        return a

    def _on_websocket(self, ws: Any) -> None:
        ws_url = ""
        try:
            ws_url = str(getattr(ws, "url", "") or "")
        except Exception:
            ws_url = ""
        if ws_url and ws_url not in self._ws_seen_urls:
            self._ws_seen_urls.add(ws_url)
            self._ws_debug(f"socket_open url={ws_url}")

        def on_frame(frame: Any, direction: str = "recv") -> None:
            try:
                text: str | None = None
                if isinstance(frame, str):
                    text = frame
                elif isinstance(frame, (bytes, bytearray)):
                    text = bytes(frame).decode("utf-8", errors="ignore")
                elif hasattr(frame, "text") and frame.text:
                    text = str(frame.text)
                elif hasattr(frame, "body") and frame.body:
                    b = frame.body
                    text = b.decode("utf-8", errors="ignore") if isinstance(b, (bytes, bytearray)) else str(b)
                if text:
                    self._ingest_ws_payload(text, ws_url=ws_url, direction=direction)
            except Exception:
                pass

        try:
            ws.on("framereceived", lambda frame: on_frame(frame, "recv"))
        except Exception:
            pass
        try:
            ws.on("framesent", lambda frame: on_frame(frame, "sent"))
        except Exception:
            pass

    def _ws_debug(self, message: str) -> None:
        if not self.cfg.ws_debug:
            return
        if self._ws_debug_lines >= self.cfg.ws_debug_max_lines:
            return
        self._ws_debug_lines += 1
        print(f"[WSDBG] {message}", flush=True)

    def _walk_quote_json(self, obj: Any, sym: str) -> float | None:
        """Find a plausible last price for sym inside nested JSON."""
        sym_u = self._normalize_pair(sym)
        if isinstance(obj, list):
            # Common compact quote tuples:
            # ["EURUSD", ts, bid, ask] or ["EURUSD", bid, ask] or nested list of those tuples.
            if obj and isinstance(obj[0], str):
                pair = self._normalize_pair(obj[0])
                if sym_u and (sym_u in pair or pair in sym_u):
                    nums = [float(x) for x in obj[1:] if isinstance(x, (int, float))]
                    # prefer last numeric as "last/ask" style fallback
                    if nums:
                        return nums[-1]
            for item in obj:
                r = self._walk_quote_json(item, sym)
                if r is not None:
                    return r
            return None
        if isinstance(obj, dict):
            asset = (
                obj.get("asset")
                or obj.get("symbol")
                or obj.get("pair")
                or obj.get("name")
                or obj.get("s")
                or obj.get("ticker")
            )
            if asset is not None:
                a = self._normalize_pair(str(asset))
                if sym_u and (sym_u in a or a in sym_u):
                    for pk in (
                        "close",
                        "price",
                        "last",
                        "bid",
                        "ask",
                        "rate",
                        "value",
                        "v",
                        "c",
                        "quote",
                    ):
                        v = obj.get(pk)
                        if isinstance(v, (int, float)) and 0.01 < float(v) < 1_000_000:
                            return float(v)
            for v in obj.values():
                r = self._walk_quote_json(v, sym)
                if r is not None:
                    return r
        return None

    @staticmethod
    def _extract_json_array_text(text: str) -> str | None:
        """
        Extract a JSON array string from frame variants like:
        - 42["event", {...}]
        - 451-["event", {...}]
        - prefixed non-json wrappers containing [...payload...]
        """
        m = re.search(r"(\[.*\])", text)
        if not m:
            return None
        return m.group(1)

    def _ingest_ws_payload(self, text: str, *, ws_url: str = "", direction: str = "recv") -> None:
        if not self.cfg.use_ws_quotes or len(text) < 2:
            return
        sym = self.cfg.quote_asset
        array_text = self._extract_json_array_text(text)
        if not array_text:
            return
        try:
            payload = json.loads(array_text)
        except json.JSONDecodeError:
            self._ws_debug(f"{direction} non-json sample={text[:120]!r} url={ws_url[:80]}")
            return
        if not isinstance(payload, list) or not payload:
            self._ws_debug(f"{direction} non-list sample={str(payload)[:120]} url={ws_url[:80]}")
            return
        evt = payload[0]
        data = payload[1] if len(payload) > 1 else None
        if isinstance(evt, str):
            el = evt.lower()
            if "chat" in el and not any(k in el for k in ("quote", "price", "tick", "rate", "candle", "history", "update", "asset", "symbol")):
                return
            self._ws_debug(f"{direction} evt={evt} sym={sym} data_type={type(data).__name__} url={ws_url[:80]}")
            if any(k in el for k in ("quote", "price", "tick", "rate", "candle", "history", "asset", "symbol", "update")):
                p = self._walk_quote_json(data, sym)
                if p is not None:
                    self._ws_debug(f"{direction} evt={evt} parsed_price={p}")
                    self._record_ws_price(p)
                    return
        p = self._walk_quote_json(payload, sym)
        if p is not None:
            self._ws_debug(f"{direction} fallback parsed_price={p} evt={evt}")
            self._record_ws_price(p)
            return
        self._ws_debug(f"{direction} evt={evt} no-price sample={str(payload)[:140]} url={ws_url[:80]}")

    def _record_ws_price(self, p: float) -> None:
        now = time.time()
        self._last_ws_price = p
        self._last_ws_ts = now
        self._ticks.append((now, p))

    async def connect(self) -> None:
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except Exception as e:
            raise RuntimeError("Install playwright: pip install playwright && playwright install chromium") from e
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.cfg.headless)
        context = await self._browser.new_context()
        self._page = await context.new_page()
        self._page.on("websocket", self._on_websocket)
        await self._page.goto(self.cfg.base_url, wait_until="domcontentloaded")
        await self._page.wait_for_timeout(int(self.cfg.startup_wait_ms))

    async def show_status_overlay(self, text: str) -> None:
        """Fixed corner panel on the trading page (visible browser only)."""
        if not self.cfg.show_overlay or self._page is None:
            return
        try:
            await self._page.evaluate(
                """(t) => {
                  let el = document.getElementById('__po_bot_status__');
                  if (!el) {
                    el = document.createElement('div');
                    el.id = '__po_bot_status__';
                    el.style.cssText =
                      'position:fixed;left:8px;bottom:8px;max-width:480px;z-index:2147483647;' +
                      'background:rgba(0,0,0,.88);color:#7cfc00;font:12px/1.45 ui-monospace,Consolas,monospace;' +
                      'white-space:pre-wrap;padding:10px 12px;border-radius:8px;border:1px solid #444;' +
                      'box-shadow:0 2px 12px rgba(0,0,0,.5);pointer-events:none;';
                    document.body.appendChild(el);
                  }
                  el.textContent = t;
                }""",
                text,
            )
        except Exception:
            pass

    async def _read_price(self) -> float:
        if self._page is None:
            raise RuntimeError("Browser adapter is not connected")
        if self.cfg.use_ws_quotes and self._last_ws_price is not None:
            age = time.time() - self._last_ws_ts
            if age < 20.0:
                return float(self._last_ws_price)
        selectors = list(self.cfg.price_selectors) + [
            ".value___UPL-L",
            ".value--current",
            ".current-price",
            "[data-qa='current-price']",
        ]
        for sel in selectors:
            try:
                txt = await self._page.locator(sel).first.text_content(timeout=300)
                if txt:
                    cleaned = txt.replace(",", "").strip()
                    return float(cleaned)
            except Exception:
                continue
        if self.cfg.use_ws_quotes and self._last_ws_price is not None:
            return float(self._last_ws_price)
        raise RuntimeError(
            "Could not read price: enable PO_USE_WS_QUOTES=true and wait for quotes, "
            "or set PO_PRICE_SELECTOR to a DOM element with the quote text."
        )

    async def disconnect(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def get_candles(self, asset: str, timeframe_sec: int, count: int) -> list[dict[str, Any]]:
        if self.cfg.use_ws_quotes and self._page is not None:
            deadline = time.time() + 45.0
            while self._last_ws_price is None and time.time() < deadline:
                await self._page.wait_for_timeout(250)
        now = time.time()
        price = await self._read_price()
        if not self._ticks or self._ticks[-1][1] != price:
            self._ticks.append((now, price))
        if len(self._ticks) < 3:
            return [{"close": price, "time": int(now)}]

        buckets: dict[int, list[float]] = {}
        for ts, p in self._ticks:
            bucket = int(ts // timeframe_sec)
            buckets.setdefault(bucket, []).append(p)
        keys = sorted(buckets.keys())[-count:]
        candles = [{"time": k * timeframe_sec, "close": float(buckets[k][-1])} for k in keys]
        return candles

    async def get_payout_pct(self, asset: str) -> float:
        if self._page is None:
            return 80.0
        # Prefer payout percentages from the RIGHT trading panel.
        # This avoids grabbing unrelated top-tile values and "100% bonus" banner text.
        try:
            values = await self._page.evaluate(
                """() => {
                  const out = [];
                  const w = window.innerWidth || 0;
                  const re = /\\+?\\s*(\\d+(?:[\\.,]\\d+)?)\\s*%/g;
                  const pushFrom = (el) => {
                    if (!el) return;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return;
                    if (r.right < w * 0.55) return; // keep right side only
                    const txt = (el.textContent || "").trim();
                    if (!txt) return;
                    if (txt.toLowerCase().includes("bonus")) return;
                    let m;
                    while ((m = re.exec(txt)) !== null) out.push(m[1]);
                  };

                  const sels = [
                    "[data-qa*='payout' i]",
                    "[data-qa*='profit' i]",
                    "[class*='payout' i]",
                    "[class*='profit' i]",
                    "[class*='return' i]",
                  ];
                  for (const s of sels) {
                    for (const el of document.querySelectorAll(s)) pushFrom(el);
                  }
                  for (const el of document.querySelectorAll("*")) {
                    const t = (el.textContent || "").toLowerCase();
                    if (!t.includes("payout")) continue;
                    pushFrom(el);
                    pushFrom(el.parentElement);
                  }
                  return out;
                }"""
            )
            if isinstance(values, list):
                parsed: list[float] = []
                for raw in values:
                    try:
                        v = float(str(raw).replace(",", "."))
                    except Exception:
                        continue
                    # Keep below 99 to avoid accidental "100% bonus" matches.
                    if 55.0 <= v < 99.0:
                        parsed.append(v)
                if parsed:
                    return max(parsed)
        except Exception:
            pass

        selectors = list(self.cfg.payout_selectors) + [
            ".profit-block .value",
            ".payout__value",
            "[data-qa='profit-percent']",
            "[data-qa='payout']",
            "[class*='payout']",
            "[class*='profit']",
        ]
        fallback_vals: list[float] = []
        for sel in selectors:
            try:
                loc = self._page.locator(sel)
                count = await loc.count()
                for i in range(min(count, 30)):
                    txt = await loc.nth(i).text_content(timeout=300)
                    if not txt or "bonus" in txt.lower():
                        continue
                    m = re.search(r"(\\d+(?:[\\.,]\\d+)?)\\s*%", txt)
                    if m:
                        v = float(m.group(1).replace(",", "."))
                        if 55.0 <= v < 99.0:
                            fallback_vals.append(v)
                            continue
                    digits = re.sub(r"[^\\d\\.,]", "", txt)
                    if digits:
                        v = float(digits.replace(",", "."))
                        if 55.0 <= v < 99.0:
                            fallback_vals.append(v)
            except Exception:
                continue
        if fallback_vals:
            return max(fallback_vals)
        return 80.0

    async def place_order(self, asset: str, amount: float, direction: str, expiry_sec: int) -> str:
        if self._page is None:
            raise RuntimeError("Browser adapter is not connected")
        # Minimal best-effort click flow. If selector mismatch occurs, user should update selectors.
        try:
            amount_input = self._page.locator(".block--bet-amount input, [data-qa='amount-input']").first
            await amount_input.click(timeout=500)
            await amount_input.fill(str(amount))
        except Exception:
            pass

        button_selector = ".btn-call" if direction == "CALL" else ".btn-put"
        alt_selector = "[data-qa='call-button']" if direction == "CALL" else "[data-qa='put-button']"
        clicked = False
        for sel in (button_selector, alt_selector):
            try:
                await self._page.locator(sel).first.click(timeout=800)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            raise RuntimeError("Could not click trade button. Update browser selectors.")

        order_id = f"browser-{int(time.time() * 1000)}"
        entry = await self._read_price()
        balance_before = await self.get_balance()
        self._orders[order_id] = {
            "direction": direction,
            "amount": float(amount),
            "expiry_sec": int(expiry_sec),
            "entry": entry,
            "open_ts": time.time(),
            "balance_before": balance_before,
        }
        return order_id

    @staticmethod
    def _classify_pnl_from_balance_delta(pnl: float, stake: float) -> tuple[str, bool]:
        """Map balance change to Pocket Option outcome (win / loss / draw)."""
        stake = max(0.01, float(stake))
        tol = max(0.02, stake * 0.05)
        if pnl > tol:
            return "win", True
        if pnl < -tol:
            return "loss", False
        return "draw", True

    @staticmethod
    def _prices_plausible_for_settlement(entry: float, exit: float) -> bool:
        """Reject DOM mis-reads (timers, payout %, etc.) when guessing from prices."""
        if entry <= 0 or exit <= 0:
            return False
        ref = max(abs(entry), abs(exit), 1e-9)
        return (abs(entry - exit) / ref) <= 0.25

    async def _read_balance_after_settle(self, polls: int = 5) -> float:
        """Re-read demo balance a few times after expiry so settlement is applied."""
        last = 0.0
        for _ in range(max(1, polls)):
            await self._page.wait_for_timeout(400)  # type: ignore[union-attr]
            last = await self.get_balance()
        return last

    async def _settle_from_balance(self, order: dict[str, Any]) -> dict[str, Any] | None:
        before = float(order.get("balance_before") or 0.0)
        if before <= 0:
            return None
        after = await self._read_balance_after_settle()
        if after <= 0:
            return None
        stake = float(order["amount"])
        pnl = round(after - before, 4)
        result, won_flag = self._classify_pnl_from_balance_delta(pnl, stake)
        return {
            "order_id": "",
            "result": result,
            "won": won_flag,
            "pnl": pnl if result == "draw" else pnl,
            "balance_before": before,
            "balance_after": after,
            "settlement_source": "balance",
        }

    async def _settle_from_price_guess(self, order: dict[str, Any], exit_price: float) -> dict[str, Any]:
        entry = float(order["entry"])
        direction = str(order["direction"])
        if not self._prices_plausible_for_settlement(entry, exit_price):
            return {
                "order_id": "",
                "result": "unknown",
                "won": False,
                "pnl": None,
                "entry": entry,
                "exit": exit_price,
                "settlement_source": "price_unreliable",
            }
        eps = max(1e-9, abs(entry) * 1e-6)
        if direction == "CALL":
            won = exit_price > entry + eps
            tie = abs(exit_price - entry) <= eps
        else:
            won = exit_price < entry - eps
            tie = abs(exit_price - entry) <= eps
        result = "draw" if tie else ("win" if won else "loss")
        won_flag = True if tie else won
        return {
            "order_id": "",
            "result": result,
            "won": won_flag,
            "entry": entry,
            "exit": exit_price,
            "pnl": 0.0 if tie else None,
            "settlement_source": "price_guess",
        }

    async def check_result(self, order_id: str, wait_sec: int) -> dict[str, Any]:
        order = self._orders.get(order_id)
        if not order:
            return {"order_id": order_id, "result": "unknown", "won": False}
        await self._page.wait_for_timeout((wait_sec + 1) * 1000)  # type: ignore[union-attr]

        settled = await self._settle_from_balance(order)
        if settled is None:
            try:
                exit_price = await self._read_price()
            except Exception:
                exit_price = 0.0
            settled = await self._settle_from_price_guess(order, exit_price)
        else:
            try:
                settled["exit"] = await self._read_price()
            except Exception:
                settled["exit"] = None
            settled["entry"] = order["entry"]

        settled["order_id"] = order_id
        return settled

    async def get_balance(self) -> float:
        if self._page is None:
            return 0.0
        selectors = [
            ".js-balance-real" if self.cfg.is_real_account else ".js-balance-demo",
            ".js-balance-demo",
            ".js-balance-real",
            ".balance .value",
            "[data-qa='balance']",
        ]
        for sel in selectors:
            try:
                txt = await self._page.locator(sel).first.text_content(timeout=300)
                if txt:
                    digits = "".join(ch for ch in txt if ch.isdigit() or ch in ".,")
                    return float(digits.replace(",", ""))
            except Exception:
                continue
        return 0.0

