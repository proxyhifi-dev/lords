from __future__ import annotations

from dataclasses import dataclass

from config import settings


@dataclass
class SamcoAuth:
    access_token: str = ''

    async def login(self) -> str:
        self.access_token = settings.samco_access_token or 'SIMULATED_TOKEN'
        return self.access_token


samco_auth = SamcoAuth()
