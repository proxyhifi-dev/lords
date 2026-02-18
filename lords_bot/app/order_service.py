from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from lords_bot.app.fyers_client import FyersClient
from lords_bot.app.schemas import OrderRequest

trade_logger = logging.getLogger("lords_bot.trade")


class OrderService:
    """Order abstraction with option-chain selection, confirmation, cancel and modify support."""

    def __init__(self, client: FyersClient) -> None:
        self.client = client

    async def fetch_ltp(self, symbol: str) -> float:
        quote = await self.client.request("GET", "/quotes", params={"symbols": symbol})
        return float(quote.get("d", [{}])[0].get("v", {}).get("lp"))

    async def select_atm_option(self, direction: str, underlying: str = "NSE:NIFTY50-INDEX") -> dict[str, Any]:
        """Choose ATM call/put using optionchain endpoint; falls back gracefully if shape varies."""
        chain = await self.client.request("GET", "/optionchain", params={"symbol": underlying, "strikecount": 10})
        options = chain.get("data") or chain.get("options") or []
        if not options:
            raise RuntimeError("Option chain unavailable")

        underlying_ltp = await self.fetch_ltp(underlying)
        side_key = "CE" if direction.upper() == "CALL" else "PE"

        best = None
        best_dist = float("inf")
        for row in options:
            strike = row.get("strike_price") or row.get("strike")
            symbol = row.get("symbol") or row.get("symbol_name")
            typ = row.get("option_type") or ("CE" if symbol and symbol.endswith("CE") else "PE")
            if strike is None or not symbol or typ != side_key:
                continue
            dist = abs(float(strike) - underlying_ltp)
            if dist < best_dist:
                best_dist = dist
                best = {"symbol": symbol, "strike": float(strike), "ltp": float(row.get("ltp") or 0.0)}

        if not best:
            raise RuntimeError(f"ATM {side_key} option not found")
        if best["ltp"] <= 0:
            best["ltp"] = await self.fetch_ltp(best["symbol"])
        return best

    @staticmethod
    def build_sl_target(entry_price: float, sl_pct: float, target_pct: float) -> tuple[float, float]:
        """Premium based SL/target used consistently across UI preview and risk engine execution."""
        stop_loss = round(entry_price * (1 - max(0.01, sl_pct)), 2)
        target = round(entry_price * (1 + max(0.01, target_pct)), 2)
        return stop_loss, target

    async def place_order(self, payload: OrderRequest | dict[str, Any]) -> dict[str, Any]:
        order = payload if isinstance(payload, OrderRequest) else OrderRequest(**payload)
        trade_logger.info("Placing order: %s qty=%s side=%s", order.symbol, order.qty, order.side)
        return await self.client.request("POST", "/orders", data=order.model_dump(exclude_none=True))

    async def confirm_fill_price(self, symbol: str, order_response: dict[str, Any] | None = None) -> float:
        """Track fill from orders endpoint with retries; fallback to LTP prevents hard failures."""
        order_id = (order_response or {}).get("id") or (order_response or {}).get("order_id")
        if not order_id:
            return await self.fetch_ltp(symbol)

        for _ in range(5):
            try:
                details = await self.client.request("GET", "/orders", params={"id": order_id})
                rows = details.get("orderBook") or details.get("orders") or []
                for row in rows:
                    fp = row.get("tradedPrice") or row.get("avgPrice")
                    if fp:
                        return float(fp)
            except Exception as exc:  # noqa: BLE001
                trade_logger.warning("Fill lookup retry for %s: %s", order_id, exc)
            await asyncio.sleep(0.5)
        return await self.fetch_ltp(symbol)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        trade_logger.info("Cancelling order %s", order_id)
        return await self.client.request("DELETE", "/orders", data={"id": order_id})

    async def modify_order(self, order_id: str, qty: int | None = None, limit_price: float | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": order_id}
        if qty is not None:
            payload["qty"] = qty
        if limit_price is not None:
            payload["limitPrice"] = limit_price
        trade_logger.info("Modifying order %s with payload=%s", order_id, payload)
        return await self.client.request("PUT", "/orders", data=payload)

    async def place_orb_order(self, direction: str, qty: int, sl_pct: float, target_pct: float) -> dict[str, Any]:
        opt = await self.select_atm_option(direction)
        stop_loss, target = self.build_sl_target(opt["ltp"], sl_pct, target_pct)
        order = {
            "symbol": opt["symbol"],
            "qty": qty,
            "type": 2,
            "side": 1 if direction.upper() == "CALL" else -1,
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": stop_loss,
            "takeProfit": target,
            "orderTag": "ORB",
        }
        response = await self.place_order(order)
        fill = await self.confirm_fill_price(opt["symbol"], response)
        return {
            "order_response": response,
            "symbol": opt["symbol"],
            "entry_price": fill,
            "stop_loss": stop_loss,
            "target": target,
            "timestamp": dt.datetime.utcnow().isoformat(),
        }
