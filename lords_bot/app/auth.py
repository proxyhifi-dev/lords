from __future__ import annotations

import asyncio
import hashlib
import logging
import webbrowser
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request

from lords_bot.app.config import get_settings
from lords_bot.app.token_store import TokenStore

logger = logging.getLogger("lords_bot.auth")


class AuthService:
    """Handles FYERS authentication, token storage, and auto login flow."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self._refresh_lock = asyncio.Lock()

    def _app_id_hash(self) -> str:
        raw = f"{self.settings.fyers_app_id}:{self.settings.fyers_secret}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


    def _auth_base_url(self) -> str:
        # Always use the dynamic property for the correct environment
        return str(self.settings.fyers_auth_url).rstrip("/")

    def _login_url(self) -> str:
        """
        FYERS auth code URL.
        """
        return (
            f"{self._auth_base_url()}/generate-authcode"
            f"?client_id={self.settings.fyers_app_id}"
            f"&redirect_uri={self.settings.fyers_redirect_uri}"
            f"&response_type=code&state=lords-bot"
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any]:
        try:
            return response.json() if isinstance(response.json(), dict) else {}
        except ValueError:
            return {}

    async def validate_auth_code(self, auth_code: str) -> dict[str, Any]:
        """
        Exchange auth code for access token & refresh token.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self._auth_base_url()}/validate-authcode",
                json={
                    "grant_type": "authorization_code",
                    "appIdHash": self._app_id_hash(),
                    "code": auth_code,
                },
            )

        payload = self._safe_json(response)

        if response.status_code >= 400 or payload.get("s") == "error":
            raise RuntimeError(payload.get("message", "validate-authcode failed"))

        self.access_token = payload.get("access_token")
        self.refresh_token = payload.get("refresh_token")

        if not self.access_token:
            raise RuntimeError("validate-authcode missing access_token")

        TokenStore.save({"access_token": self.access_token, "refresh_token": self.refresh_token})
        logger.info("FYERS access token generated and stored.")
        return payload

    async def auto_login(self) -> None:
        """
        Try to load cached token, if expired or invalid then start auth.
        Avoid refresh endpoint — FYERS V3 requires full auth code flow.
        """

        # Try load cached
        token_data = TokenStore.load()
        if token_data:
            self.access_token = token_data.get("access_token")
            self.refresh_token = token_data.get("refresh_token")

            if self.access_token:
                logger.info("Using cached FYERS access token.")
                return

        # No token or expired → start full login flow
        logger.info("No valid token; starting login flow.")
        auth_code = await self._capture_auth_code()
        await self.validate_auth_code(auth_code)

    async def _capture_auth_code(self) -> str:
        """
        Open redirect login flow and get auth_code via local FastAPI callback.
        """

        app = FastAPI()
        state: dict[str, str | None] = {"auth_code": None}

        @app.get("/")
        async def callback(request: Request) -> dict[str, str]:
            code = request.query_params.get("auth_code")
            if not code:
                # fallback attempt to parse from redirect param if present
                redirect_uri = request.query_params.get("redirect")
                if redirect_uri:
                    parsed = redirect_uri.split("auth_code=")
                    if len(parsed) > 1:
                        code = parsed[1].split("&")[0]
            if not code:
                return {"message": "auth_code missing in callback"}

            state["auth_code"] = code
            return {"message": "Login successful — you can close this tab."}

        # Run local server
        config = uvicorn.Config(app=app, host="127.0.0.1", port=self.settings.auth_callback_port, log_level="error")
        server = uvicorn.Server(config)

        # Open user browser to login
        webbrowser.open(self._login_url())

        # Run server until auth_code arrives
        server_task = asyncio.create_task(server.serve())
        try:
            while not state["auth_code"]:
                await asyncio.sleep(0.2)
        finally:
            # Trigger shutdown
            server.should_exit = True
            await server_task

        return str(state["auth_code"])
