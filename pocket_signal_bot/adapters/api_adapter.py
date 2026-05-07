from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import asyncio


@dataclass
class PocketApiConfig:
    session: str
    uid: str
    is_demo: bool = True
    region: str = "DEMO"


class PocketOptionApiAdapter:
    """
    Unofficial API adapter.
    This wrapper is defensive: if SDK is not installed, it raises clear errors.
    """

    def __init__(self, cfg: PocketApiConfig):
        self.cfg = cfg
        self._client = None
        self._deals = None

    async def connect(self) -> None:
        try:
            from pocket_option import PocketOptionClient  # type: ignore
            from pocket_option.models import AuthorizationData  # type: ignore
            from pocket_option.contrib.deals import MemoryDealsStorage  # type: ignore
        except Exception as e:
            raise RuntimeError("Install unofficial SDK: pip install pocket-option") from e

        self._client = PocketOptionClient()
        regions = __import__("pocket_option.constants", fromlist=["Regions"]).Regions
        await self._client.connect(getattr(regions, self.cfg.region, regions.DEMO))
        auth = AuthorizationData.model_validate(
            {
                "session": self.cfg.session,
                "isDemo": 1 if self.cfg.is_demo else 0,
                "uid": int(self.cfg.uid),
                "platform": 2,
                "isFastHistory": True,
                "isOptimized": True,
            }
        )
        await self._client.emit.auth(auth)
        self._deals = MemoryDealsStorage(self._client)
        await asyncio.sleep(1)

    async def disconnect(self) -> None:
        if self._client:
            await self._client.disconnect()

    async def get_candles(self, asset: str, timeframe_sec: int, count: int) -> list[dict[str, Any]]:
        if self._client is None:
            raise RuntimeError("API adapter is not connected")
        method_names = ("get_candles", "candles", "history", "get_history")
        for name in method_names:
            method = getattr(self._client, name, None)
            if method is None:
                continue
            try:
                maybe = method(asset, timeframe_sec, count)  # type: ignore[misc]
                data = await maybe if asyncio.iscoroutine(maybe) else maybe
                if isinstance(data, list) and data:
                    normalized: list[dict[str, Any]] = []
                    for row in data[-count:]:
                        if isinstance(row, dict):
                            close = row.get("close") or row.get("c")
                            ts = row.get("time") or row.get("timestamp") or row.get("t")
                        else:
                            close = getattr(row, "close", None)
                            ts = getattr(row, "time", None) or getattr(row, "timestamp", None)
                        if close is None:
                            continue
                        normalized.append({"close": float(close), "time": ts})
                    if normalized:
                        return normalized
            except Exception:
                continue
        raise RuntimeError("Could not fetch candles from unofficial SDK. Check SDK version/method names.")

    async def get_payout_pct(self, asset: str) -> float:
        # SDK does not expose payout; return 80 so caller falls through to browser path.
        return 80.0

    async def place_order(self, asset: str, amount: float, direction: str, expiry_sec: int) -> str:
        if self._client is None or self._deals is None:
            raise RuntimeError("API adapter is not connected")
        try:
            from pocket_option.models import Asset, DealAction  # type: ignore
        except Exception as e:
            raise RuntimeError("Install unofficial SDK: pip install pocket-option") from e

        action = DealAction.CALL if direction == "CALL" else DealAction.PUT
        asset_enum = getattr(Asset, asset, None)
        if asset_enum is None:
            raise RuntimeError(f"Asset '{asset}' not found in SDK Asset enum")
        deal = await self._deals.open_deal(
            asset=asset_enum,
            amount=amount,
            action=action,
            is_demo=1 if self.cfg.is_demo else 0,
            option_type=100,
            time=expiry_sec,
        )
        return str(getattr(deal, "id", "")) or str(getattr(deal, "deal_id", ""))

    async def check_result(self, order_id: str, wait_sec: int) -> dict[str, Any]:
        if self._deals is None:
            await asyncio.sleep(wait_sec + 1)
            return {"order_id": order_id, "result": "unknown", "won": False}
        try:
            deal = type("DealObj", (), {"id": order_id})
            res = await self._deals.check_deal_result(wait_time=wait_sec, deal=deal)
            payload = res if isinstance(res, dict) else res.__dict__
            result = str(payload.get("result", payload.get("status", "unknown"))).lower()
            profit = float(payload.get("profit_amount", 0))
            won = result == "win" or profit > 0
            return {"order_id": order_id, "result": result, "won": won, "pnl": profit, "raw": payload}
        except Exception:
            await asyncio.sleep(wait_sec + 1)
            return {"order_id": order_id, "result": "unknown", "won": False}

    async def get_balance(self) -> float:
        if self._client is None:
            return 0.0
        try:
            for name in ("balance", "get_balance"):
                method = getattr(self._client, name, None)
                if method is None:
                    continue
                maybe = method()  # type: ignore[misc]
                bal = await maybe if asyncio.iscoroutine(maybe) else maybe
                if isinstance(bal, (int, float)):
                    return float(bal)
                if bal is not None:
                    return float(getattr(bal, "balance", 0.0))
            return 0.0
        except Exception:
            return 0.0
