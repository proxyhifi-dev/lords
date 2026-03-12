from __future__ import annotations

import logging
from typing import Any

from config import settings
from models import OptionChainSnapshot, OptionStrikeData
from samco_client import SamcoClient

logger = logging.getLogger("option.chain")


class OptionChainService:
    def __init__(self, client: SamcoClient) -> None:
        self.client = client

    @staticmethod
    def _extract_payload(raw: dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(raw.get("data"), list):
            return raw["data"]
        if isinstance(raw.get("optionChain"), list):
            return raw["optionChain"]
        if isinstance(raw.get("data"), dict) and isinstance(raw["data"].get("optionChain"), list):
            return raw["data"]["optionChain"]
        return []

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    async def fetch_snapshot(self) -> OptionChainSnapshot:
        raw = await self.client.get_option_chain()
        rows = self._extract_payload(raw)
        if not rows:
            raise ValueError("No option chain rows returned by SAMCO")

        spot = self._to_float(raw.get("spotPrice") or raw.get("underlyingValue") or rows[0].get("spotPrice"))
        if spot <= 0:
            spot = self._to_float(rows[0].get("ltp"))
        atm = round(spot / 50) * 50 if spot > 0 else 0

        strikes: list[OptionStrikeData] = []
        for row in rows:
            strike = self._to_float(row.get("strikePrice"))
            ce_oi = self._to_float(row.get("ceOi") or row.get("ce_oi") or row.get("callOi"))
            pe_oi = self._to_float(row.get("peOi") or row.get("pe_oi") or row.get("putOi"))
            ce_ltp = self._to_float(row.get("ceLtp") or row.get("callLtp") or row.get("ltp"))
            pe_ltp = self._to_float(row.get("peLtp") or row.get("putLtp"))
            if strike > 0:
                strikes.append(
                    OptionStrikeData(
                        strike_price=strike,
                        ce_oi=ce_oi,
                        pe_oi=pe_oi,
                        ce_ltp=ce_ltp,
                        pe_ltp=pe_ltp,
                    )
                )

        strikes.sort(key=lambda x: x.strike_price)

        logger.info("option_chain_snapshot", extra={"rows": len(strikes), "spot": spot, "atm": atm})
        return OptionChainSnapshot(
            symbol=settings.nifty_symbol,
            exchange=settings.option_exchange,
            expiry_date=settings.option_expiry,
            spot_price=spot,
            atm_strike=atm,
            strikes=strikes,
        )
