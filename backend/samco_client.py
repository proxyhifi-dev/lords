from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from config import settings
from samco_auth import SamcoAuthManager

logger = logging.getLogger("samco.client")


class SamcoClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.samco_base_url,
            timeout=settings.request_timeout_seconds,
            headers={"Accept": "application/json"},
        )
        self.auth = SamcoAuthManager(self._http)

    async def close(self) -> None:
        await self._http.aclose()

    async def _authorized_headers(self) -> dict[str, str]:
        token = self.auth.token or await self.auth.login()
        return {"x-session-token": token}

    async def request(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        retries = settings.max_retries
        for attempt in range(1, retries + 1):
            try:
                headers = await self._authorized_headers()
                response = await self._http.request(method, path, params=params, headers=headers)

                if response.status_code in (401, 403):
                    logger.warning("samco_token_expired", extra={"status": response.status_code})
                    await self.auth.invalidate()
                    if attempt < retries:
                        continue

                if response.status_code >= 400:
                    response.raise_for_status()

                return response.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                logger.error(
                    "samco_request_error",
                    extra={"path": path, "attempt": attempt, "error": str(exc)},
                )
                if attempt == retries:
                    raise
                await asyncio.sleep(settings.retry_backoff_seconds * attempt)

        raise RuntimeError(f"Unable to complete request for {path}")

    async def get_option_chain(self) -> dict[str, Any]:
        params = {
            "exchange": settings.option_exchange,
            "searchSymbolName": settings.nifty_symbol,
            "optionType": settings.option_type,
            "expiryDate": settings.option_expiry,
        }
        return await self.request("GET", "/option/optionChain", params=params)
