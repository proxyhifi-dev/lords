from __future__ import annotations

from fastapi import APIRouter

from backend.runtime_state import runtime_state

router = APIRouter(tags=['signals'])


@router.get('/signals')
async def get_signals() -> dict:
    return runtime_state.latest_signal
