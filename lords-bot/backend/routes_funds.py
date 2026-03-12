from fastapi import APIRouter
import logging

from samco_client import samco_client
from api_cache import get_cached_funds, set_cached_funds

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/funds")
async def get_funds():

    # Check cache
    cached = get_cached_funds()
    if cached:
        return cached

    try:
        data = await samco_client.get_funds()

        set_cached_funds(data)

        return data

    except Exception:
        logger.warning("API failed -> /user/funds")

        return {
            "status": "offline",
            "message": "Funds unavailable"
        }