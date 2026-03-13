from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)


class SamcoClient:
    def __init__(self) -> None:
        self.base_url = settings.samco_base_url.rstrip('/')
        self.headers = {
            'x-api-key': settings.samco_api_key,
            'Authorization': f'Bearer {settings.samco_access_token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        self._last_request_ts: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._warned_missing_creds = False

    @staticmethod
    def to_expiry_code(expiry: str) -> str:
        return datetime.strptime(expiry, '%Y-%m-%d').strftime('%d%b%Y').upper()

    @staticmethod
    def to_expiry_dash(expiry: str) -> str:
        return datetime.strptime(expiry, '%Y-%m-%d').strftime('%d-%m-%Y')

    def _has_credentials(self) -> bool:
        return bool(settings.samco_api_key and settings.samco_access_token)

    async def _rate_guard(self, endpoint: str) -> bool:
        now = time.time()
        blocked_until = self._cooldown_until.get(endpoint, 0.0)
        if now < blocked_until:
            logger.warning('samco cooldown active endpoint=%s wait=%.1fs', endpoint, blocked_until - now)
            return False

        min_gap = 0.35
        last_ts = self._last_request_ts.get(endpoint, 0.0)
        if now - last_ts < min_gap:
            await asyncio.sleep(min_gap - (now - last_ts))
        self._last_request_ts[endpoint] = time.time()
        return True

    async def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], int | None]:
        if not self._has_credentials():
            if not self._warned_missing_creds:
                logger.warning('samco credentials missing; skipping live API calls until SAMCO_API_KEY and SAMCO_ACCESS_TOKEN are set')
                self._warned_missing_creds = True
            return {}, None

        allowed = await self._rate_guard(endpoint)
        if not allowed:
            return {}, None

        url = f'{self.base_url}{endpoint}'
        timeout = httpx.Timeout(settings.request_timeout)

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(3):
                try:
                    response = await client.get(url, params=params, headers=self.headers)
                    response.raise_for_status()
                    payload = response.json()
                    if isinstance(payload, dict):
                        return payload, response.status_code
                    return {}, response.status_code
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status in {429, 500, 502, 503, 504} and attempt < 2:
                        backoff = 2**attempt
                        logger.warning('samco retry endpoint=%s status=%s sleep=%ss', endpoint, status, backoff)
                        await asyncio.sleep(backoff)
                        continue
                    if status == 429:
                        self._cooldown_until[endpoint] = time.time() + 10
                    detail = (exc.response.text or '')[:120].replace('\n', ' ')
                    logger.error('samco http error endpoint=%s status=%s detail=%s', endpoint, status, detail)
                    return {}, status
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt < 2:
                        backoff = 2**attempt
                        logger.warning('samco transient error endpoint=%s err=%s sleep=%ss', endpoint, exc, backoff)
                        await asyncio.sleep(backoff)
                        continue
                    logger.error('samco transport/timeout endpoint=%s err=%s', endpoint, exc)
                    return {}, None
                except Exception as exc:  # noqa: BLE001
                    logger.error('samco unexpected endpoint=%s err=%s', endpoint, exc)
                    return {}, None
        return {}, None

    async def get_option_chain(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        expiry_code = self.to_expiry_code(expiry)
        expiry_dash = self.to_expiry_dash(expiry)

        param_variants = [
            {'exchange': 'NFO', 'searchSymbolName': symbol, 'expiry': expiry_code},
            {'exchange': 'NFO', 'searchSymbolName': symbol, 'expiry': expiry},
            {'exchange': 'NFO', 'searchSymbolName': symbol, 'expiryDate': expiry_code},
            {'exchange': 'NFO', 'symbolName': symbol, 'expiry': expiry_code},
            {'exchange': 'NFO', 'symbolName': symbol, 'expiry': expiry_dash},
        ]

        for params in param_variants:
            payload, status = await self._get('/option/optionChain', params)
            data = payload.get('data', []) if isinstance(payload, dict) else []
            if isinstance(data, list) and data:
                return data
            if status is not None and status != 400:
                break
        return []

    async def get_profile(self) -> dict[str, Any]:
        payload, _ = await self._get('/user/profile')
        return payload if isinstance(payload, dict) else {}

    async def get_funds(self) -> dict[str, Any]:
        payload, _ = await self._get('/user/funds')
        return payload if isinstance(payload, dict) else {}

    async def get_underlying_price(self, symbol: str) -> float:
        payload, _ = await self._get('/market/quote', {'symbolName': symbol, 'exchange': 'NSE'})
        data = payload.get('data', {}) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            return 0.0
        value = data.get('lastPrice', data.get('lastTradedPrice', 0.0))
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


samco_client = SamcoClient()
