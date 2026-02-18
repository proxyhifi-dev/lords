import asyncio

import httpx
import pytest

from lords_bot.app.fyers_client import FyersAPIError, FyersClient


class DummyAuth:
    def __init__(self):
        self.access_token = "t1"
        self.refreshed = False

    async def refresh_access_token(self):
        self.access_token = "t2"
        self.refreshed = True


def test_retry_on_503(monkeypatch):
    auth = DummyAuth()
    client = FyersClient(auth)
    calls = {"n": 0}

    async def fake_request(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"s": "ok", "foo": "bar"})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    out = asyncio.run(client.request("GET", "/quotes", params={"symbols": "NSE:NIFTY50-INDEX"}))
    assert out["foo"] == "bar"
    assert calls["n"] == 2


def test_refresh_on_401(monkeypatch):
    auth = DummyAuth()
    client = FyersClient(auth)
    calls = {"n": 0}

    async def fake_request(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"s": "error", "message": "expired"})
        return httpx.Response(200, json={"s": "ok"})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    out = asyncio.run(client.request("GET", "/quotes", params={"symbols": "NSE:NIFTY50-INDEX"}))
    assert out["s"] == "ok"
    assert auth.refreshed is True


def test_circuit_breaker_engages(monkeypatch):
    auth = DummyAuth()
    client = FyersClient(auth)
    client.failure_threshold = 2
    client.max_retries = 0

    async def fake_request(self, *args, **kwargs):
        return httpx.Response(503, text="busy")

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    with pytest.raises(FyersAPIError):
        asyncio.run(client.request("GET", "/quotes", params={"symbols": "NSE:NIFTY50-INDEX"}))
    with pytest.raises(FyersAPIError):
        asyncio.run(client.request("GET", "/quotes", params={"symbols": "NSE:NIFTY50-INDEX"}))

    assert client.is_trading_paused() is True
