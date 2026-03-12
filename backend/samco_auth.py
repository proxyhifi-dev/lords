from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from config import settings

logger = logging.getLogger("samco.auth")


class AuthError(RuntimeError):
    pass


@dataclass
class SamcoSession:
    token: str | None = None


class SamcoAuthManager:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._session = SamcoSession(token=None)
        self._lock = asyncio.Lock()

    @property
    def token(self) -> str | None:
        return self._session.token

    async def login(self) -> str:
        async with self._lock:
            if self._session.token:
                return self._session.token

            payload = {
                "userId": settings.samco_user_id,
                "password": settings.samco_password,
                "yob": settings.samco_yob,
            }
            logger.info("samco_login_attempt", extra={"user_id": settings.samco_user_id})
            response = await self._client.post("/login", json=payload)
            if response.status_code >= 400:
                logger.error("samco_login_failed", extra={"status": response.status_code, "body": response.text})
                raise AuthError(f"Login failed with status {response.status_code}")

            data = response.json()
            token = data.get("sessionToken") or data.get("data", {}).get("sessionToken")
            if not token:
                raise AuthError("sessionToken missing in login response")

            self._session.token = token
            logger.info("samco_login_success")
            return token

    async def invalidate(self) -> None:
        async with self._lock:
            self._session.token = None
            logger.warning("samco_token_invalidated")
