from __future__ import annotations

from datetime import date

from broker.samco_broker import SamcoBroker, build_nfo_option_symbol, next_weekly_expiry
from config import settings


class OptionChainService:
    def __init__(self, broker: SamcoBroker) -> None:
        self._broker = broker

    async def get_chain_for_next_expiry(self) -> list[dict]:
        expiry = next_weekly_expiry()
        response = await self._broker.get_option_chain(
            search_symbol_name=settings.option_root_symbol,
            exchange=settings.exchange,
            expiry_date=expiry.isoformat(),
        )
        if not response.ok:
            return []
        return response.payload.get('optionDetails', [])

    @staticmethod
    def build_symbol(strike: int, option_type: str, expiry: date | None = None) -> str:
        return build_nfo_option_symbol('NIFTY', expiry or next_weekly_expiry(), strike, option_type)
