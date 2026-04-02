from __future__ import annotations

import json
from datetime import date
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
        self._live_expiry_by_symbol: dict[str, str] = {}

    @staticmethod
    def _extract_details(response: dict[str, Any]) -> list[dict[str, Any]]:
        details = (
            response.get("optionChainDetails")
            or response.get("optionDetails")
            or response.get("data")
            or []
        )
        return [row for row in details if isinstance(row, dict)]

    @staticmethod
    def _extract_error_text(response: dict[str, Any]) -> str:
        return str(
            response.get("statusMessage")
            or response.get("errorMessage")
            or response.get("message")
            or ""
        ).strip()

    @staticmethod
    def _extract_expiries(details: list[dict[str, Any]]) -> list[str]:
        values: set[str] = set()
        for row in details:
            raw = str(row.get("expiryDate") or row.get("expiry_date") or "").strip()
            if not raw:
                continue
            try:
                values.add(SamcoClient.to_expiry_api_date(raw))
            except ValueError:
                continue
        return sorted(values)

    @staticmethod
    def _pick_live_expiry(expiries: list[str]) -> str | None:
        if not expiries:
            return None
        today = date.today().isoformat()
        future_or_today = [item for item in expiries if item >= today]
        return (future_or_today[0] if future_or_today else expiries[-1])

    def get_live_expiry(self, symbol: str, fallback_expiry: str) -> str:
        return self._live_expiry_by_symbol.get(symbol.upper(), fallback_expiry)

    async def get_option_chain(
        self,
        symbol: str,
        expiry: str,
        strike_price: str | None = None
    ) -> list[dict[str, Any]]:

        symbol_key = symbol.upper()
        requested_expiry = self.get_live_expiry(symbol_key, expiry)

        key = f"option_chain:{symbol_key}:{requested_expiry}:{strike_price or 'ALL'}"

        cached = self.cache.get(key)
        if cached is not None:
            return cached

        response = self._normalize_response(
            await samco_client.get_option_chain(symbol_key, requested_expiry, strike_price=strike_price)
        )
        details = self._extract_details(response)

        if not details:
            # Recover from stale expiry by pulling contracts without expiry filter
            recovery = self._normalize_response(
                await samco_client.get_option_chain(symbol_key, None, strike_price=strike_price)
            )
            recovery_details = self._extract_details(recovery)
            live_expiry = self._pick_live_expiry(self._extract_expiries(recovery_details))

            if live_expiry and live_expiry != requested_expiry:
                self._live_expiry_by_symbol[symbol_key] = live_expiry
                key = f"option_chain:{symbol_key}:{live_expiry}:{strike_price or 'ALL'}"
                response = self._normalize_response(
                    await samco_client.get_option_chain(symbol_key, live_expiry, strike_price=strike_price)
                )
                details = self._extract_details(response)
            else:
                details = recovery_details

            if not details:
                error_text = self._extract_error_text(recovery) or self._extract_error_text(response)
                raise RuntimeError(error_text or "Option chain unavailable from Samco")

        available_expiries = self._extract_expiries(details)
        if available_expiries:
            live_expiry = self._pick_live_expiry(available_expiries)
            if live_expiry:
                self._live_expiry_by_symbol[symbol_key] = live_expiry

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
