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

logger = logging.getLogger('core.logger')


class SamcoClient:
    SESSION_FILE = Path(__file__).resolve().parents[1] / '.session'

    def __init__(self) -> None:
        self.samco = StocknoteAPIPythonBridge()
        self._lock = Lock()
        self._authenticated = False
        self._token = ''
        self._session_restored = False
        self._last_market_request_ts = 0.0
        self._bootstrap_session()

    @staticmethod
    def _is_success(payload: dict[str, Any]) -> bool:
        return str(payload.get('status', '')).lower() == 'success'

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
        return any(token in msg for token in ['429', 'too many requests', 'internal server error', 'timeout', 'tempor'])

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
            logger.info('samco_session_restored source=env')
            return
        if self._load_session():
            self._authenticated = True
            self._session_restored = True
            logger.info('samco_session_restored source=file')
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
            logger.warning('samco_session_save_failed')

    def set_session_token(self, token: str, persist: bool = True) -> None:
        self._token = token
        try:
            self.samco.set_session_token(sessionToken=token)
        except Exception:
            logger.exception('samco_set_session_failed')
        if persist:
            self._save_session()

    def login(self) -> bool:
        with self._lock:
            if self._authenticated and self._token and not self._session_restored:
                return True
            try:
                response = self.samco.login(
                    body={
                        'userId': settings.samco_user_id,
                        'password': settings.samco_password,
                        'yob': settings.samco_yob,
                    }
                )
                payload = self._parse_response(response)
                if not self._is_success(payload):
                    logger.error('samco_login_failed message=%s', self._message(payload))
                    return False
                token = payload.get('sessionToken') or payload.get('accessToken')
                if not token:
                    logger.error('samco_login_missing_token')
                    return False
                self.set_session_token(token)
                self._authenticated = True
                self._session_restored = False
                logger.info('samco_login_success')
                return True
            except Exception:
                logger.exception('samco_login_error')
                return False

    def _parse_response(self, response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response
        if isinstance(response, str):
            try:
                payload = json.loads(response)
                return payload if isinstance(payload, dict) else {'status': 'Error', 'message': response}
            except Exception:
                return {'status': 'Error', 'message': response}
        return {'status': 'Error', 'message': 'Invalid response'}

    def _rate_limit_market_calls(self) -> None:
        min_gap = max(1, settings.min_market_poll_seconds)
        now = time.time()
        elapsed = now - self._last_market_request_ts
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        self._last_market_request_ts = time.time()

    def _call(self, fn: Callable[..., Any], *, rate_limited: bool = False, **kwargs: Any) -> dict[str, Any]:
        retries = max(1, settings.max_api_retries)
        base_delay = max(0.2, settings.base_retry_delay_seconds)
        for attempt in range(retries + 1):
            try:
                if fn != self.samco.login and not self._authenticated and not self.login():
                    return {'status': 'Error', 'message': 'Samco login failed'}
                if rate_limited:
                    self._rate_limit_market_calls()
                payload = self._parse_response(fn(**kwargs))
                if self._is_success(payload):
                    return payload
                if self._is_session_error(payload):
                    self._authenticated = False
                    if self.login():
                        continue
                if not self._is_retryable_error(payload):
                    return payload
                logger.warning('samco_retryable_error attempt=%s message=%s', attempt + 1, self._message(payload))
            except Exception:
                logger.exception('samco_sdk_call_failed')
            if attempt < retries:
                time.sleep(base_delay * (2**attempt))
        return {'status': 'Error', 'message': 'API call failed after retries'}

    async def _run_io(self, fn: Callable[..., Any], *, rate_limited: bool = False, **kwargs: Any) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._call, fn, rate_limited=rate_limited, **kwargs),
                timeout=settings.request_timeout,
            )
        except asyncio.TimeoutError:
            logger.error('samco_timeout')
            return {'status': 'Error', 'message': 'timeout'}

    async def multi_quote(self, payload: dict[str, list[str]]) -> dict[str, Any]:
        if hasattr(self.samco, 'multi_quote'):
            return await self._run_io(self.samco.multi_quote, symbol_list=payload, rate_limited=True)
        index_name = ((payload or {}).get('INDEX') or [''])[0]
        return await self._run_io(self.samco.index_quote, exchange='NSE', indexName=index_name, rate_limited=True)

    async def index_quote(self, index_name: str) -> dict[str, Any]:
        return await self.multi_quote({'INDEX': [index_name]})

    async def get_quote(self, symbol_name: str) -> dict[str, Any]:
        return await self._run_io(self.samco.get_quote, symbol_name=symbol_name, exchange='NSE', rate_limited=True)

    async def get_positions(self) -> dict[str, Any]:
        return await self._run_io(self.samco.get_positions)

    async def get_option_chain(self, symbol: str, expiry: str | None = None, strike_price: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {'search_symbol_name': symbol, 'exchange': self.samco.EXCHANGE_NFO}
        if expiry:
            payload['expiry_date'] = self.to_expiry_api_date(expiry)
        if strike_price:
            payload['strike_price'] = strike_price
        return await self._run_io(self.samco.get_option_chain, rate_limited=True, **payload)

    async def place_order(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._run_io(self.samco.place_order, body=body)

    async def get_limits(self) -> dict[str, Any]:
        return await self._run_io(self.samco.get_limits)

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        return await self._run_io(self.samco.get_order_status, order_id=order_id)


samco_client = SamcoClient()
