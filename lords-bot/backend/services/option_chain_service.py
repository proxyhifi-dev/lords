from __future__ import annotations

import json
from typing import Any

from brokers.samco_client import SamcoClient, samco_client
from config import settings
from core.cache import TTLCache


class OptionChainService:

    @staticmethod
    def _normalize_response(response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response

        if isinstance(response, str):
            try:
                payload = json.loads(response)
                return payload if isinstance(payload, dict) else {}
            except Exception:
                return {}

        return {}

    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache

    async def get_option_chain(
        self,
        symbol: str,
        expiry: str,
        strike_price: str | None = None
    ) -> list[dict[str, Any]]:

        key = f"option_chain:{symbol}:{expiry}:{strike_price or 'ALL'}"

        cached = self.cache.get(key)
        if cached is not None:
            return cached

        response = self._normalize_response(
            await samco_client.get_option_chain(symbol, expiry, strike_price=strike_price)
        )

        details = (
            response.get("optionChainDetails")
            or response.get("optionDetails")
            or response.get("data")
            or []
        )

        chain_by_strike: dict[float, dict[str, Any]] = {}

        for row in details:

            try:
                strike = float(row.get("strikePrice") or row.get("strike_price") or 0)
            except (TypeError, ValueError):
                continue

            if strike <= 0:
                continue

            opt = str(row.get("optionType") or row.get("option_type") or "").upper()

            item = chain_by_strike.setdefault(
                strike,
                {
                    "strike_price": strike,
                    "call_oi": 0.0,
                    "put_oi": 0.0,
                    "call_ltp": 0.0,
                    "put_ltp": 0.0,
                },
            )

            if opt == "CE":
                item["call_oi"] = float(row.get("openInterest") or row.get("open_interest") or 0)
                item["call_ltp"] = float(row.get("lastTradedPrice") or row.get("last_traded_price") or 0)

            elif opt == "PE":
                item["put_oi"] = float(row.get("openInterest") or row.get("open_interest") or 0)
                item["put_ltp"] = float(row.get("lastTradedPrice") or row.get("last_traded_price") or 0)

        normalized = sorted(chain_by_strike.values(), key=lambda x: x["strike_price"])

        self.cache.set(key, normalized, settings.option_chain_ttl)

        return normalized

    def get_option_chain_bias(self, chain: list[dict[str, Any]]) -> str:

        if not chain:
            return "NEUTRAL"

        call_oi = sum(float(r.get("call_oi", 0) or 0) for r in chain)
        put_oi = sum(float(r.get("put_oi", 0) or 0) for r in chain)

        if call_oi <= 0 and put_oi <= 0:
            return "NEUTRAL"

        pcr = put_oi / max(1.0, call_oi)

        if pcr >= 1.1:
            return "BULLISH"

        if pcr <= 0.9:
            return "BEARISH"

        return "NEUTRAL"

    def select_atm_strike(self, spot_price: float, chain: list[dict[str, Any]]) -> float:

        if not chain:
            return round(spot_price / 50.0) * 50.0

        best_row = None
        best_score = -1

        for row in chain:

            strike = float(row.get("strike_price") or 0)

            call_oi = float(row.get("call_oi") or 0)
            put_oi = float(row.get("put_oi") or 0)

            ltp = max(
                float(row.get("call_ltp") or 0),
                float(row.get("put_ltp") or 0),
            )

            distance = abs(strike - spot_price)

            # Liquidity scoring
            score = (call_oi + put_oi) + (ltp * 10) - distance

            if score > best_score:
                best_score = score
                best_row = row

        if best_row:
            return float(best_row["strike_price"])

        return round(spot_price / 50.0) * 50.0

    def build_option_symbol(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        option_side: str
    ) -> str:

        expiry_code = SamcoClient.to_expiry_code(expiry)

        suffix = "CE" if option_side.upper() == "CALL" else "PE"

        return f"{underlying.upper()}{expiry_code}{int(round(strike))}{suffix}"

    def pick_option_contract(
        self,
        chain: list[dict[str, Any]],
        spot_price: float,
        option_side: str,
        symbol: str,
        expiry: str,
    ) -> dict[str, Any]:

        strike = self.select_atm_strike(spot_price, chain)

        row = next(
            (
                item
                for item in chain
                if float(item.get("strike_price") or 0.0) == strike
            ),
            {},
        )

        if not row:
            return {}

        ltp_key = "call_ltp" if option_side.upper() == "CALL" else "put_ltp"

        premium = float(row.get(ltp_key) or 0.0)

        return {
            "option_symbol": self.build_option_symbol(symbol, expiry, strike, option_side),
            "strike": strike,
            "premium": premium,
            "expiry": SamcoClient.to_expiry_api_date(expiry),
            "option_type": "CE" if option_side.upper() == "CALL" else "PE",
        }