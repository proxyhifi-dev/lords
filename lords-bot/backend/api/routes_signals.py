from __future__ import annotations

from fastapi import APIRouter

from main_dependencies import scheduler

router = APIRouter(tags=['signals'])


@router.get('/signals/latest')
async def latest_signal() -> dict:
    return scheduler.state.latest_signal
