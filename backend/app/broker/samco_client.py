from __future__ import annotations
import asyncio
import json
import time
from typing import Any, Callable

from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge
from backend.app.core.config_loader import get_settings
from backend.app.core.circuit_breaker import CircuitBreaker
from backend.app.utils.logger import get_logger

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

        # Caches
        self._quote_cache: dict = {}
        self._quote_cache_ttl = 1       # seconds

        self._chain_cache: dict = {}
        self._chain_cache_ttl = 5       # seconds

    # --------------------------------------------------
    # LOGIN
    # --------------------------------------------------
    async def login(self) -> dict[str, Any]:
        async with self._lock:
            self.logger.info("Attempting SAMCO login")

            body: dict[str, Any] = {
                "userId":   settings.samco_user_id,
                "password": settings.samco_password,
                "yob":      settings.samco_yob,
            }

            # Include access token if provided (TOTP flow)
            if settings.samco_access_token:
                body["accessToken"] = settings.samco_access_token

            response = await asyncio.to_thread(self.samco.login, body=body)

            response = self._parse_response(response)

            if response.get("status") != "Success":
                raise RuntimeError(f"SAMCO login failed: {response}")

            session_token = response.get("sessionToken")
            if not session_token:
                raise RuntimeError("SAMCO login succeeded but sessionToken missing")

            await asyncio.to_thread(
                self.samco.set_session_token, sessionToken=session_token
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
    async def get_index_quote(self, index_name: str) -> dict:
        await self.ensure_session()
        return await self._call_sdk(
            lambda: self.samco.index_quote(indexName=index_name),
            "index_quote",
        )

    # --------------------------------------------------
    # GET QUOTE  (with 1-second cache)
    # --------------------------------------------------
    async def get_quote(self, symbol_name: str, exchange: str) -> dict:
        await self.ensure_session()

        cache_key = f"{symbol_name}_{exchange}"
        cached = self._quote_cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < self._quote_cache_ttl:
            return cached["data"]

        exch = self._map_exchange(exchange)

        result = await self._call_sdk(
            lambda: self.samco.get_quote(symbol_name=symbol_name, exchange=exch),
            "get_quote",
        )

        if not isinstance(result, dict):
            result = {}

        self._quote_cache[cache_key] = {"ts": time.time(), "data": result}

        self.logger.debug("get_quote symbol=%s response=%s", symbol_name, result)
        return result

    # --------------------------------------------------
    # OPTION CHAIN  (with 5-second cache)
    # --------------------------------------------------
    async def get_option_chain(
        self,
        search_symbol_name: str,
        exchange: str,
        expiry_date: str,
        strike_price: str,
        option_type: str,
    ) -> dict:
        await self.ensure_session()

        cache_key = f"{search_symbol_name}_{expiry_date}_{strike_price}_{option_type}"
        cached = self._chain_cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < self._chain_cache_ttl:
            return cached["data"]

        exch = self._map_exchange(exchange)

        self.logger.info(
            "Fetching option chain symbol=%s expiry=%s strike=%s type=%s",
            search_symbol_name, expiry_date, strike_price, option_type,
        )

        result = await self._call_sdk(
            lambda: self.samco.get_option_chain(
                search_symbol_name=search_symbol_name,
                exchange=exch,
                expiry_date=expiry_date,
                strike_price=strike_price,
                option_type=option_type,
            ),
            "get_option_chain",
        )

        if not isinstance(result, dict):
            result = {}

        self._chain_cache[cache_key] = {"ts": time.time(), "data": result}
        return result

    # --------------------------------------------------
    # PLACE ORDER
    # --------------------------------------------------
    async def place_order(self, symbol: str, side: str, quantity: int) -> dict:
        await self.ensure_session()

        txn_type = (
            self.samco.TRANSACTION_TYPE_BUY
            if side == "BUY"
            else self.samco.TRANSACTION_TYPE_SELL
        )

        response = await self._call_sdk(
            lambda: self.samco.place_order(
                body={
                    "symbolName":    symbol,
                    "exchange":      self.samco.EXCHANGE_NFO,
                    "transactionType": txn_type,
                    "orderType":     self.samco.ORDER_TYPE_MARKET,
                    "quantity":      str(quantity),
                    "productType":   self.samco.PRODUCT_MIS,
                    "orderValidity": self.samco.VALIDITY_DAY,
                }
            ),
            "place_order",
        )

        if response.get("status") != "Success":
            self.logger.error("Order rejected: %s", response)
        else:
            self.logger.info(
                "Order placed: side=%s symbol=%s qty=%s", side, symbol, quantity
            )

        return response

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
    # PARSE SPOT PRICE
    # --------------------------------------------------
    @staticmethod
    def parse_spot(quote: dict | None) -> float | None:
        if not quote:
            return None
        try:
            # Direct key
            for key in ("spotPrice",):
                val = quote.get(key)
                if val:
                    return float(str(val).replace(",", ""))

            # Nested indexDetails list
            details = quote.get("indexDetails")
            if isinstance(details, list) and details:
                val = details[0].get("spotPrice")
                if val:
                    return float(str(val).replace(",", ""))

            # Fallback to LTP
            for key in ("lastTradedPrice", "lastTradePrice"):
                val = quote.get(key)
                if val:
                    return float(str(val).replace(",", ""))

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

        keys = ["lastTradedPrice", "lastTradePrice", "ltp", "last_price"]

        def _extract(data: dict) -> float | None:
            for key in keys:
                val = data.get(key)
                if not val:
                    continue
                try:
                    val = float(str(val).replace(",", "").strip())
                    if val > 0:
                        return val
                except (ValueError, TypeError):
                    continue
            return None

        try:
            val = _extract(quote)
            if val:
                return val

            # Try nested data key
            data = quote.get("data")
            if isinstance(data, dict):
                return _extract(data)
            if isinstance(data, list) and data:
                return _extract(data[0])

        except Exception:
            return None
        return None

    # --------------------------------------------------
    # EXCHANGE MAPPER
    # --------------------------------------------------
    def _map_exchange(self, exchange: str) -> str:
        mapping = {
            "NFO": self.samco.EXCHANGE_NFO,
            "NSE": self.samco.EXCHANGE_NSE,
        }
        return mapping.get(exchange.upper(), exchange)

    # --------------------------------------------------
    # INTERNAL SDK WRAPPER  (retry + circuit breaker)
    # --------------------------------------------------
    async def _call_sdk(self, fn: Callable[[], Any], api_name: str) -> dict:

        if not self._breaker.allow_request():
            raise RuntimeError(f"Circuit breaker OPEN — skipping {api_name}")

        attempts = settings.reconnect_max_attempts
        delay = float(settings.reconnect_base_delay)

        for attempt in range(1, attempts + 1):
            try:
                result = await asyncio.to_thread(fn)
                self._breaker.record_success()

                return self._parse_response(result)

            except Exception as exc:
                self._breaker.record_failure()
                self.logger.error(
                    "SAMCO API error api=%s attempt=%s/%s error=%s",
                    api_name, attempt, attempts, exc,
                )

                if attempt < attempts:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60.0)

        return {}

    # --------------------------------------------------
    # RESPONSE PARSER  (handles str / dict / None)
    # --------------------------------------------------
    @staticmethod
    def _parse_response(result: Any) -> dict:
        if result is None:
            return {}
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            result = result.strip()
            if not result:
                return {}
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return {}
        return {}
