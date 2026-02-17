from __future__ import annotations

import datetime as dt
from typing import Any


class OptionSelector:
    def __init__(self, client) -> None:
        self.client = client

    async def get_nifty_ltp(self) -> float:
        response = await self.client.request("GET", "/quotes", params={"symbols": "NSE:NIFTY50-INDEX"})
        return float(response["d"][0]["v"]["lp"])

    @staticmethod
    def _round_to_50(price: float) -> int:
        return int(round(price / 50.0) * 50)

    @staticmethod
    def _safe_ltp(record: dict[str, Any]) -> float | None:
        lp = record.get("ltp")
        if lp is None:
            lp = record.get("v", {}).get("lp")
        return float(lp) if lp is not None else None

    async def select_option(self, direction: str) -> dict[str, Any]:
        side = direction.upper()
        if side not in {"CALL", "PUT"}:
            raise ValueError("direction must be CALL or PUT")

        nifty_ltp = await self.get_nifty_ltp()
        atm_strike = self._round_to_50(nifty_ltp)

        chain = await self.client.request(
            "GET",
            "/options-chain-v3",
            params={"symbol": "NSE:NIFTY50-INDEX", "strikecount": "20", "timestamp": ""},
        )

        options = chain.get("optionsChain") or chain.get("data") or []
        if not options:
            raise RuntimeError("No option chain data returned from FYERS.")

        option_type = "CE" if side == "CALL" else "PE"
        today = dt.date.today()

        def score(item: dict[str, Any]) -> tuple[int, int]:
            strike = int(float(item.get("strike_price", item.get("strike", 0))))
            expiry_raw = item.get("expiry") or item.get("expiryDate") or ""
            try:
                expiry = dt.datetime.fromtimestamp(int(expiry_raw)).date() if str(expiry_raw).isdigit() else dt.date.fromisoformat(str(expiry_raw)[:10])
            except Exception:
                expiry = today
            expiry_gap = (expiry - today).days if expiry >= today else 999
            strike_gap = abs(strike - atm_strike)
            return (expiry_gap, strike_gap)

        filtered: list[dict[str, Any]] = [
            item
            for item in options
            if str(item.get("option_type", item.get("type", ""))).upper().endswith(option_type)
        ]
        if not filtered:
            raise RuntimeError(f"No {option_type} contracts found in option chain response.")

        selected = min(filtered, key=score)
        symbol = selected.get("symbol") or selected.get("symbol_name")
        strike = int(float(selected.get("strike_price", selected.get("strike", atm_strike))))

        if not symbol:
            raise RuntimeError("Option chain response missing tradable symbol.")

        quote = await self.client.request("GET", "/quotes", params={"symbols": symbol})
        option_ltp = float(quote["d"][0]["v"]["lp"])

        return {
            "symbol": symbol,
            "strike": strike,
            "type": option_type,
            "ltp": option_ltp,
            "underlying_price": nifty_ltp,
            "atm_strike": atm_strike,
            "direction": side,
        }
