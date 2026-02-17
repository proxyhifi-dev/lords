import datetime
from typing import Dict, Any


class OptionSelector:
    """
    Dynamically selects ATM NIFTY weekly option (CE / PE)
    based on current NIFTY price.
    """

    def __init__(self, client):
        self.client = client

    async def get_nifty_ltp(self) -> float:
        response = await self.client.request(
            "GET",
            "/quotes",
            params={"symbols": "NSE:NIFTY50-INDEX"},
        )

        return response["d"][0]["v"]["lp"]

    def _round_to_50(self, price: float) -> int:
        return int(round(price / 50) * 50)

    def _get_next_thursday(self) -> datetime.date:
        today = datetime.date.today()
        days_ahead = 3 - today.weekday()  # Thursday = 3
        if days_ahead <= 0:
            days_ahead += 7
        return today + datetime.timedelta(days=days_ahead)

    def _format_expiry(self, expiry_date: datetime.date) -> str:
        """
        Format expiry as per Fyers weekly format:
        Example: 25FEB
        """
        return expiry_date.strftime("%y%b").upper()

    async def select_option(self, direction: str) -> Dict[str, Any]:
        """
        direction = "CALL" or "PUT"
        """

        nifty_price = await self.get_nifty_ltp()
        strike = self._round_to_50(nifty_price)
        expiry = self._get_next_thursday()
        expiry_code = self._format_expiry(expiry)

        option_type = "CE" if direction == "CALL" else "PE"

        symbol = f"NSE:NIFTY{expiry_code}{strike}{option_type}"

        quote = await self.client.request(
            "GET",
            "/quotes",
            params={"symbols": symbol},
        )

        ltp = quote["d"][0]["v"]["lp"]

        return {
            "symbol": symbol,
            "strike": strike,
            "expiry": expiry_code,
            "type": option_type,
            "ltp": ltp,
            "underlying_price": nifty_price,
        }
