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
    @staticmethod
    def to_expiry_code(expiry: str) -> str:
        return datetime.strptime(expiry, '%Y-%m-%d').strftime('%d%b%Y').upper()

    @staticmethod
    def to_expiry_dash(expiry: str) -> str:
        return datetime.strptime(expiry, '%Y-%m-%d').strftime('%d-%m-%Y')

    def __init__(self) -> None:
        self.samco = StocknoteAPIPythonBridge()
        self._session_token = settings.samco_session_token
        self._authenticated = bool(self._session_token)
        self._auth_lock = Lock()
        self._last_login_epoch = 0.0
        if self._session_token:
            self.samco.set_session_token(sessionToken=self._session_token)

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    def _credentials_present(self) -> bool:
        return all([settings.samco_user_id, settings.samco_password, settings.samco_yob])

    def authenticate(self, force: bool = False) -> bool:
        if self._authenticated and not force:
            return True
        if not self._credentials_present():
            logger.error('samco credentials missing; set SAMCO_USER_ID, SAMCO_PASSWORD, and SAMCO_YOB')
            return False

        with self._auth_lock:
            if self._authenticated and not force:
                return True

            # prevent repeated login bursts when multiple consumers fail together
            now = time.time()
            if not force and now - self._last_login_epoch < 2:
                return self._authenticated

            self._last_login_epoch = now
            logger.info('attempting samco login for user=%s', settings.samco_user_id)
            try:
                login_response = self.samco.login(
                    body={
                        'userId': settings.samco_user_id,
                        'password': settings.samco_password,
                        'yob': settings.samco_yob,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.error('exception during samco authentication: %s', exc)
                self._authenticated = False
                return False

            if not isinstance(login_response, dict):
                logger.error('samco login returned invalid payload=%s', login_response)
                self._authenticated = False
                return False

            if login_response.get('status') != 'Success':
                logger.error('samco login failed: %s', login_response.get('statusMessage'))
                self._authenticated = False
                return False

            session_token = login_response.get('sessionToken') or login_response.get('session_token')
            if not session_token:
                logger.error('samco login did not return session token')
                self._authenticated = False
                return False

            self.samco.set_session_token(sessionToken=session_token)
            self._session_token = str(session_token)
            self._authenticated = True
            logger.info('samco login successful and session token updated')
            return True

    def _needs_reauth(self, response: Any) -> bool:
        if not isinstance(response, dict):
            return False
        message = str(response.get('statusMessage', '')).lower()
        return response.get('status') != 'Success' and any(k in message for k in ('session', 'token', 'unauthor'))

    def _call_with_retry(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if not self.authenticate():
            return {}

        retries = max(0, settings.max_api_retries)
        delay = max(0.1, settings.base_retry_delay_seconds)
        response: Any = {}

        for attempt in range(retries + 1):
            try:
                response = func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.warning('samco api call failed attempt=%s err=%s', attempt + 1, exc)
                response = {'status': 'Error', 'statusMessage': str(exc)}

            if isinstance(response, dict) and response.get('status') == 'Success':
                return response

            if self._needs_reauth(response):
                self._authenticated = False
                if self.authenticate(force=True):
                    continue

            if attempt < retries:
                time.sleep(delay)
                delay *= 2

        return response

    def get_nifty_spot(self) -> float | None:
        response = self._call_with_retry(self.samco.index_quote, 'NIFTY 50')
        if response and response.get('status') == 'Success':
            try:
                details = response.get('indexDetails') or response.get('data') or []
                row = details[0] if details else {}
                return float(row.get('spotPrice') or row.get('ltp') or 0)
            except (TypeError, ValueError, KeyError, IndexError) as exc:
                logger.error('failed to parse NIFTY spot price: %s | response=%s', exc, response)
                return None

        logger.error('api error fetching NIFTY spot: %s', response)
        return None

    async def get_underlying_price(self, symbol: str) -> float:
        if symbol.upper() in {'NIFTY', 'NIFTY 50'}:
            spot = await asyncio.to_thread(self.get_nifty_spot)
            return float(spot or 0.0)

        response = await asyncio.to_thread(self._call_with_retry, self.samco.index_quote, symbol)
        if response and response.get('status') == 'Success':
            try:
                details = response.get('indexDetails') or response.get('data') or []
                row = details[0] if details else {}
                return float(row.get('spotPrice') or row.get('ltp') or 0)
            except (TypeError, ValueError, KeyError, IndexError):
                return 0.0
        return 0.0

    async def get_option_chain(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        response = await asyncio.to_thread(
            self._call_with_retry,
            self.samco.get_option_chain,
            search_symbol_name=symbol,
            exchange=self.samco.EXCHANGE_NFO,
            expiry_date=expiry,
        )
        if not isinstance(response, dict):
            return []

        details = response.get('optionChainDetails') or response.get('optionDetails') or response.get('data') or []
        if isinstance(details, dict):
            details = details.get('data', [])
        return details if isinstance(details, list) else []

    async def get_profile(self) -> dict[str, Any]:
        response = await asyncio.to_thread(self._call_with_retry, self.samco.user_details)
        return response if isinstance(response, dict) else {}

    async def get_funds(self) -> dict[str, Any]:
        response = await asyncio.to_thread(self._call_with_retry, self.samco.get_limits)
        return response if isinstance(response, dict) else {}


samco_client = SamcoClient()
samco_client_instance = samco_client
