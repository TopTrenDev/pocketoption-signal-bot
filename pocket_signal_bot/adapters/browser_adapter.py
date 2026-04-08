from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from collections import deque
import json
import time


@dataclass
class BrowserConfig:
    headless: bool = True
    base_url: str = "https://pocketoption.com/en/cabinet/demo-quick-high-low/"
    is_real_account: bool = False
    price_selectors: tuple[str, ...] = ()
    startup_wait_ms: int = 5000
    quote_asset: str = "EURUSD_otc"
    use_ws_quotes: bool = True


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

    @staticmethod
    def _normalize_pair(asset: str) -> str:
        a = (asset or "").upper().replace("_OTC", "").replace("-OTC", "").replace("/", "").replace("-", "")
        return a

    def _on_websocket(self, ws: Any) -> None:
        def on_frame(frame: Any) -> None:
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
                    self._ingest_ws_payload(text)
            except Exception:
                pass

        try:
            ws.on("framereceived", on_frame)
        except Exception:
            pass

    def _walk_quote_json(self, obj: Any, sym: str) -> float | None:
        """Find a plausible last price for sym inside nested JSON."""
        sym_u = self._normalize_pair(sym)
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
        elif isinstance(obj, list):
            for item in obj:
                r = self._walk_quote_json(item, sym)
                if r is not None:
                    return r
        return None

    def _ingest_ws_payload(self, text: str) -> None:
        if not self.cfg.use_ws_quotes or len(text) < 2:
            return
        sym = self.cfg.quote_asset
        for prefix in ("42", "43", "44"):
            if not text.startswith(prefix):
                continue
            body = text[len(prefix) :].strip()
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, list) or not payload:
                continue
            evt = payload[0]
            data = payload[1] if len(payload) > 1 else None
            if isinstance(evt, str):
                el = evt.lower()
                if any(k in el for k in ("quote", "price", "tick", "rate", "candle")):
                    p = self._walk_quote_json(data, sym)
                    if p is not None:
                        self._record_ws_price(p)
                        return
            p = self._walk_quote_json(payload, sym)
            if p is not None:
                self._record_ws_price(p)
                return

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
        selectors = [
            ".profit-block .value",
            ".payout__value",
            "[data-qa='profit-percent']",
        ]
        for sel in selectors:
            try:
                txt = await self._page.locator(sel).first.text_content(timeout=250)
                if not txt:
                    continue
                digits = "".join(ch for ch in txt if ch.isdigit() or ch == ".")
                if digits:
                    return float(digits)
            except Exception:
                continue
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
        self._orders[order_id] = {
            "direction": direction,
            "amount": float(amount),
            "expiry_sec": int(expiry_sec),
            "entry": entry,
            "open_ts": time.time(),
        }
        return order_id

    async def check_result(self, order_id: str, wait_sec: int) -> dict[str, Any]:
        order = self._orders.get(order_id)
        if not order:
            return {"order_id": order_id, "result": "unknown", "won": False}
        await self._page.wait_for_timeout((wait_sec + 1) * 1000)  # type: ignore[union-attr]
        exit_price = await self._read_price()
        direction = order["direction"]
        if direction == "CALL":
            won = exit_price > order["entry"]
            tie = exit_price == order["entry"]
        else:
            won = exit_price < order["entry"]
            tie = exit_price == order["entry"]
        result = "draw" if tie else ("win" if won else "loss")
        return {"order_id": order_id, "result": result, "won": won, "entry": order["entry"], "exit": exit_price}

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

