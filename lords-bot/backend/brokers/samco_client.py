from __future__ import annotations

import asyncio
import logging
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

    @staticmethod
    def to_expiry_code(expiry: str) -> str:
        # 2026-03-26 -> 26MAR2026
        return datetime.strptime(expiry, '%Y-%m-%d').strftime('%d%b%Y').upper()

    async def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f'{self.base_url}{endpoint}'
        timeout = httpx.Timeout(settings.request_timeout)

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(3):
                try:
                    response = await client.get(url, params=params, headers=self.headers)
                    response.raise_for_status()
                    payload = response.json()
                    if isinstance(payload, dict):
                        return payload
                    return {}
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status in {429, 500, 502, 503, 504} and attempt < 2:
                        backoff = 2**attempt
                        logger.warning('samco retry endpoint=%s status=%s sleep=%ss', endpoint, status, backoff)
                        await asyncio.sleep(backoff)
                        continue
                    logger.error('samco http error endpoint=%s status=%s', endpoint, status)
                    return {}
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt < 2:
                        backoff = 2**attempt
                        logger.warning('samco transient error endpoint=%s err=%s sleep=%ss', endpoint, exc, backoff)
                        await asyncio.sleep(backoff)
                        continue
                    logger.error('samco transport/timeout endpoint=%s err=%s', endpoint, exc)
                    return {}
                except Exception as exc:  # noqa: BLE001
                    logger.error('samco unexpected endpoint=%s err=%s', endpoint, exc)
                    return {}
        return {}

    async def get_option_chain(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        formatted_expiry = self.to_expiry_code(expiry)
        payload = await self._get('/option/optionChain', {
            'exchange': 'NFO',
            'searchSymbolName': symbol,
            'expiry': formatted_expiry,
        })
        data = payload.get('data', []) if isinstance(payload, dict) else []
        return data if isinstance(data, list) else []

    async def get_profile(self) -> dict[str, Any]:
        payload = await self._get('/user/profile')
        return payload if isinstance(payload, dict) else {}

    async def get_funds(self) -> dict[str, Any]:
        payload = await self._get('/user/funds')
        return payload if isinstance(payload, dict) else {}

    async def get_underlying_price(self, symbol: str) -> float:
        payload = await self._get('/market/quote', {'symbolName': symbol, 'exchange': 'NSE'})
        data = payload.get('data', {}) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            return 0.0
        value = data.get('lastPrice', data.get('lastTradedPrice', 0.0))
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


samco_client = SamcoClient()
