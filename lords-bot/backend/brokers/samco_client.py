from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


class SamcoClient:
    def __init__(self) -> None:
        self.base_url = settings.samco_base_url.rstrip('/')
        self.headers = {
            'x-api-key': settings.samco_api_key,
            'Authorization': f'Bearer {settings.samco_access_token}',
            'Content-Type': 'application/json',
        }

    async def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f'{self.base_url}{endpoint}'
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            for attempt in range(3):
                try:
                    response = await client.get(url, params=params, headers=self.headers)
                    if response.status_code == 200:
                        return response.json()
                    if response.status_code in {429, 500, 502, 503, 504}:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    logger.warning('samco request failed endpoint=%s status=%s', endpoint, response.status_code)
                    return {}
                except httpx.TimeoutException:
                    logger.warning('samco timeout endpoint=%s attempt=%s', endpoint, attempt + 1)
                    await asyncio.sleep(0.5 * (attempt + 1))
                except Exception as exc:  # noqa: BLE001
                    logger.warning('samco error endpoint=%s err=%s', endpoint, exc)
                    await asyncio.sleep(0.5 * (attempt + 1))
        return {}

    async def get_option_chain(self, symbol: str, expiry: str) -> list[dict[str, Any]]:
        payload = await self._get('/option/optionChain', {'exchange': 'NFO', 'searchSymbolName': symbol, 'expiry': expiry})
        return payload.get('data', []) if isinstance(payload, dict) else []

    async def get_profile(self) -> dict[str, Any]:
        return await self._get('/user/profile')

    async def get_funds(self) -> dict[str, Any]:
        return await self._get('/user/funds')

    async def get_underlying_price(self, symbol: str) -> float:
        payload = await self._get('/market/quote', {'symbolName': symbol, 'exchange': 'NSE'})
        try:
            return float(payload['data']['lastTradedPrice'])
        except Exception:
            return 0.0


samco_client = SamcoClient()
