from __future__ import annotations

import datetime as dt
import logging

from lords_bot.app.fyers_client import FyersClient
from lords_bot.app.schemas import AutoSliceOrderRequest, MultiLegOrderRequest, MultiOrderRequest, OrderRequest

trade_logger = logging.getLogger("lords_bot.trade")


class OrderService:
    def __init__(self, client: FyersClient) -> None:
        self.client = client

    async def place_order(self, payload: OrderRequest | dict) -> dict:
        order = payload if isinstance(payload, OrderRequest) else OrderRequest(**payload)
        self._validate_tick_and_quantity(order)
        return await self.client.request("POST", "/orders/sync", data=order.model_dump(exclude_none=True))

    async def place_multi_order(self, payload: MultiOrderRequest | dict) -> dict:
        order = payload if isinstance(payload, MultiOrderRequest) else MultiOrderRequest(**payload)
        for item in order.orders:
            self._validate_tick_and_quantity(item)
        return await self.client.request(
            "POST",
            "/multi-order/sync",
            data=[item.model_dump(exclude_none=True) for item in order.orders],
        )

    async def place_multileg_order(self, payload: MultiLegOrderRequest | dict) -> dict:
        order = payload if isinstance(payload, MultiLegOrderRequest) else MultiLegOrderRequest(**payload)
        for leg in order.legs:
            self._validate_tick_and_quantity(leg)
        return await self.client.request("POST", "/multileg/orders/sync", data=order.model_dump(exclude_none=True))

    async def place_auto_slice_order(self, payload: AutoSliceOrderRequest | dict) -> dict:
        order = payload if isinstance(payload, AutoSliceOrderRequest) else AutoSliceOrderRequest(**payload)
        self._validate_tick_and_quantity(order)
        order_data = order.model_dump(exclude_none=True)
        order_data["autoslice"] = True
        return await self.client.request("POST", "/orders/sync", data=order_data)

    async def fetch_ltp(self, symbol: str) -> float:
        quote = await self.client.request("GET", "/quotes", params={"symbols": symbol})
        return float(quote["d"][0]["v"]["lp"])

    async def compute_dynamic_atm_strike(self) -> int:
        nifty_ltp = await self.fetch_ltp("NSE:NIFTY50-INDEX")
        return int(round(nifty_ltp / 50.0) * 50)

    async def confirm_fill_price(self, symbol: str, order_response: dict | None = None) -> float:
        if order_response:
            order_id = order_response.get("id") or order_response.get("order_id")
            if order_id:
                try:
                    details = await self.client.request("GET", "/orders", params={"id": order_id})
                    book = details.get("orderBook", []) or details.get("orders", [])
                    if book:
                        for candidate in book:
                            lp = candidate.get("tradedPrice") or candidate.get("avgPrice")
                            if lp:
                                return float(lp)
                except Exception as exc:
                    trade_logger.warning("Order fill lookup failed for %s: %s", order_id, exc)
        return await self.fetch_ltp(symbol)

    @staticmethod
    def build_sl_target(entry_price: float, sl_pct: float, rr_ratio: float = 1.5) -> tuple[float, float]:
        sl = round(entry_price * (1 - sl_pct), 2)
        risk = max(entry_price - sl, 0.05)
        target = round(entry_price + (risk * max(rr_ratio, 1.5)), 2)
        return sl, target

    async def simulate_paper_fill(self, symbol: str) -> dict:
        fill = await self.fetch_ltp(symbol)
        return {
            "status": "simulated",
            "symbol": symbol,
            "fill_price": fill,
            "timestamp": dt.datetime.utcnow().isoformat(),
        }

    @staticmethod
    def _validate_tick_and_quantity(order: OrderRequest) -> None:
        if order.qty <= 0:
            raise ValueError("Quantity must be greater than zero")
        if order.limitPrice < 0 or order.stopPrice < 0:
            raise ValueError("Price values cannot be negative")
