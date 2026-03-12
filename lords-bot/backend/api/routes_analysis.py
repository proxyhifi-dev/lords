from __future__ import annotations

from fastapi import APIRouter

from backend.runtime_state import runtime_state

router = APIRouter(tags=['analysis'])


@router.get('/analysis')
async def get_analysis() -> dict:
    return runtime_state.latest_analysis
