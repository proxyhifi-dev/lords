from __future__ import annotations

import asyncio
from typing import Any, Callable

from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge

from backend.app.core.circuit_breaker import CircuitBreaker
from backend.app.utils.logger import get_logger
from backend.config import settings


class SamcoClient:
    """Official SAMCO SDK adapter with reconnect and circuit breaker protection."""

    def __init__(self) -> None:
        self.logger = get_logger("samco_client")
        self.samco = StocknoteAPIPythonBridge()
        self._session_live = False
        self._lock = asyncio.Lock()
        self._breaker = CircuitBreaker(
            failure_threshold=settings.circuit_failure_threshold,
            cooldown_seconds=settings.circuit_cooldown_seconds,
        )

    async def login(self) -> dict[str, Any]:
        async with self._lock:
            response = await self._call_sdk(
                lambda: self.samco.login(
                    body={
                        "userId": settings.samco_user_id,
                        "password": settings.samco_password,
                        "yob": settings.samco_yob,
                    }
                ),
                "login",
                use_breaker=False,
            )
            session_token = response.get("sessionToken") or response.get("sessionToken")
            if not session_token:
                raise RuntimeError(f"SAMCO login failed: missing sessionToken in {response}")
            await asyncio.to_thread(self.samco.set_session_token, session_token)
            self._session_live = True
            self.logger.info("SAMCO login successful")
            return response

    async def ensure_session(self) -> None:
        if not self._session_live:
            await self.login()

    async def get_quote(self, symbol_name: str, exchange: str) -> dict[str, Any]:
        await self.ensure_session()
        return await self._call_sdk(lambda: self.samco.get_quote(symbol_name=symbol_name, exchange=exchange), "get_quote")

    async def get_option_chain(
        self,
        symbol_name: str,
        exchange: str,
        strike_price: int,
        expiry_date: str,
    ) -> dict[str, Any]:
        await self.ensure_session()
        return await self._call_sdk(
            lambda: self.samco.get_option_chain(
                symbol_name=symbol_name,
                exchange=exchange,
                strike_price=strike_price,
                expiry_date=expiry_date,
            ),
            "get_option_chain",
        )

    async def place_order(self, symbol: str, side: str, quantity: int) -> dict[str, Any]:
        await self.ensure_session()
        transaction_type = self.samco.TRANSACTION_TYPE_BUY if side == "BUY" else self.samco.TRANSACTION_TYPE_SELL
        return await self._call_sdk(
            lambda: self.samco.place_order(
                body={
                    "symbolName": symbol,
                    "exchange": self.samco.EXCHANGE_NFO,
                    "transactionType": transaction_type,
                    "orderType": self.samco.ORDER_TYPE_MARKET,
                    "quantity": str(quantity),
                    "disclosedQuantity": "",
                    "orderValidity": self.samco.VALIDITY_DAY,
                    "productType": self.samco.PRODUCT_MIS,
                    "afterMarketOrderFlag": "NO",
                }
            ),
            "place_order",
        )

    async def get_orders(self) -> dict[str, Any]:
        await self.ensure_session()
        return await self._call_sdk(lambda: self.samco.get_order_book(), "get_order_book")

    async def get_positions(self) -> dict[str, Any]:
        await self.ensure_session()
        return await self._call_sdk(lambda: self.samco.get_positions(), "get_positions")

    async def healthcheck(self) -> bool:
        try:
            await self.get_positions()
            return True
        except Exception:
            return False

    async def _call_sdk(
        self,
        fn: Callable[[], dict[str, Any]],
        api_name: str,
        *,
        use_breaker: bool = True,
    ) -> dict[str, Any]:
        if use_breaker and not self._breaker.allow_request():
            raise RuntimeError(f"Circuit OPEN: blocked {api_name}")

        attempts = settings.reconnect_max_attempts
        delay = settings.reconnect_base_delay
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                result = await asyncio.to_thread(fn)
                self._breaker.record_success()
                return result
            except Exception as exc:
                last_error = exc
                self._breaker.record_failure()
                self.logger.error("API call failed api=%s attempt=%s error=%s", api_name, attempt, exc)
                if attempt < attempts:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
                    if "session" in str(exc).lower() or "token" in str(exc).lower():
                        self._session_live = False
                        await self.login()

        raise RuntimeError(f"{api_name} failed after retries: {last_error}")
