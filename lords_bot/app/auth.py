import asyncio
import hashlib
import logging
import webbrowser
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI

from app.config import get_settings
from app.token_store import TokenStore

logger = logging.getLogger(__name__)


class AuthService:

    def __init__(self) -> None:
        self.settings = get_settings()
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self._refresh_lock = asyncio.Lock()

    # ----------------------------------------------------------
    # CORRECT HASH (Official FYERS v3)
    # ----------------------------------------------------------

    def _generate_hash(self) -> str:
        app_id = self.settings.fyers_app_id.strip()
        secret = self.settings.fyers_secret.strip()

        raw_string = f"{app_id}:{secret}"
        hash_value = hashlib.sha256(raw_string.encode("utf-8")).hexdigest()

        print("DEBUG HASH INPUT:", raw_string)
        print("DEBUG HASH OUTPUT:", hash_value)

        return hash_value

    # ----------------------------------------------------------

    def _login_url(self) -> str:
        return (
            "https://api-t1.fyers.in/api/v3/generate-authcode"
            f"?client_id={self.settings.fyers_app_id}"
            f"&redirect_uri={self.settings.fyers_redirect_uri}"
            "&response_type=code"
            "&state=autologin"
        )

    # ----------------------------------------------------------

    async def validate_auth_code(self, auth_code: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://api-t1.fyers.in/api/v3/validate-authcode",
                json={
                    "grant_type": "authorization_code",
                    "appIdHash": self._generate_hash(),
                    "code": auth_code,
                },
            )

        print("STATUS:", response.status_code)
        print("BODY:", response.text)

        if response.status_code != 200:
            raise RuntimeError("validate-authcode failed")

        data = response.json()

        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]

        TokenStore.save(data)

        logger.info("Access token generated and stored.")
        return data

    # ----------------------------------------------------------

    async def auto_login(self) -> None:
        token_data = TokenStore.load()

        if token_data and not TokenStore.is_expired(token_data):
            self.access_token = token_data["access_token"]
            self.refresh_token = token_data["refresh_token"]
            logger.info("Using stored access token.")
            return

        logger.info("First-time login required. Opening browser...")

        app = FastAPI()
        auth_code_holder: dict[str, str | None] = {"code": None}

        @app.get("/")
        async def capture_code(auth_code: str):
            auth_code_holder["code"] = auth_code
            return {"message": "Login successful. Close this window."}

        config = uvicorn.Config(app, host="127.0.0.1", port=8080, log_level="error")
        server = uvicorn.Server(config)

        webbrowser.open(self._login_url())
        server_task = asyncio.create_task(server.serve())

        while not auth_code_holder["code"]:
            await asyncio.sleep(0.5)

        server.should_exit = True
        await server_task

        await self.validate_auth_code(auth_code_holder["code"])

        logger.info("Login completed.")
