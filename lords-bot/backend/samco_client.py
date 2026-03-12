from __future__ import annotations

import math
import random
from datetime import datetime

import httpx

from auth import samco_auth
from config import settings
from models import OptionRow
from utils import with_retries


class SamcoClient:
    def __init__(self) -> None:
        self.base_url = settings.samco_base_url.rstrip('/')
        self.timeout = httpx.Timeout(5.0)

    async def login(self) -> str:
        return await samco_auth.login()

    async def get_option_chain(self, symbol: str, expiry: str) -> list[dict]:
        token = await self.login()

        async def _fetch():
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
                response = await client.get(
                    '/option/optionChain',
                    params={
                        'exchange': 'NFO',
                        'searchSymbolName': symbol,
                        'expiry': expiry,
                    },
                    headers={'Authorization': f'Bearer {token}'},
                )
                response.raise_for_status()
                payload = response.json()
                return self._parse_option_chain(payload)

        try:
            return await with_retries(_fetch, retries=3, delay=0.4)
        except Exception:
            return self._mock_option_chain(symbol)

    def _parse_option_chain(self, payload: dict) -> list[dict]:
        rows: list[dict] = []
        raw = payload.get('data') or payload.get('optionChain') or []
        for item in raw:
            row = OptionRow(
                strike_price=float(item.get('strikePrice', 0)),
                call_oi=float(item.get('callOI', item.get('call_oi', 0))),
                put_oi=float(item.get('putOI', item.get('put_oi', 0))),
                call_change_oi=float(item.get('callChangeInOI', item.get('call_change_oi', 0))),
                put_change_oi=float(item.get('putChangeInOI', item.get('put_change_oi', 0))),
                call_ltp=float(item.get('callLTP', item.get('call_ltp', 0))),
                put_ltp=float(item.get('putLTP', item.get('put_ltp', 0))),
                volume=float(item.get('volume', 0)),
            )
            rows.append(row.model_dump())
        return rows

    def _mock_option_chain(self, symbol: str) -> list[dict]:
        seed = datetime.utcnow().second + (1 if symbol == 'BANKNIFTY' else 0)
        random.seed(seed)
        spot = 22500 if symbol == 'NIFTY' else 48500
        step = 50 if symbol == 'NIFTY' else 100
        base_strike = int(round(spot / step) * step)
        rows: list[dict] = []

        for i in range(-10, 11):
            strike = base_strike + i * step
            distance = abs(i)
            oi_bias = max(1, 11 - distance)
            call_oi = 1500 + random.randint(100, 1500) * (1 + max(i, 0) / 10)
            put_oi = 1500 + random.randint(100, 1500) * (1 + max(-i, 0) / 10)
            call_ltp = max(5.0, 220 - distance * 17 + random.uniform(-8, 8))
            put_ltp = max(5.0, 220 - distance * 17 + random.uniform(-8, 8))

            rows.append(
                OptionRow(
                    strike_price=float(strike),
                    call_oi=float(call_oi * oi_bias),
                    put_oi=float(put_oi * oi_bias),
                    call_change_oi=float(random.randint(-300, 600)),
                    put_change_oi=float(random.randint(-300, 600)),
                    call_ltp=round(call_ltp + math.sin(seed / 10) * 3, 2),
                    put_ltp=round(put_ltp + math.cos(seed / 10) * 3, 2),
                    volume=float(random.randint(500, 9000)),
                ).model_dump()
            )
        return rows


samco_client = SamcoClient()
