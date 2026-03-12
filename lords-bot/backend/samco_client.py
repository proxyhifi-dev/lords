from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime

import httpx

from auth import samco_auth
from cache import load_cached_snapshot
from config import settings, yaml_config
from models import OptionRow
from utils import with_retries

logger = logging.getLogger(__name__)


class SamcoClient:
    def __init__(self) -> None:
        self.base_url = settings.samco_base_url.rstrip('/')
        self.timeout = httpx.Timeout(float(yaml_config.api_timeout))
        self._last_request_ts = 0.0
        self._min_interval = 0.2

    async def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_request_ts
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request_ts = time.time()

    async def login(self) -> str:
        return await samco_auth.login()

    async def _safe_get(self, path: str, params: dict | None = None) -> dict:
        token = await self.login()

        async def _fetch() -> dict:
            await self._rate_limit()
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
                response = await client.get(path, params=params, headers={'Authorization': f'Bearer {token}'})
                response.raise_for_status()
                return response.json()

        return await with_retries(_fetch, retries=3, delay=0.5)

    async def get_profile(self) -> dict:
        try:
            return await self._safe_get('/user/profile')
        except Exception:
            return {'status': 'fallback', 'username': settings.samco_username, 'broker': 'SAMCO'}

    async def get_funds(self) -> dict:
        try:
            return await self._safe_get('/user/funds')
        except Exception:
            return {'status': 'fallback', 'available_cash': 100000, 'utilized_margin': 0}

    async def get_orders(self) -> dict:
        try:
            return await self._safe_get('/orders')
        except Exception:
            return {'status': 'fallback', 'orders': []}

    async def get_underlying_price(self, symbol: str = 'NIFTY') -> float:
        try:
            payload = await self._safe_get('/market/quote', params={'symbolName': symbol, 'exchange': 'NSE'})
            data = payload.get('data', payload)
            return float(data.get('ltp') or data.get('lastTradedPrice') or 0)
        except Exception:
            cached = load_cached_snapshot().get('underlying_price')
            return float(cached or 22500.0)

    async def get_option_chain(self, symbol: str, expiry: str) -> list[dict]:
        try:
            payload = await self._safe_get(
                '/option/optionChain',
                params={'exchange': 'NFO', 'searchSymbolName': symbol, 'expiry': expiry},
            )
            return self._parse_option_chain(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning('Option chain API failed, using fallback cache/mock: %s', exc)
            cached = load_cached_snapshot().get('option_chain', [])
            return cached or self._mock_option_chain(symbol)

    def _parse_option_chain(self, payload: dict) -> list[dict]:
        rows: list[dict] = []
        raw = payload.get('data') or payload.get('optionChain') or []
        for item in raw:
            rows.append(
                OptionRow(
                    strike_price=float(item.get('strikePrice', item.get('strike_price', 0))),
                    call_oi=float(item.get('callOI', item.get('call_oi', 0))),
                    put_oi=float(item.get('putOI', item.get('put_oi', 0))),
                    call_change_oi=float(item.get('callChangeInOI', item.get('call_change_oi', 0))),
                    put_change_oi=float(item.get('putChangeInOI', item.get('put_change_oi', 0))),
                    call_ltp=float(item.get('callLTP', item.get('call_ltp', 0))),
                    put_ltp=float(item.get('putLTP', item.get('put_ltp', 0))),
                    volume=float(item.get('volume', 0)),
                ).model_dump()
            )
        return rows

    def _mock_option_chain(self, symbol: str) -> list[dict]:
        seed = datetime.utcnow().second
        random.seed(seed)
        spot = 22500
        step = 50
        base_strike = int(round(spot / step) * step)
        rows = []
        for i in range(-15, 16):
            strike = base_strike + (i * step)
            rows.append(
                OptionRow(
                    strike_price=float(strike),
                    call_oi=float(random.randint(1000, 12000)),
                    put_oi=float(random.randint(1000, 12000)),
                    call_change_oi=float(random.randint(-500, 800)),
                    put_change_oi=float(random.randint(-500, 800)),
                    call_ltp=round(max(5, 200 - abs(i) * 10 + random.uniform(-5, 5)), 2),
                    put_ltp=round(max(5, 200 - abs(i) * 10 + random.uniform(-5, 5)), 2),
                    volume=float(random.randint(500, 15000)),
                ).model_dump()
            )
        return rows


samco_client = SamcoClient()
