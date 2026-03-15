from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from threading import Lock
from typing import Any, Callable

from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge

from config import settings

logger = logging.getLogger(__name__)


class SamcoClient:
    """Thin resiliency wrapper around StocknoteAPIPythonBridge."""

    @staticmethod
    def to_expiry_code(expiry: str) -> str:
        return datetime.strptime(expiry, '%Y-%m-%d').strftime('%d%b%Y').upper()

    def __init__(self) -> None:
        self.samco = StocknoteAPIPythonBridge()
        self._token = settings.samco_session_token
        self._authenticated = bool(self._token)
        self._lock = Lock()
        if self._token:
            self.samco.set_session_token(sessionToken=self._token)

    def login(self, force: bool = False) -> bool:
        if self._authenticated and not force:
            return True

        with self._lock:
            if self._authenticated and not force:
                return True

            if not all([settings.samco_user_id, settings.samco_password, settings.samco_yob]):
                logger.warning('samco credentials missing; client remains unauthenticated')
                return False

            try:
                response = self.samco.login(
                    body={
                        'userId': settings.samco_user_id,
                        'password': settings.samco_password,
                        'yob': settings.samco_yob,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.error('samco login exception err=%s', exc)
                self._authenticated = False
                return False
            if not isinstance(response, dict) or response.get('status') != 'Success':
                logger.error('samco login failed response=%s', response)
                self._authenticated = False
                return False

            token = response.get('sessionToken') or response.get('session_token')
            if not token:
                self._authenticated = False
                return False

            self.set_session_token(str(token))
            self._authenticated = True
            return True

    def set_session_token(self, session_token: str) -> None:
        self._token = session_token
        self.samco.set_session_token(sessionToken=session_token)

    def _needs_relogin(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        message = str(payload.get('statusMessage', '')).lower()
        return payload.get('status') != 'Success' and any(word in message for word in ('session', 'token', 'auth'))

    def _call(self, fn: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
        self.login()
        retries = max(0, settings.max_api_retries)
        delay = settings.base_retry_delay_seconds

        for attempt in range(retries + 1):
            try:
                response = fn(**kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.warning('samco call failed attempt=%s err=%s', attempt + 1, exc)
                response = {'status': 'Error', 'statusMessage': str(exc)}

            if isinstance(response, dict) and response.get('status') == 'Success':
                return response

            if self._needs_relogin(response):
                self._authenticated = False
                self.login(force=True)
                continue

            if attempt < retries:
                time.sleep(delay)
                delay *= 2

        return response if isinstance(response, dict) else {'status': 'Error', 'statusMessage': 'Invalid response'}

    async def index_quote(self, index_name: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._call, self.samco.index_quote, index_name=index_name)

    async def get_option_chain(self, symbol: str, expiry: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._call,
            self.samco.get_option_chain,
            search_symbol_name=symbol,
            exchange=self.samco.EXCHANGE_NFO,
            expiry_date=expiry,
        )

    async def place_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._call, self.samco.place_order, body=order_payload)

    async def get_limits(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._call, self.samco.get_limits)

    async def user_details(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._call, self.samco.user_details)

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._call, self.samco.get_order_status, order_id=order_id)


samco_client = SamcoClient()
