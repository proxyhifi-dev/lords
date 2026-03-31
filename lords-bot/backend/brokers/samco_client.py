from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge
from config import settings

logger = logging.getLogger("core.logger")


class SamcoClient:

    SESSION_FILE = Path(__file__).resolve().parents[1] / ".session"

    def __init__(self) -> None:

        self.samco = StocknoteAPIPythonBridge()

        self._lock = Lock()
        self._authenticated = False
        self._token = ""

        self._bootstrap_session()

    # -------------------------------------------------
    # Expiry helpers
    # -------------------------------------------------

    @staticmethod
    def to_expiry_code(expiry: str) -> str:

        expiry = expiry.strip().upper()

        if "-" in expiry:
            return datetime.strptime(expiry, "%Y-%m-%d").strftime("%d%b%y").upper()

        for fmt in ("%d%b%Y", "%d%b%y"):
            try:
                dt = datetime.strptime(expiry, fmt)
                return dt.strftime("%d%b%y").upper()
            except ValueError:
                pass

        raise ValueError(f"Unsupported expiry format: {expiry}")

    @staticmethod
    def to_expiry_api_date(expiry: str) -> str:

        expiry = expiry.strip().upper()

        if "-" in expiry:
            datetime.strptime(expiry, "%Y-%m-%d")
            return expiry

        for fmt in ("%d%b%Y", "%d%b%y"):
            try:
                return datetime.strptime(expiry, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass

        raise ValueError(f"Unsupported expiry format: {expiry}")

    # -------------------------------------------------
    # Session bootstrap
    # -------------------------------------------------

    def _bootstrap_session(self) -> None:

        if self._load_session():
            self._authenticated = True
            logger.info("Samco session restored from file")
            return

        self.login()

    def _load_session(self) -> bool:

        if not self.SESSION_FILE.exists():
            return False

        try:

            payload = json.loads(self.SESSION_FILE.read_text())

            token = payload.get("access_token")

            if not token:
                return False

            self.set_session_token(token, persist=False)

            return True

        except Exception:
            return False

    def _save_session(self) -> None:

        if not self._token:
            return

        try:
            self.SESSION_FILE.write_text(json.dumps({"access_token": self._token}))
        except Exception:
            pass

    def set_session_token(self, token: str, persist: bool = True):

        self._token = token

        try:
            self.samco.set_session_token(sessionToken=token)
        except Exception:
            pass

        if persist:
            self._save_session()

    # -------------------------------------------------
    # Login
    # -------------------------------------------------

    def login(self) -> bool:

        with self._lock:

            try:

                response = self.samco.login(
                    body={
                        "userId": settings.samco_user_id,
                        "password": settings.samco_password,
                        "yob": settings.samco_yob,
                    }
                )

                payload = self._parse_response(response)

                if payload.get("status", "").lower() != "success":

                    logger.error("Samco login failed")

                    return False

                token = payload.get("sessionToken") or payload.get("accessToken")

                if not token:

                    logger.error("Samco token missing")

                    return False

                self.set_session_token(token)

                self._authenticated = True

                logger.info("Samco login successful")

                return True

            except Exception as e:

                logger.error("Samco login error: %s", e)

                return False

    # -------------------------------------------------
    # Utilities
    # -------------------------------------------------

    def _parse_response(self, response: Any) -> dict[str, Any]:

        if isinstance(response, dict):
            return response

        if isinstance(response, str):

            try:
                return json.loads(response)
            except Exception:
                return {"status": "Error", "message": response}

        return {"status": "Error", "message": "Invalid response"}

    def _call(self, fn: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:

        retries = settings.max_api_retries
        delay = settings.base_retry_delay_seconds

        for attempt in range(retries + 1):

            try:

                response = fn(**kwargs)

                payload = self._parse_response(response)

                if payload.get("status", "").lower() == "success":

                    return payload

            except Exception as e:

                logger.error("Samco SDK call failed: %s", e)

            time.sleep(delay)

            delay *= 2

        return {"status": "Error", "message": "API call failed"}

    async def _run_io(self, fn: Callable[..., Any], **kwargs: Any):

        timeout = settings.request_timeout

        try:

            return await asyncio.wait_for(
                asyncio.to_thread(self._call, fn, **kwargs),
                timeout=timeout,
            )

        except asyncio.TimeoutError:

            logger.error("Samco API timeout")

            return {"status": "Error", "message": "timeout"}

    # -------------------------------------------------
    # Market APIs
    # -------------------------------------------------

    async def index_quote(self, index_name: str):

        return await self._run_io(
            self.samco.index_quote,
            exchange="NSE",
            indexName=index_name,
        )

    async def get_quote(self, symbol_name: str):

        return await self._run_io(
            self.samco.get_quote,
            symbol_name=symbol_name,
            exchange="NSE",
        )

    async def get_positions(self):

        return await self._run_io(self.samco.get_positions)

    async def get_option_chain(
        self,
        symbol: str,
        expiry: str,
        strike_price: str | None = None,
    ):

        payload = {
            "search_symbol_name": symbol,
            "exchange": self.samco.EXCHANGE_NFO,
            "expiry_date": self.to_expiry_api_date(expiry),
        }

        if strike_price:
            payload["strike_price"] = strike_price

        return await self._run_io(self.samco.get_option_chain, **payload)

    # -------------------------------------------------
    # Orders
    # -------------------------------------------------

    async def place_order(
        self,
        symbol_name: str,
        quantity: int,
        transaction_type: str,
        order_type: str = "MARKET",
        exchange: str = "NFO",
        product_type: str = "INTRADAY",
        price: float = 0,
    ):

        body = {
            "symbolName": symbol_name,
            "exchange": exchange,
            "transactionType": transaction_type,
            "orderType": order_type,
            "quantity": quantity,
            "productType": product_type,
            "price": price,
        }

        return await self._run_io(self.samco.place_order, body=body)

    async def get_limits(self):

        return await self._run_io(self.samco.get_limits)

    async def get_order_status(self, order_id: str):

        return await self._run_io(self.samco.get_order_status, order_id=order_id)


samco_client = SamcoClient()