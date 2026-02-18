from __future__ import annotations

import asyncio
import hashlib
import logging
import webbrowser
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import uvicorn
from fastapi import FastAPI, Request

from lords_bot.app.config import get_settings
from lords_bot.app.token_store import TokenStore

logger = logging.getLogger("lords_bot.auth")


class AuthService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self._refresh_lock = asyncio.Lock()

    def _app_id_hash(self) -> str:
        raw = f"{self.settings.fyers_app_id}:{self.settings.fyers_secret}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _auth_base_url(self) -> str:
        return str(self.settings.fyers_auth_url).rstrip("/")

    def _login_url(self) -> str:
        return (
            f"{self._auth_base_url()}/generate-authcode"
            f"?client_id={self.settings.fyers_app_id}"
            f"&redirect_uri={self.settings.fyers_redirect_uri}"
            "&response_type=code"
            "&state=lords-bot"
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except ValueError:
            return {}

    async def validate_auth_code(self, auth_code: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self._auth_base_url()}/validate-authcode",
                json={"grant_type": "authorization_code", "appIdHash": self._app_id_hash(), "code": auth_code},
            )

        payload = self._safe_json(response)
        if response.status_code >= 400 or payload.get("s") == "error":
            raise RuntimeError(payload.get("message", "validate-authcode failed"))

        access_token = payload.get("access_token")
        if not access_token:
            raise RuntimeError("validate-authcode response missing access_token")

        self.access_token = access_token
        self.refresh_token = payload.get("refresh_token")
        TokenStore.save({"access_token": self.access_token, "refresh_token": self.refresh_token})
        logger.info("FYERS access token generated and stored.")
        return payload

    async def refresh_access_token(self) -> dict[str, Any]:
        """Refresh token safely with lock; on failure caller should fallback to auto_login."""
        async with self._refresh_lock:
            if not self.refresh_token:
                cached = TokenStore.load() or {}
                self.refresh_token = cached.get("refresh_token")
            if not self.refresh_token:
                raise RuntimeError("Refresh token unavailable; complete login flow")

            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self._auth_base_url()}/token",
                    json={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
                )

            payload = self._safe_json(response)
            if response.status_code >= 400 or payload.get("s") == "error":
                raise RuntimeError(payload.get("message", "token refresh failed"))

            access_token = payload.get("access_token")
            if not access_token:
                raise RuntimeError("Refresh response missing access_token")

            self.access_token = access_token
            self.refresh_token = payload.get("refresh_token", self.refresh_token)
            TokenStore.save({"access_token": self.access_token, "refresh_token": self.refresh_token})
            logger.info("FYERS token refreshed.")
            return payload

    async def auto_login(self) -> None:
        token_data = TokenStore.load()
        if token_data and not TokenStore.is_expired(token_data):
            self.access_token = token_data.get("access_token")
            self.refresh_token = token_data.get("refresh_token")
            if self.access_token:
                logger.info("Using cached FYERS access token.")
                return

        if token_data and token_data.get("refresh_token"):
            self.refresh_token = token_data.get("refresh_token")
            try:
                await self.refresh_access_token()
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cached refresh failed, falling back to auth-code login: %s", exc)

        logger.info("No valid token found; starting FYERS auth-code login flow.")
        auth_code = await self._capture_auth_code()
        await self.validate_auth_code(auth_code)

    async def _capture_auth_code(self) -> str:
        app = FastAPI()
        state: dict[str, str | None] = {"auth_code": None}

        @app.get("/")
        async def callback(request: Request) -> dict[str, str]:
            auth_code = request.query_params.get("auth_code")
            if not auth_code:
                redirect_uri = request.query_params.get("redirect")
                if redirect_uri:
                    auth_code = parse_qs(urlparse(redirect_uri).query).get("auth_code", [None])[0]
            if not auth_code:
                return {"message": "Missing auth_code in callback."}
            state["auth_code"] = auth_code
            return {"message": "Login successful. You can close this tab."}

        config = uvicorn.Config(app=app, host="127.0.0.1", port=8080, log_level="error")
        server = uvicorn.Server(config)
        webbrowser.open(self._login_url())

        server_task = asyncio.create_task(server.serve())
        try:
            while not state["auth_code"]:
                await asyncio.sleep(0.2)
        finally:
            server.should_exit = True
            await server_task

        return str(state["auth_code"])
