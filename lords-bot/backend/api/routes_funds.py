from __future__ import annotations

from fastapi import APIRouter

from main_dependencies import funds_service

router = APIRouter(tags=['funds'])


@router.get('/funds')
async def get_funds() -> dict:
    return await funds_service.get_funds()
