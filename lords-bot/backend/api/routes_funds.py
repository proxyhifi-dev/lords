from __future__ import annotations

from fastapi import APIRouter

from brokers.samco_client import samco_client

router = APIRouter(tags=['funds'])


@router.get('/funds')
async def funds() -> dict:
    return await samco_client.get_limits()
