import os
import time
import asyncio
import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.stocknote.com"

API_KEY = os.getenv("SAMCO_API_KEY")
ACCESS_TOKEN = os.getenv("SAMCO_ACCESS_TOKEN")

HEADERS = {
    "x-api-key": API_KEY,
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Content-Type": "application/json"
}


class SamcoClient:

    def __init__(self):
        self.last_request_time = 0
        self.rate_limit_seconds = 1

    async def _rate_limit(self):
        now = time.time()
        diff = now - self.last_request_time

        if diff < self.rate_limit_seconds:
            await asyncio.sleep(self.rate_limit_seconds - diff)

        self.last_request_time = time.time()

    async def _request(self, endpoint, params=None):

        await self._rate_limit()

        url = f"{BASE_URL}{endpoint}"
        retries = 3

        async with httpx.AsyncClient(timeout=10) as client:

            for attempt in range(retries):

                try:

                    response = await client.get(
                        url,
                        headers=HEADERS,
                        params=params
                    )

                    if response.status_code == 200:
                        return response.json()

                    if response.status_code == 429:
                        await asyncio.sleep(2)
                        continue

                    if response.status_code >= 500:
                        await asyncio.sleep(1)
                        continue

                except Exception:
                    await asyncio.sleep(1)

        print(f"WARNING: API failed -> {endpoint}")
        return {}

    # ----------------------------------
    # OPTION CHAIN
    # ----------------------------------

    async def get_option_chain(self, symbol="NIFTY", expiry=None):

        params = {
            "exchange": "NFO",
            "searchSymbolName": symbol
        }

        if expiry:
            params["expiry"] = expiry

        data = await self._request(
            "/option/optionChain",
            params=params
        )

        if not data:
            return []

        return data

    # ----------------------------------
    # UNDERLYING PRICE
    # ----------------------------------

    async def get_underlying_price(self, symbol="NIFTY"):

        params = {
            "symbolName": symbol,
            "exchange": "NSE"
        }

        data = await self._request(
            "/market/quote",
            params=params
        )

        try:
            # extract price from API response
            price = float(data["data"]["lastTradedPrice"])
            return price
        except Exception:
            return 0.0

    # ----------------------------------
    # PROFILE
    # ----------------------------------

    async def get_profile(self):

        data = await self._request("/user/profile")

        if not data:
            return {"status": "offline"}

        return data

    # ----------------------------------
    # FUNDS
    # ----------------------------------

    async def get_funds(self):

        data = await self._request("/user/funds")

        if not data:
            return {"balance": 0}

        return data

    # ----------------------------------
    # ORDERS
    # ----------------------------------

    async def get_orders(self):

        data = await self._request("/order/orders")

        if not data:
            return []

        return data


# GLOBAL INSTANCE
samco_client = SamcoClient()