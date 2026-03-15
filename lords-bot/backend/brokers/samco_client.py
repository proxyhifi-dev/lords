from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from threading import Lock
from typing import Any, Callable

from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge
from config import settings

logger = logging.getLogger(__name__)


class SamcoClient:
    @staticmethod
    def to_expiry_code(expiry: str) -> str:
        if not expiry:
            raise ValueError('Expiry cannot be empty')
        expiry = expiry.strip().upper()
        if '-' in expiry:
            dt = datetime.strptime(expiry, '%Y-%m-%d')
            return dt.strftime('%d%b%y').upper()
        if len(expiry) == 9:
            return expiry
        if len(expiry) == 11:  # e.g. 26MAR2026
            dt = datetime.strptime(expiry, '%d%b%Y')
            return dt.strftime('%d%b%y').upper()
        return expiry

    @staticmethod
    def to_expiry_api_date(expiry: str) -> str:
        if not expiry:
            raise ValueError('Expiry cannot be empty')
        expiry = expiry.strip().upper()
        if '-' in expiry:
            datetime.strptime(expiry, '%Y-%m-%d')
            return expiry
        if len(expiry) == 9:
            dt = datetime.strptime(expiry, '%d%b%y')
            return dt.strftime('%Y-%m-%d')
        dt = datetime.strptime(expiry, '%d%b%Y')
        return dt.strftime('%Y-%m-%d')

    def __init__(self) -> None:
        self.samco = StocknoteAPIPythonBridge()
        self._token = settings.samco_session_token
        self._authenticated = bool(self._token)
        self._lock = Lock()
        if self._token:
            try:
                self.samco.set_session_token(sessionToken=self._token)
                logger.info('Samco session token restored')
            except Exception:
                logger.warning('Stored session token invalid')

    def login(self, force: bool = False) -> bool:
        if self._authenticated and not force:
            return True
        with self._lock:
            if self._authenticated and not force:
                return True
            if not all([settings.samco_user_id, settings.samco_password, settings.samco_yob]):
                logger.warning('Samco credentials missing')
                return False
            try:
                response = self.samco.login(body={'userId': settings.samco_user_id, 'password': settings.samco_password, 'yob': settings.samco_yob})
            except Exception as exc:
                logger.error('Samco login exception err=%s', exc)
                self._authenticated = False
                return False
            if isinstance(response, str):
                try:
                    response = json.loads(response)
                except Exception:
                    logger.error('Invalid login response: %s', response)
                    return False
            status = str(response.get('status', '')).strip()
            if status != 'Success':
                logger.error('Samco login failed response=%s', response)
                self._authenticated = False
                return False
            token = response.get('sessionToken') or response.get('session_token')
            if not token:
                logger.error('Session token missing in login response')
                self._authenticated = False
                return False
            self.set_session_token(str(token))
            self._authenticated = True
            logger.info('Samco login successful')
            return True

    def set_session_token(self, token: str) -> None:
        self._token = token
        self.samco.set_session_token(sessionToken=token)

    def _needs_relogin(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        message = str(payload.get('statusMessage', '')).lower()
        return payload.get('status') != 'Success' and any(word in message for word in ('session', 'token', 'auth'))

    def _call(self, fn: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
        if not self._authenticated:
            self.login()
        retries = max(0, settings.max_api_retries)
        delay = settings.base_retry_delay_seconds
        for attempt in range(retries + 1):
            try:
                response = fn(**kwargs)
            except Exception as exc:
                logger.warning('Samco API failed attempt=%s err=%s', attempt + 1, exc)
                response = {'status': 'Error', 'statusMessage': str(exc)}
            if isinstance(response, str):
                try:
                    response = json.loads(response)
                except Exception:
                    response = {'status': 'Error', 'statusMessage': response}
            if isinstance(response, dict) and response.get('status') == 'Success':
                time.sleep(0.2)
                return response
            if self._needs_relogin(response):
                logger.warning('Samco session expired — relogin')
                self._authenticated = False
                self.login(force=True)
                continue
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
        return response if isinstance(response, dict) else {'status': 'Error', 'statusMessage': 'Invalid response'}

    async def index_quote(self, index_name: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._call, self.samco.index_quote, exchange='NSE', indexName=index_name)

    async def get_option_chain(self, symbol: str, expiry: str) -> dict[str, Any]:
        expiry_date = self.to_expiry_api_date(expiry)
        return await asyncio.to_thread(
            self._call,
            self.samco.get_option_chain,
            search_symbol_name=symbol,
            exchange=self.samco.EXCHANGE_NFO,
            expiry_date=expiry_date,
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
