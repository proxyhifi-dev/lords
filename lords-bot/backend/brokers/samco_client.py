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

    @staticmethod
    def to_expiry_code(expiry: str) -> str:
        if not expiry:
            raise ValueError('Expiry cannot be empty')
        expiry = expiry.strip().upper()
        if '-' in expiry:
            return datetime.strptime(expiry, '%Y-%m-%d').strftime('%d%b%y').upper()
        for fmt in ('%d%b%Y', '%d%b%y'):
            try:
                dt = datetime.strptime(expiry, fmt)
                return dt.strftime('%d%b%y').upper()
            except ValueError:
                continue
        raise ValueError(f'Unsupported expiry format: {expiry}')

    @staticmethod
    def to_expiry_api_date(expiry: str) -> str:
        if not expiry:
            raise ValueError('Expiry cannot be empty')
        expiry = expiry.strip().upper()
        if '-' in expiry:
            datetime.strptime(expiry, '%Y-%m-%d')
            return expiry
        for fmt in ('%d%b%Y', '%d%b%y'):
            try:
                return datetime.strptime(expiry, fmt).strftime('%Y-%m-%d')
            except ValueError:
                continue
        raise ValueError(f'Unsupported expiry format: {expiry}')

    def __init__(self) -> None:
        self.samco = StocknoteAPIPythonBridge()
        self._lock = Lock()
        self._authenticated = False
        self._token = ''
        self._api_key = settings.samco_api_key or settings.api_key
        self._api_secret = settings.samco_api_secret
        self._institution_id = settings.samco_institution_id
        self._bootstrap_session()

    def _bootstrap_session(self) -> None:
        if self._load_session() and self._validate_session():
            self._authenticated = True
            logger.info('Samco session restored from .session file')
            return

        env_token = (settings.samco_access_token or settings.samco_session_token).strip()
        if env_token:
            self.set_session_token(env_token, persist=True)
            if self._validate_session():
                self._authenticated = True
                logger.info('Samco session restored from environment token')
                return
            logger.warning('Environment session token invalid; falling back to login')

        self.login(force=True)

    def _load_session(self) -> bool:
        if not self.SESSION_FILE.exists():
            return False
        try:
            payload = json.loads(self.SESSION_FILE.read_text(encoding='utf-8'))
            token = str(payload.get('access_token', '')).strip()
            if not token:
                return False
            self.set_session_token(token, persist=False)
            return True
        except Exception as exc:
            logger.warning('Could not load Samco session file: %s', exc)
            return False

    def _save_session(self) -> None:
        if not self._token:
            return
        try:
            self.SESSION_FILE.write_text(json.dumps({'access_token': self._token}), encoding='utf-8')
        except Exception as exc:
            logger.warning('Could not persist Samco session token: %s', exc)

    def _validate_session(self) -> bool:
        if not self._token:
            return False
        try:
            response = self.samco.user_details()
        except (ConnectionError, TimeoutError, OSError) as exc:
            logger.warning('Unable to validate Samco session because of connection issue: %s', exc)
            return False
        except Exception as exc:
            logger.warning('Unable to validate Samco session: %s', exc)
            return False
        parsed = self._parse_response(response)
        return isinstance(parsed, dict) and str(parsed.get('status', '')).strip().lower() == 'success'

    def login(self, force: bool = False) -> bool:
        if self._authenticated and not force:
            return True

        with self._lock:
            if self._authenticated and not force:
                return True

            if not all([settings.samco_user_id, settings.samco_password, settings.samco_yob]):
                logger.error('Samco credentials missing for login')
                self._authenticated = False
                return False
            if not all([self._api_key, self._api_secret, self._institution_id]):
                logger.warning('Samco API_KEY/API_SECRET/INSTITUTION_ID not fully configured in environment')

            try:
                response = self.samco.login(
                    body={
                        'userId': settings.samco_user_id,
                        'password': settings.samco_password,
                        'yob': settings.samco_yob,
                    }
                )
            except (ConnectionError, TimeoutError, OSError) as exc:
                logger.error('Samco login network error: %s', exc)
                self._authenticated = False
                return False
            except Exception as exc:
                logger.error('Samco login exception: %s', exc)
                self._authenticated = False
                return False

            payload = self._parse_response(response)
            if str(payload.get('status', '')).strip().lower() != 'success':
                logger.error('Samco login failed: %s', payload)
                self._authenticated = False
                return False

            token = str(payload.get('sessionToken') or payload.get('session_token') or payload.get('accessToken') or '').strip()
            if not token:
                logger.error('Samco login response missing session token: %s', payload)
                self._authenticated = False
                return False

            self.set_session_token(token, persist=True)
            self._authenticated = True
            logger.info('Samco login successful')
            return True

    def set_session_token(self, token: str, persist: bool = True) -> None:
        self._token = token
        self.samco.set_session_token(sessionToken=token)
        if persist:
            self._save_session()

    def _needs_relogin(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        message = str(payload.get('statusMessage', '')).lower()
        status = str(payload.get('status', '')).lower()
        return status != 'success' and any(word in message for word in ('session', 'token', 'auth', 'expired'))

    def _parse_response(self, response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response
        if isinstance(response, str):
            try:
                parsed = json.loads(response)
                return parsed if isinstance(parsed, dict) else {'status': 'Error', 'statusMessage': 'Non-dict JSON response'}
            except Exception:
                return {'status': 'Error', 'statusMessage': response}
        return {'status': 'Error', 'statusMessage': 'Invalid response from Samco SDK'}

    def _call(self, fn: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
        if not self._authenticated and not self.login():
            return {'status': 'Error', 'statusMessage': 'Samco authentication failed'}

        retries = max(0, settings.max_api_retries)
        delay = max(0.1, settings.base_retry_delay_seconds)

        for attempt in range(retries + 1):
            try:
                response = fn(**kwargs)
            except (ConnectionError, TimeoutError, OSError) as exc:
                logger.error('Samco connection error on attempt %s: %s', attempt + 1, exc)
                response = {'status': 'Error', 'statusMessage': str(exc)}
            except Exception as exc:
                logger.error('Samco SDK call failed on attempt %s: %s', attempt + 1, exc)
                response = {'status': 'Error', 'statusMessage': str(exc)}

            payload = self._parse_response(response)
            if str(payload.get('status', '')).strip().lower() == 'success':
                time.sleep(0.15)
                return payload

            if self._needs_relogin(payload):
                logger.warning('Samco session seems expired; retrying with fresh login')
                self._authenticated = False
                if not self.login(force=True):
                    return {'status': 'Error', 'statusMessage': 'Session expired and re-login failed'}
                continue

            if attempt < retries:
                time.sleep(delay)
                delay *= 2

            return payload

        return {'status': 'Error', 'statusMessage': 'Samco SDK call failed after retries'}


    async def _run_io(self, fn: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
        timeout = max(1, int(settings.request_timeout))
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._call, fn, **kwargs), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error('Samco call timeout after %ss', timeout)
            return {'status': 'Error', 'statusMessage': f'timeout:{timeout}s'}

    async def index_quote(self, index_name: str) -> dict[str, Any]:
        return await self._run_io(self.samco.index_quote, exchange='NSE', indexName=index_name)

    async def get_quote(self, symbol_name: str, exchange: str = 'NSE') -> dict[str, Any]:
        return await self._run_io(self.samco.get_quote, symbol_name=symbol_name, exchange=exchange)

    async def get_positions(self) -> dict[str, Any]:
        return await self._run_io(self.samco.get_positions)

    async def get_option_chain(self, symbol: str, expiry: str, strike_price: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'search_symbol_name': symbol,
            'exchange': self.samco.EXCHANGE_NFO,
            'expiry_date': self.to_expiry_api_date(expiry),
            'includeGreeks': True,
        }
        if strike_price:
            payload['strike_price'] = str(strike_price)
        return await self._run_io(self.samco.get_option_chain, **payload)

    async def place_order(
        self,
        order_payload: dict[str, Any] | None = None,
        *,
        variety: str = 'NORMAL',
        symbol_name: str = '',
        exchange: str = 'NFO',
        transaction_type: str = 'BUY',
        order_type: str = 'MARKET',
        quantity: int = 0,
        product_type: str = 'INTRADAY',
        price: float = 0.0,
    ) -> dict[str, Any]:
        if order_payload:
            body = order_payload
        else:
            body = {
                'variety': variety,
                'symbolName': symbol_name,
                'exchange': exchange,
                'transactionType': transaction_type,
                'orderType': order_type,
                'quantity': quantity,
                'productType': product_type,
                'price': price,
            }
        return await self._run_io(self.samco.place_order, body=body)

    async def get_limits(self) -> dict[str, Any]:
        return await self._run_io(self.samco.get_limits)

    async def user_details(self) -> dict[str, Any]:
        return await self._run_io(self.samco.user_details)

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        return await self._run_io(self.samco.get_order_status, order_id=order_id)


samco_client = SamcoClient()
