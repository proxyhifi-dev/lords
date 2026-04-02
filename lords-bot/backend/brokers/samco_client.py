from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge

from config import settings
from core.rate_limiter import GlobalRateLimiter

logger = logging.getLogger(__name__)


class SamcoClient:
    SESSION_FILE = Path(__file__).resolve().parents[1] / '.session'

    def __init__(self) -> None:
        self.samco = StocknoteAPIPythonBridge()
        self._lock = Lock()
        self._authenticated = False
        self._token = ''
        self._session_restored = False
        self._limiter = GlobalRateLimiter(settings.max_api_calls_per_second)
        self._bootstrap_session()

    @staticmethod
    def _parse_response(response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response
        if isinstance(response, str):
            try:
                payload = json.loads(response)
                return payload if isinstance(payload, dict) else {'status': 'Error', 'message': response}
            except Exception:
                return {'status': 'Error', 'message': response}
        return {'status': 'Error', 'message': 'Invalid response'}

    @staticmethod
    def _is_success(payload: dict[str, Any]) -> bool:
        return str(payload.get('status', '')).lower() in {'success', 'ok'}

    @staticmethod
    def _message(payload: dict[str, Any]) -> str:
        return str(payload.get('statusMessage') or payload.get('errorMessage') or payload.get('message') or '').strip()

    @classmethod
    def _is_session_error(cls, payload: dict[str, Any]) -> bool:
        msg = cls._message(payload).lower()
        return 'session' in msg and ('expire' in msg or 'invalid' in msg or 'token' in msg)

    @classmethod
    def _is_retryable_error(cls, payload: dict[str, Any]) -> bool:
        msg = cls._message(payload).lower()
        return any(token in msg for token in ['429', 'too many requests', 'internal', 'tempor', 'timeout', 'gateway'])

    @staticmethod
    def to_expiry_code(expiry: str) -> str:
        expiry = expiry.strip().upper()
        if '-' in expiry:
            return datetime.strptime(expiry, '%Y-%m-%d').strftime('%d%b%y').upper()
        for fmt in ('%d%b%Y', '%d%b%y'):
            try:
                return datetime.strptime(expiry, fmt).strftime('%d%b%y').upper()
            except ValueError:
                pass
        raise ValueError(f'Unsupported expiry format: {expiry}')

    @staticmethod
    def to_expiry_api_date(expiry: str) -> str:
        expiry = expiry.strip().upper()
        if '-' in expiry:
            datetime.strptime(expiry, '%Y-%m-%d')
            return expiry
        for fmt in ('%d%b%Y', '%d%b%y'):
            try:
                return datetime.strptime(expiry, fmt).strftime('%Y-%m-%d')
            except ValueError:
                pass
        raise ValueError(f'Unsupported expiry format: {expiry}')

    def _bootstrap_session(self) -> None:
        if settings.samco_session_token:
            self.set_session_token(settings.samco_session_token, persist=False)
            self._authenticated = True
            self._session_restored = True
            return
        if self._load_session():
            self._authenticated = True
            self._session_restored = True
            return
        self.login()

    def _load_session(self) -> bool:
        if not self.SESSION_FILE.exists():
            return False
        try:
            payload = json.loads(self.SESSION_FILE.read_text())
            token = payload.get('access_token')
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
            self.SESSION_FILE.write_text(json.dumps({'access_token': self._token}))
        except Exception:
            logger.exception('samco_session_save_failed')

    def set_session_token(self, token: str, persist: bool = True) -> None:
        self._token = token
        self.samco.set_session_token(sessionToken=token)
        if persist:
            self._save_session()

    def login(self) -> bool:
        with self._lock:
            if self._authenticated and self._token and not self._session_restored:
                return True
            response = self.samco.login(
                body={
                    'userId': settings.samco_user_id,
                    'password': settings.samco_password,
                    'yob': settings.samco_yob,
                }
            )
            payload = self._parse_response(response)
            if not self._is_success(payload):
                logger.error('samco_login_failed %s', self._message(payload))
                return False
            token = payload.get('sessionToken') or payload.get('accessToken')
            if not token:
                return False
            self.set_session_token(token)
            self._authenticated = True
            self._session_restored = False
            return True

    def _call_sync(self, fn: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
        retries = max(1, settings.max_api_retries)
        base_delay = max(0.2, settings.base_retry_delay_seconds)
        for attempt in range(retries + 1):
            try:
                if fn != self.samco.login and not self._authenticated and not self.login():
                    return {'status': 'Error', 'message': 'Samco login failed'}
                payload = self._parse_response(fn(**kwargs))
                if self._is_success(payload):
                    return payload
                if self._is_session_error(payload):
                    self._authenticated = False
                    if self.login():
                        continue
                if not self._is_retryable_error(payload):
                    return payload
            except Exception:
                logger.exception('samco_call_failed fn=%s', getattr(fn, '__name__', 'unknown'))
            if attempt < retries:
                delay = base_delay * (2**attempt)
                asyncio.run(asyncio.sleep(delay))
        return {'status': 'Error', 'message': 'API call failed after retries'}

    async def _run_io(self, fn: Callable[..., Any], *, rate_limited: bool = False, **kwargs: Any) -> dict[str, Any]:
        if rate_limited:
            await self._limiter.wait()
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._call_sync, fn, **kwargs), timeout=settings.request_timeout)
        except asyncio.TimeoutError:
            return {'status': 'Error', 'message': 'timeout'}

    async def multi_quote(self, payload: dict[str, list[str]]) -> dict[str, Any]:
        if hasattr(self.samco, 'multi_quote'):
            return await self._run_io(self.samco.multi_quote, body=payload, rate_limited=True)
        if hasattr(self.samco, 'get_multi_quote'):
            return await self._run_io(self.samco.get_multi_quote, body=payload, rate_limited=True)
        index_name = ((payload or {}).get('INDEX') or [''])[0]
        return await self._run_io(self.samco.index_quote, indexName=index_name, rate_limited=True)

    async def index_quote(self, index_name: str) -> dict[str, Any]:
        return await self.multi_quote({'INDEX': [index_name]})

    async def get_quote(self, symbol_name: str, exchange: str = 'NFO') -> dict[str, Any]:
        return await self._run_io(self.samco.get_quote, symbol_name=symbol_name, exchange=exchange, rate_limited=True)

    async def get_option_chain(self, symbol: str, expiry: str | None = None, strike_price: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {'search_symbol_name': symbol.upper(), 'exchange': 'NFO'}
        if expiry:
            payload['expiry_date'] = self.to_expiry_api_date(expiry)
        if strike_price:
            payload['strike_price'] = strike_price
        return await self._run_io(self.samco.get_option_chain, rate_limited=True, **payload)

    async def place_order(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._run_io(self.samco.place_order, body=body, rate_limited=True)

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        return await self._run_io(self.samco.get_order_status, order_id=order_id, rate_limited=True)

    async def get_positions(self) -> dict[str, Any]:
        return await self._run_io(self.samco.get_positions, rate_limited=True)

    async def get_limits(self) -> dict[str, Any]:
        return await self._run_io(self.samco.get_limits, rate_limited=True)

    async def user_details(self) -> dict[str, Any]:
        if hasattr(self.samco, 'user_details'):
            return await self._run_io(self.samco.user_details, rate_limited=True)
        if hasattr(self.samco, 'get_profile'):
            return await self._run_io(self.samco.get_profile, rate_limited=True)
        return {'status': 'Error', 'message': 'user_details_not_supported'}


samco_client = SamcoClient()
