import asyncio

import httpx

from lords_bot.app.auth import AuthService


def test_refresh_fallback_endpoint_on_404(monkeypatch):
    svc = AuthService()
    svc.refresh_token = "r1"

    calls = {"n": 0}

    async def fake_post(self, url, json):
        calls["n"] += 1
        if url.endswith("/validate-refresh-token"):
            return httpx.Response(404, text="not found")
        if url.endswith("/token"):
            return httpx.Response(200, json={"s": "ok", "access_token": "new"})
        return httpx.Response(500, json={"s": "error"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    out = asyncio.run(svc.refresh_access_token())
    assert out["access_token"] == "new"
    assert svc.access_token == "new"
    assert calls["n"] >= 2
