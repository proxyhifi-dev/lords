from __future__ import annotations
from datetime import datetime, timedelta


class OptionSelector:

    STRIKE_STEP = 50

    @staticmethod
    def get_atm_strike(spot: float) -> int:
        step = OptionSelector.STRIKE_STEP
        return int(round(spot / step) * step)

    @staticmethod
    def get_dynamic_strike(spot: float, vwap: float, signal: str) -> int:
        step = OptionSelector.STRIKE_STEP
        atm = OptionSelector.get_atm_strike(spot)
        if signal == "CALL":
            return atm if spot > vwap else atm - step
        if signal == "PUT":
            return atm if spot < vwap else atm + step
        return atm

    @staticmethod
    def get_option_type(signal: str) -> str:
        return "CE" if signal == "CALL" else "PE"

    @staticmethod
    def _calc_expiry() -> datetime:
        """
        Returns the next Thursday expiry as a datetime object.
        - Thursday before 15:30 → use today
        - Thursday after 15:30  → roll to next week
        - Any other day         → find coming Thursday
        """
        today = datetime.now()
        days_ahead = (3 - today.weekday()) % 7
        if days_ahead == 0:
            if today.hour > 15 or (today.hour == 15 and today.minute >= 30):
                days_ahead = 7
        return today + timedelta(days=days_ahead)

    @staticmethod
    def get_expiry() -> str:
        """DDMMMYYYY — for display/logging e.g. 16APR2026"""
        return OptionSelector._calc_expiry().strftime("%d%b%Y").upper()

    @staticmethod
    def get_expiry_api() -> str:
        """yyyy-mm-dd — for Samco get_option_chain API e.g. 2026-04-16"""
        return OptionSelector._calc_expiry().strftime("%Y-%m-%d")

    @staticmethod
    def get_otm_strike(spot: float, signal: str, distance: int = 1) -> int:
        step = OptionSelector.STRIKE_STEP
        atm = OptionSelector.get_atm_strike(spot)
        if signal == "CALL":
            return atm + (distance * step)
        if signal == "PUT":
            return atm - (distance * step)
        return atm

    @staticmethod
    def get_strike_range(spot: float, steps: int = 5) -> list[int]:
        atm = OptionSelector.get_atm_strike(spot)
        step = OptionSelector.STRIKE_STEP
        return [atm + i * step for i in range(-steps, steps + 1)]
