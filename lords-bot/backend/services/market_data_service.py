from __future__ import annotations

import logging
from typing import Any

from broker.samco_broker import SamcoBroker

logger = logging.getLogger(__name__)


class MarketDataService:
    def __init__(self, broker: SamcoBroker) -> None:
        self._broker = broker
        self._last_price: float | None = None

    @property
    def last_price(self) -> float | None:
        return self._last_price

    async def get_nifty_spot(self) -> float:
        response = await self._broker.index_quote('Nifty 50')
        if response.ok:
            details: list[dict[str, Any]] = response.payload.get('indexDetails') or response.payload.get('data') or []
            if details:
                for key in ('spotPrice', 'ltp', 'indexValue'):
                    if key in details[0]:
                        self._last_price = float(details[0][key])
                        return self._last_price

        if self._last_price is not None:
            logger.warning('market_data_fallback_cached_price')
            return self._last_price
        raise RuntimeError(f'Could not fetch spot price: {response.error}')
