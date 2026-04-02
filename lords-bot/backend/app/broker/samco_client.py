from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge

from backend.app.core.config_loader import get_settings
from backend.app.utils.logger import get_logger
from backend.app.core.circuit_breaker import CircuitBreaker


settings = get_settings()


class SamcoClient:

    """
    SAMCO Broker Adapter
    """

    def __init__(self):

        self.logger = get_logger("samco_client")

        self.samco = StocknoteAPIPythonBridge()

        self._session_live = False

        self._lock = asyncio.Lock()

        self._breaker = CircuitBreaker(
            failure_threshold=settings.circuit_failure_threshold,
            cooldown_seconds=settings.circuit_cooldown_seconds,
        )

    # --------------------------------------------------
    # LOGIN
    # --------------------------------------------------

    async def login(self) -> dict[str, Any]:

        async with self._lock:

            self.logger.info("Attempting SAMCO login")

            response = await asyncio.to_thread(
                self.samco.login,
                body={
                    "userId": settings.samco_user_id,
                    "password": settings.samco_password,
                    "yob": settings.samco_yob,
                    "accessToken": settings.samco_access_token,
                },
            )

            if isinstance(response, str):
                response = json.loads(response)

            if response.get("status") != "Success":
                raise RuntimeError(f"SAMCO login failed: {response}")

            session_token = response["sessionToken"]

            await asyncio.to_thread(
                self.samco.set_session_token,
                sessionToken=session_token,
            )

            self._session_live = True

            self.logger.info("SAMCO login successful")

            return response

    # --------------------------------------------------
    # SESSION CHECK
    # --------------------------------------------------

    async def ensure_session(self):

        if not self._session_live:
            await self.login()

    # --------------------------------------------------
    # INDEX QUOTE
    # --------------------------------------------------

    async def get_index_quote(self, index_name: str):

        await self.ensure_session()

        return await self._call_sdk(

            lambda: self.samco.index_quote(
                indexName=index_name
            ),

            "index_quote",
        )

    # --------------------------------------------------
    # QUOTE
    # --------------------------------------------------

    async def get_quote(self, symbol_name: str, exchange: str):

        await self.ensure_session()

        return await self._call_sdk(

            lambda: self.samco.get_quote(
                symbolName=symbol_name,
                exchange=exchange
            ),

            "get_quote",
        )

    # --------------------------------------------------
    # OPTION CHAIN
    # --------------------------------------------------

    async def get_option_chain(
        self,
        symbol_name: str,
        exchange: str,
        strike_price: int,
        expiry_date: str,
    ):

        await self.ensure_session()

        return await self._call_sdk(

            lambda: self.samco.get_option_chain(
                symbolName=symbol_name,
                exchange=exchange,
                strikePrice=str(strike_price),
                expiryDate=expiry_date
            ),

            "get_option_chain",
        )

    # --------------------------------------------------
    # PLACE ORDER
    # --------------------------------------------------

    async def place_order(self, symbol: str, side: str, quantity: int):

        await self.ensure_session()

        transaction_type = (
            self.samco.TRANSACTION_TYPE_BUY
            if side == "BUY"
            else self.samco.TRANSACTION_TYPE_SELL
        )

        return await self._call_sdk(

            lambda: self.samco.place_order(
                body={
                    "symbolName": symbol,
                    "exchange": self.samco.EXCHANGE_NFO,
                    "transactionType": transaction_type,
                    "orderType": self.samco.ORDER_TYPE_MARKET,
                    "quantity": str(quantity),
                    "productType": self.samco.PRODUCT_MIS,
                    "orderValidity": self.samco.VALIDITY_DAY,
                }
            ),

            "place_order",
        )

    # --------------------------------------------------
    # ORDER BOOK
    # --------------------------------------------------

    async def get_orders(self):

        await self.ensure_session()

        return await self._call_sdk(

            lambda: self.samco.get_order_book(),

            "get_order_book",
        )

    # --------------------------------------------------
    # POSITIONS (DISABLED SAFE MODE)
    # --------------------------------------------------

    async def get_positions(self):

        # SDK version doesn't support positions
        # return empty data to avoid crash

        return {"positions": []}

    # --------------------------------------------------
    # HEALTHCHECK
    # --------------------------------------------------

    async def healthcheck(self) -> bool:

        try:

            await self.get_index_quote("NIFTY 50")

            return True

        except Exception:

            return False

    # --------------------------------------------------
    # INTERNAL CALL
    # --------------------------------------------------

    async def _call_sdk(
        self,
        fn: Callable[[], Any],
        api_name: str,
        *,
        use_breaker: bool = True,
    ):

        if use_breaker and not self._breaker.allow_request():
            raise RuntimeError(f"Circuit breaker OPEN for {api_name}")

        attempts = settings.reconnect_max_attempts
        delay = settings.reconnect_base_delay

        last_error = None

        for attempt in range(1, attempts + 1):

            try:

                result = await asyncio.to_thread(fn)

                if isinstance(result, str):
                    result = json.loads(result)

                self._breaker.record_success()

                return result

            except Exception as exc:

                last_error = exc

                self._breaker.record_failure()

                self.logger.error(
                    "SAMCO API failed api=%s attempt=%s error=%s",
                    api_name,
                    attempt,
                    exc,
                )

                if attempt < attempts:

                    await asyncio.sleep(delay)

                    delay = min(delay * 2, 60)

                    if "session" in str(exc).lower():

                        self._session_live = False
                        await self.login()

        raise RuntimeError(
            f"{api_name} failed after retries: {last_error}"
        )