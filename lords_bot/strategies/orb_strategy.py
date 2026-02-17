import datetime


class ORBStrategy:

    def __init__(self, client):
        self.client = client
        self.range_high = None
        self.range_low = None

    async def fetch_orb_range(self):

        today = datetime.date.today()
        date_str = today.strftime("%Y-%m-%d")

        response = await self.client.request(
            "GET",
            "/history",
            params={
                "symbol": "NSE:NIFTY50-INDEX",
                "resolution": "5",
                "date_format": "1",
                "range_from": date_str,
                "range_to": date_str,
                "cont_flag": "1",
            },
        )

        candles = response.get("candles", [])

        if len(candles) < 3:
            raise RuntimeError("Not enough candles")

        first_three = candles[:3]

        highs = [c[2] for c in first_three]
        lows = [c[3] for c in first_three]

        self.range_high = max(highs)
        self.range_low = min(lows)

    async def check_breakout(self):

        if self.range_high is None or self.range_low is None:
            raise RuntimeError("ORB range not set")

        quote = await self.client.request(
            "GET",
            "/quotes",
            params={"symbols": "NSE:NIFTY50-INDEX"},
        )

        ltp = quote["d"][0]["v"]["lp"]

        if ltp > self.range_high:
            return {"direction": "CALL", "price": ltp}

        if ltp < self.range_low:
            return {"direction": "PUT", "price": ltp}

        return None
