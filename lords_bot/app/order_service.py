from __future__ import annotations

from lords_bot.app.fyers_client import FyersClient
from lords_bot.app.schemas import AutoSliceOrderRequest, MultiLegOrderRequest, MultiOrderRequest, OrderRequest


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

    @staticmethod
    def _validate_tick_and_quantity(order: OrderRequest) -> None:
        if order.qty <= 0:
            raise ValueError("Quantity must be greater than zero")
        if order.limitPrice < 0 or order.stopPrice < 0:
            raise ValueError("Price values cannot be negative")
