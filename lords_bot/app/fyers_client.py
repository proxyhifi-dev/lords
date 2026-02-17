import asyncio
import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.auth import AuthService
from app.config import get_settings
from app.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

FYERS_ERROR_MESSAGES = {
    -8: "Invalid input or request payload.",
    -15: "Token invalid/expired. Re-authentication required.",
    -50: "Order rejected due to risk or margin checks.",
}


class FyersAPIError(Exception):
    def __init__(self, message: str, code: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


class FyersClient:
    def __init__(self, auth_service: AuthService):
        self.auth = auth_service
        self.settings = get_settings()
        self.limiter = RateLimiter(per_second=10, per_minute=200)
        self._refresh_lock = asyncio.Lock()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, FyersAPIError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        reraise=True,
    )
    async def request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.limiter.acquire()

        if not self.auth.access_token:
            raise RuntimeError("Missing access token. Call validate_auth_code first.")

        headers = {
            "Authorization": self.auth.access_token,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(
                method=method,
                url=f"{self.settings.fyers_base_url}{endpoint}",
                headers=headers,
                json=data,
                params=params,
            )

        if response.status_code == 401:
            await self._refresh_and_retry_once(method, endpoint, data, params)
            return await self.request(method, endpoint, data, params)

        if response.status_code in {429, 500, 502, 503, 504}:
            logger.warning("Retryable status code received: %s", response.status_code)
            response.raise_for_status()

        response.raise_for_status()
        payload = response.json()
        self._raise_for_fyers_error(payload)
        return payload

    async def _refresh_and_retry_once(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None,
        params: dict[str, Any] | None,
    ) -> None:
        async with self._refresh_lock:
            await self.auth.refresh_access_token()
            logger.info("Token refreshed after 401 for %s %s", method, endpoint)

    @staticmethod
    def _raise_for_fyers_error(payload: dict[str, Any]) -> None:
        code = payload.get("code")
        if code is None or code == 200:
            return

        message = payload.get("message") or FYERS_ERROR_MESSAGES.get(code) or "FYERS API error"
        raise FyersAPIError(message=message, code=code, payload=payload)
