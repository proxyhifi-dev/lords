from __future__ import annotations

from fastapi import APIRouter

from config import settings
from main_dependencies import option_chain_service

router = APIRouter(tags=['option-chain'])


@router.get('/option-chain')
async def option_chain() -> dict:
    chain = await option_chain_service.get_option_chain(settings.symbol, settings.expiry)
    live_expiry = option_chain_service.get_live_expiry(settings.symbol, settings.expiry)
    return {
        'symbol': settings.symbol,
        'expiry': live_expiry,
        'pcr': option_chain_service.calculate_pcr(chain),
        'bias': option_chain_service.get_option_chain_bias(chain),
        'records': chain,
    }
