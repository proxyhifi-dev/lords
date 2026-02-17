import httpx
import logging
from app.config import get_settings

logger = logging.getLogger(__name__)


class FyersClient:

    def __init__(self, auth_service):
        self.settings = get_settings()
        self.auth = auth_service

    async def request(self, method: str, endpoint: str, json: dict | None = None):
        url = f"{self.settings.fyers_base_url}{endpoint}"

        headers = {
            "Authorization": f"{self.settings.fyers_app_id}:{self.auth.access_token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=json,
            )

        print("REQUEST URL:", url)
        print("STATUS:", response.status_code)
        print("BODY:", response.text)

        response.raise_for_status()
        return response.json()
