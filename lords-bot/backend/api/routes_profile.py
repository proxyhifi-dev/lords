from __future__ import annotations

from fastapi import APIRouter

from main_dependencies import profile_service

router = APIRouter(tags=['profile'])


@router.get('/profile')
async def get_profile() -> dict:
    return await profile_service.get_profile()
