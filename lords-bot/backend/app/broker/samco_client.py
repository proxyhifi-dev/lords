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

            body = {
                "userId": settings.samco_user_id,
                "password": settings.samco_password,
                "yob": settings.samco_yob,
            }

            if getattr(settings, "samco_access_token", None):
                body["accessToken"] = settings.samco_access_token

            response = await asyncio.to_thread(
                self.samco.login,
                body=body,
            )

            if isinstance(response, str):

                response = response.strip()

                if not response:
                    raise RuntimeError("Empty SAMCO login response")

                response = json.loads(response)

            if response.get("status") != "Success":
                raise RuntimeError(f"SAMCO login failed: {response}")

            session_token = response.get("sessionToken")

            if not session_token:
                raise RuntimeError("SAMCO login succeeded but sessionToken missing")

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
            lambda: self.samco.index_quote(indexName=index_name),
            "index_quote",
        )

    # --------------------------------------------------
    # GET QUOTE
    # --------------------------------------------------

    async def get_quote(self, symbol_name: str, exchange: str):

        await self.ensure_session()

        if exchange == "NFO":
            exchange = self.samco.EXCHANGE_NFO
        elif exchange == "NSE":
            exchange = self.samco.EXCHANGE_NSE

        result = await self._call_sdk(
            lambda: self.samco.get_quote(
                symbol_name=symbol_name,
                exchange=exchange,
            ),
            "get_quote",
        )

        self.logger.debug(
            "get_quote raw response symbol=%s response=%s",
            symbol_name,
            result,
        )

        return result

    # --------------------------------------------------
    # OPTION CHAIN
    # --------------------------------------------------

    async def get_option_chain(
        self,
        search_symbol_name: str,
        exchange: str,
        expiry_date: str,
        strike_price: str,
        option_type: str,
    ):

        await self.ensure_session()

        if exchange == "NFO":
            exchange = self.samco.EXCHANGE_NFO
        elif exchange == "NSE":
            exchange = self.samco.EXCHANGE_NSE

        return await self._call_sdk(
            lambda: self.samco.get_option_chain(
                search_symbol_name=search_symbol_name,
                exchange=exchange,
                expiry_date=expiry_date,
                strike_price=strike_price,
                option_type=option_type,
            ),
            "get_option_chain",
        )

    # --------------------------------------------------
    # PARSE SPOT
    # --------------------------------------------------

    @staticmethod
    def parse_spot(quote: dict | None) -> float | None:

        if not quote:
            return None

        try:

            if "spotPrice" in quote:
                price = quote.get("spotPrice")
                if price:
                    return float(str(price).replace(",", ""))

            details = quote.get("indexDetails")

            if isinstance(details, list) and details:
                price = details[0].get("spotPrice")
                if price:
                    return float(str(price).replace(",", ""))

            if "lastTradedPrice" in quote:
                price = quote.get("lastTradedPrice")
                if price:
                    return float(str(price).replace(",", ""))

        except Exception:
            return None

        return None

    # --------------------------------------------------
    # PARSE LTP
    # --------------------------------------------------

    @staticmethod
    def parse_ltp(quote: dict | None) -> float | None:

        if not quote:
            return None

        keys = [
            "lastTradedPrice",
            "lastTradePrice",
            "ltp",
            "last_price",
        ]

        try:

            def extract(data):

                for key in keys:

                    if key not in data:
                        continue

                    val = data[key]

                    if not val:
                        continue

                    val = str(val).replace(",", "").strip()

                    try:
                        return float(val)
                    except:
                        continue

                return None

            val = extract(quote)

            if val:
                return val

            data = quote.get("data")

            if isinstance(data, dict):
                return extract(data)

            if isinstance(data, list) and data:
                return extract(data[0])

        except Exception:
            return None

        return None

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
    # HEALTHCHECK
    # --------------------------------------------------

    async def healthcheck(self) -> bool:

        try:
            await self.get_index_quote("NIFTY 50")
            return True
        except Exception:
            return False

    # --------------------------------------------------
    # INTERNAL SDK WRAPPER
    # --------------------------------------------------

    async def _call_sdk(self, fn: Callable[[], Any], api_name: str):

        if not self._breaker.allow_request():
            raise RuntimeError(f"Circuit breaker open — skipping {api_name}")

        attempts = settings.reconnect_max_attempts
        delay = settings.reconnect_base_delay

        for attempt in range(1, attempts + 1):

            try:

                result = await asyncio.to_thread(fn)

                self._breaker.record_success()

                if result is None:
                    return {}

                if isinstance(result, str):

                    result = result.strip()

                    if not result:
                        return {}

                    try:
                        result = json.loads(result)
                    except json.JSONDecodeError:
                        return {}

                return result

            except Exception as exc:

                self._breaker.record_failure()

                self.logger.error(
                    "SAMCO API failed api=%s attempt=%s/%s error=%s",
                    api_name,
                    attempt,
                    attempts,
                    exc,
                )

                if attempt < attempts:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)

        return {}