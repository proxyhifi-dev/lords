from __future__ import annotations
from backend.app.broker.samco_client import get_expiry_api
from backend.app.core.config_loader import get_settings

settings = get_settings()


class OptionSelector:

    @staticmethod
    def get_atm_strike(spot: float, step: int = 50) -> int:
        return int(round(spot / step) * step)

    @staticmethod
    def get_otm_strike(spot: float, signal: str, distance: int | None = None, step: int = 50) -> int:
        dist = distance if distance is not None else settings.otm_distance
        atm  = OptionSelector.get_atm_strike(spot, step)
        return atm + dist * step if signal.upper() == "CALL" else atm - dist * step

    @staticmethod
    def get_option_type(signal: str) -> str:
        return "CE" if signal.upper() == "CALL" else "PE"

    @staticmethod
    def get_expiry_api() -> str:
        return get_expiry_api()
