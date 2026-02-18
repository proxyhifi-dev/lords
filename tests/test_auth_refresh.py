import asyncio

import httpx

from lords_bot.app.auth import AuthService


def test_refresh_uses_token_endpoint(monkeypatch):
    svc = AuthService()
    svc.refresh_token = "r1"

    calls = {"url": None}

    async def fake_post(self, url, json):
        calls["url"] = url
        return httpx.Response(200, json={"s": "ok", "access_token": "new"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    out = asyncio.run(svc.refresh_access_token())
    assert out["access_token"] == "new"
    assert svc.access_token == "new"
    assert calls["url"].endswith("/token")
