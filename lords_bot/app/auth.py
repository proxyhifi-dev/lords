import hashlib
import logging
from dataclasses import dataclass

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str


class AuthService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._tokens: TokenPair | None = None

    @property
    def access_token(self) -> str | None:
        return self._tokens.access_token if self._tokens else None

    def _app_id_hash(self) -> str:
        raw = f"{self.settings.fyers_app_id}:{self.settings.fyers_secret}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def validate_auth_code(self, auth_code: str) -> TokenPair:
        payload = {
            "grant_type": "authorization_code",
            "appIdHash": self._app_id_hash(),
            "code": auth_code,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.settings.fyers_base_url}/validate-authcode", json=payload
            )
            response.raise_for_status()
            data = response.json()

        self._tokens = TokenPair(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
        )
        logger.info("Access token obtained from auth code.")
        return self._tokens

    async def refresh_access_token(self) -> str:
        if not self._tokens:
            raise RuntimeError("refresh token unavailable; validate auth code first")

        payload = {
            "grant_type": "refresh_token",
            "appIdHash": self._app_id_hash(),
            "refresh_token": self._tokens.refresh_token,
            "pin": self.settings.fyers_pin,
        }

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.settings.fyers_base_url}/validate-refresh-token", json=payload
            )
            response.raise_for_status()
            data = response.json()

        self._tokens = TokenPair(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", self._tokens.refresh_token),
        )
        logger.info("Access token refreshed.")
        return self._tokens.access_token
