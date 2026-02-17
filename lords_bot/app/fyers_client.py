from __future__ import annotations

import logging
from typing import Any

import httpx

from lords_bot.app.config import get_settings

logger = logging.getLogger(__name__)


class FyersAPIError(RuntimeError):
    """Raised when FYERS API returns an error payload or non-2xx status."""

    def __init__(self, message: str, *, status_code: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class FyersClient:
    """
    Unified FYERS v3 Client

    • Uses TRADING URL for:
        - profile
        - funds
        - orders
        - positions

    • Uses DATA URL for:
        - history
        - quotes
        - option chain
    """

    def __init__(self, auth_service: Any) -> None:
        self.settings = get_settings()
        self.auth = auth_service

    # ------------------------------------------------------------
    # URL Router
    # ------------------------------------------------------------

    def _resolve_base_url(self, endpoint: str) -> str:
        """
        Automatically select correct base URL.
        """

        endpoint = endpoint.lstrip("/")

        # Data APIs
        if endpoint.startswith(("history", "quotes", "options")):
            return str(self.settings.fyers_data_url).rstrip("/")

        # Trading APIs
        return str(self.settings.fyers_trading_url).rstrip("/")

    # ------------------------------------------------------------
    # Request Wrapper
    # ------------------------------------------------------------

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:

        if not self.auth.access_token:
            raise FyersAPIError("Access token missing. Complete login first.")

        base_url = self._resolve_base_url(endpoint)
        url = f"{base_url}/{endpoint.lstrip('/')}"

        headers = {
            "Authorization": f"{self.settings.fyers_app_id}:{self.auth.access_token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                method=method.upper(),
                url=url,
                headers=headers,
                params=params,
                json=data,
            )

        try:
            payload = response.json()
        except ValueError:
            logger.error(
                "Non-JSON FYERS response (status %s): %s",
                response.status_code,
                response.text[:300],
            )
            raise FyersAPIError(
                f"Non-JSON FYERS response: {response.text[:200]}",
                status_code=response.status_code,
            )

        if response.status_code >= 400:
            raise FyersAPIError(
                payload.get("message", "FYERS request failed"),
                status_code=response.status_code,
                code=payload.get("code"),
            )

        if payload.get("s") == "error":
            raise FyersAPIError(
                payload.get("message", "FYERS returned error"),
                status_code=response.status_code,
                code=payload.get("code"),
            )

        logger.debug("FYERS %s %s OK", method.upper(), endpoint)
        return payload
