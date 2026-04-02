from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable

from config import settings
from core.rate_limiter import GlobalRateLimiter

try:
    from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge
except Exception:  # pragma: no cover
    StocknoteAPIPythonBridge = object  # type: ignore[assignment]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BrokerResponse:
    ok: bool
    payload: dict[str, Any]
    error: str = ''


class SamcoBroker:
    def __init__(self, bridge: Any | None = None) -> None:
        self._samco = bridge or StocknoteAPIPythonBridge()
        self._rate_limiter = GlobalRateLimiter(settings.min_api_interval_seconds)
        self._session_token = settings.samco_session_token
        if self._session_token:
            self.set_session_token(self._session_token)

    def _parse_payload(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {'status': 'Error', 'message': raw}
        return {'status': 'Error', 'message': 'Invalid response type'}

    def _is_ok(self, payload: dict[str, Any]) -> bool:
        return str(payload.get('status', 'Success')).lower() in {'success', 'ok'} or 'status' not in payload

    def _error_message(self, payload: dict[str, Any]) -> str:
        return str(payload.get('statusMessage') or payload.get('errorMessage') or payload.get('message') or 'unknown_error')

    def _is_retryable(self, message: str) -> bool:
        msg = message.lower()
        return any(token in msg for token in ['timeout', 'internal server', 'tempor', 'gateway', '429'])

    async def _call_with_retry(self, fn: Callable[..., Any], **kwargs: Any) -> BrokerResponse:
        retries = max(settings.max_api_retries, 1)
        delay = max(settings.base_retry_delay_seconds, 0.2)

        for attempt in range(retries + 1):
            await self._rate_limiter.wait()
            try:
                raw = await asyncio.wait_for(asyncio.to_thread(fn, **kwargs), timeout=settings.request_timeout_seconds)
                payload = self._parse_payload(raw)
                if self._is_ok(payload):
                    return BrokerResponse(ok=True, payload=payload)

                message = self._error_message(payload)
                if 'session' in message.lower() and attempt < retries and await self.login():
                    continue
                if attempt < retries and self._is_retryable(message):
                    await asyncio.sleep(delay * (2**attempt))
                    continue
                return BrokerResponse(ok=False, payload=payload, error=message)
            except (asyncio.TimeoutError, OSError) as exc:
                if attempt >= retries:
                    return BrokerResponse(ok=False, payload={}, error=str(exc))
                await asyncio.sleep(delay * (2**attempt))
            except Exception as exc:  # pragma: no cover
                logger.exception('samco_call_failed')
                return BrokerResponse(ok=False, payload={}, error=str(exc))

        return BrokerResponse(ok=False, payload={}, error='call_failed_after_retries')

    async def login(self) -> bool:
        response = await self._call_with_retry(
            self._samco.login,
            body={'userId': settings.samco_user_id, 'password': settings.samco_password, 'yob': settings.samco_yob},
        )
        if not response.ok:
            return False
        token = response.payload.get('sessionToken')
        if token:
            self.set_session_token(token)
            return True
        return False

    def set_session_token(self, token: str) -> None:
        self._session_token = token
        self._samco.set_session_token(token)

    async def index_quote(self, index_name: str) -> BrokerResponse:
        return await self._call_with_retry(self._samco.index_quote, index_name)

    async def get_quote(self, symbol_name: str, exchange: str) -> BrokerResponse:
        return await self._call_with_retry(self._samco.get_quote, symbol_name=symbol_name, exchange=exchange)

    async def get_option_chain(self, search_symbol_name: str, exchange: str, expiry_date: str) -> BrokerResponse:
        return await self._call_with_retry(
            self._samco.get_option_chain,
            search_symbol_name=search_symbol_name,
            exchange=exchange,
            expiry_date=expiry_date,
        )

    async def place_order(self, body: dict[str, Any]) -> BrokerResponse:
        return await self._call_with_retry(self._samco.place_order, body=body)


def next_weekly_expiry(today: date | None = None) -> date:
    today = today or date.today()
    days = (3 - today.weekday()) % 7  # Thursday
    expiry = today + timedelta(days=days)
    if expiry <= today:
        expiry += timedelta(days=7)
    return expiry


def build_nfo_option_symbol(index_prefix: str, expiry: date, strike: int, option_type: str) -> str:
    opt = option_type.upper()
    if opt not in {'CE', 'PE'}:
        raise ValueError('option_type must be CE or PE')
    if strike <= 0:
        raise ValueError('strike must be positive')
    return f'{index_prefix.upper()}{expiry.strftime("%d%b%y").upper()}{strike}{opt}'
