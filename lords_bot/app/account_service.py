from app.fyers_client import FyersClient


class AccountService:
    def __init__(self, client: FyersClient) -> None:
        self.client = client

    async def get_profile(self) -> dict:
        return await self.client.request("GET", "/profile")

    async def get_funds(self) -> dict:
        return await self.client.request("GET", "/funds")

    async def get_holdings(self) -> dict:
        return await self.client.request("GET", "/holdings")

    async def get_positions(self) -> dict:
        return await self.client.request("GET", "/positions")

    async def get_orders(self) -> dict:
        return await self.client.request("GET", "/orders")
