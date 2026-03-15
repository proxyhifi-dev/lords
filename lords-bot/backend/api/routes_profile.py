from __future__ import annotations

from fastapi import APIRouter

from brokers.samco_client import samco_client

router = APIRouter(tags=['profile'])


@router.get('/profile')
async def profile() -> dict:
    return await samco_client.user_details()
