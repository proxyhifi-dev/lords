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
    def __init__(self, auth_service: Any) -> None:
        self.settings = get_settings()
        self.auth = auth_service

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

        url = f"{str(self.settings.fyers_base_url).rstrip('/')}/{endpoint.lstrip('/')}"
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
        except ValueError as exc:
            raise FyersAPIError(
                f"Non-JSON FYERS response: {response.text[:200]}",
                status_code=response.status_code,
            ) from exc

        if response.status_code >= 400:
            raise FyersAPIError(
                payload.get("message", "FYERS request failed"),
                status_code=response.status_code,
                code=payload.get("code"),
            )

        if payload.get("s") == "error":
            raise FyersAPIError(
                payload.get("message", "FYERS returned an error"),
                status_code=response.status_code,
                code=payload.get("code"),
            )

        logger.debug("FYERS %s %s OK", method.upper(), endpoint)
        return payload
