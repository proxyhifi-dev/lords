from __future__ import annotations

from fastapi import APIRouter

from main_dependencies import dashboard_service, funds_service, profile_service

router = APIRouter(tags=['dashboard'])


@router.get('/dashboard')
async def get_dashboard() -> dict:
    profile = await profile_service.get_profile()
    funds = await funds_service.get_funds()
    return dashboard_service.build_dashboard(profile=profile, funds=funds)
