from __future__ import annotations

from fastapi import Header, HTTPException

from config import settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = settings.api_key.strip()
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail='invalid_api_key')
