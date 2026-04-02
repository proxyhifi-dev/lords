from __future__ import annotations

import json
from datetime import date
from typing import Any

from brokers.samco_client import SamcoClient, samco_client
from config import settings
from core.cache import TTLCache


class OptionChainService:
    def __init__(self, cache: TTLCache) -> None:
        self.cache = cache
        self._live_expiry_by_symbol: dict[str, str] = {}

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

    @staticmethod
    def _extract_details(response: dict[str, Any]) -> list[dict[str, Any]]:
        details = response.get('optionChainDetails') or response.get('optionDetails') or response.get('data') or []
        return [row for row in details if isinstance(row, dict)]

    def get_live_expiry(self, symbol: str, fallback_expiry: str) -> str:
        return self._live_expiry_by_symbol.get(symbol.upper(), fallback_expiry)

    @staticmethod
    def _pick_live_expiry(details: list[dict[str, Any]]) -> str | None:
        expiries = []
        for row in details:
            raw = str(row.get('expiryDate') or '').strip()
            if not raw:
                continue
            try:
                expiries.append(SamcoClient.to_expiry_api_date(raw))
            except ValueError:
                continue
        expiries = sorted(set(expiries))
        if not expiries:
            return None
        today = date.today().isoformat()
        for expiry in expiries:
            if expiry >= today:
                return expiry
        return expiries[-1]

    @staticmethod
    def _norm_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _contract_quality(self, row: dict[str, Any]) -> bool:
        volume = self._norm_float(row.get('tradingVolume') or row.get('volume'))
        oi = self._norm_float(row.get('openInterest'))
        bid = self._norm_float(row.get('bestBidPrice') or row.get('bidPrice'))
        ask = self._norm_float(row.get('bestAskPrice') or row.get('askPrice'))
        spread = (ask - bid) if ask > 0 and bid > 0 else 0.0
        spread_ratio = spread / ask if ask > 0 else 1.0
        if oi <= 0:
            return False
        if bid > 0 and ask > 0:
            return spread_ratio <= 0.2
        return volume >= 0

    async def get_option_chain(self, symbol: str, expiry: str, strike_price: str | None = None) -> list[dict[str, Any]]:
        symbol_key = symbol.upper()
        requested_expiry = self.get_live_expiry(symbol_key, expiry)
        key = f'option_chain:{symbol_key}:{requested_expiry}:{strike_price or "ALL"}'
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        response = self._normalize_response(await samco_client.get_option_chain(symbol_key, requested_expiry, strike_price))
        details = self._extract_details(response)

        if not details:
            fallback = self._normalize_response(await samco_client.get_option_chain(symbol_key, None, strike_price))
            details = self._extract_details(fallback)

        live_expiry = self._pick_live_expiry(details)
        if live_expiry:
            self._live_expiry_by_symbol[symbol_key] = live_expiry
            if live_expiry != requested_expiry:
                response = self._normalize_response(await samco_client.get_option_chain(symbol_key, live_expiry, strike_price))
                details = self._extract_details(response) or details

        chain_by_strike: dict[float, dict[str, Any]] = {}
        for row in details:
            if not self._contract_quality(row):
                continue
            strike = self._norm_float(row.get('strikePrice') or row.get('strike_price'))
            if strike <= 0:
                continue
            opt = str(row.get('optionType') or row.get('option_type') or '').upper()
            item = chain_by_strike.setdefault(
                strike,
                {'strike_price': strike, 'call_oi': 0.0, 'put_oi': 0.0, 'call_ltp': 0.0, 'put_ltp': 0.0},
            )
            if opt == 'CE':
                item['call_oi'] = self._norm_float(row.get('openInterest'))
                item['call_ltp'] = self._norm_float(row.get('lastTradedPrice'))
            if opt == 'PE':
                item['put_oi'] = self._norm_float(row.get('openInterest'))
                item['put_ltp'] = self._norm_float(row.get('lastTradedPrice'))

        normalized = sorted(chain_by_strike.values(), key=lambda x: x['strike_price'])
        self.cache.set(key, normalized, settings.option_chain_ttl)
        return normalized

    def calculate_pcr(self, chain: list[dict[str, Any]]) -> float:
        call_oi = sum(self._norm_float(r.get('call_oi')) for r in chain)
        put_oi = sum(self._norm_float(r.get('put_oi')) for r in chain)
        return put_oi / max(1.0, call_oi)

    def get_option_chain_bias(self, chain: list[dict[str, Any]]) -> str:
        if not chain:
            return 'NEUTRAL'
        pcr = self.calculate_pcr(chain)
        if pcr >= 1.1:
            return 'BULLISH'
        if pcr <= 0.9:
            return 'BEARISH'
        return 'NEUTRAL'

    def select_atm_strike(self, spot_price: float, chain: list[dict[str, Any]]) -> float:
        if not chain:
            return round(spot_price / 50.0) * 50.0
        return min(chain, key=lambda x: abs(self._norm_float(x.get('strike_price')) - spot_price))['strike_price']

    def build_option_symbol(self, underlying: str, expiry: str, strike: float, option_side: str) -> str:
        expiry_code = SamcoClient.to_expiry_code(expiry)
        suffix = 'CE' if option_side.upper() == 'CALL' else 'PE'
        return f'{underlying.upper()}{expiry_code}{int(round(strike))}{suffix}'

    def pick_option_contract(self, chain: list[dict[str, Any]], spot_price: float, option_side: str, symbol: str, expiry: str) -> dict[str, Any]:
        strike = self.select_atm_strike(spot_price, chain)
        row = next((item for item in chain if float(item.get('strike_price') or 0) == float(strike)), None)
        if not row:
            return {}
        ltp_key = 'call_ltp' if option_side.upper() == 'CALL' else 'put_ltp'
        premium = self._norm_float(row.get(ltp_key))
        return {
            'option_symbol': self.build_option_symbol(symbol, expiry, strike, option_side),
            'strike': strike,
            'premium': premium,
            'expiry': SamcoClient.to_expiry_api_date(expiry),
            'option_type': 'CE' if option_side.upper() == 'CALL' else 'PE',
        }
