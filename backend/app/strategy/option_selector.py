from __future__ import annotations
from datetime import datetime, timedelta


class OptionSelector:

    STRIKE_STEP = 50

    # -------------------------------------------------
    # ATM STRIKE
    # -------------------------------------------------
    @staticmethod
    def get_atm_strike(spot: float) -> int:
        step = OptionSelector.STRIKE_STEP
        return int(round(spot / step) * step)

    # -------------------------------------------------
    # DYNAMIC STRIKE (uses VWAP bias)
    # -------------------------------------------------
    @staticmethod
    def get_dynamic_strike(spot: float, vwap: float, signal: str) -> int:
        step = OptionSelector.STRIKE_STEP
        atm = OptionSelector.get_atm_strike(spot)

        if signal == "CALL":
            return atm if spot > vwap else atm - step
        if signal == "PUT":
            return atm if spot < vwap else atm + step
        return atm

    # -------------------------------------------------
    # OPTION TYPE
    # -------------------------------------------------
    @staticmethod
    def get_option_type(signal: str) -> str:
        return "CE" if signal == "CALL" else "PE"

    # -------------------------------------------------
    # EXPIRY — DDMMMYYYY format (Samco API requirement)
    # Returns nearest weekly expiry (Thu for Nifty)
    # -------------------------------------------------
    @staticmethod
    def get_expiry() -> str:
        """
        Returns next Thursday expiry in DDMMMYYYY format.
        e.g.  "30JAN2025"
        If today IS Thursday and market hasn't expired yet, use today.
        """
        today = datetime.now()

        # Thursday = weekday 3
        days_ahead = (3 - today.weekday()) % 7

        # If today is Thursday treat it as current expiry
        if days_ahead == 0:
            expiry = today
        else:
            expiry = today + timedelta(days=days_ahead)

        # Samco format: 30JAN2025
        return expiry.strftime("%d%b%Y").upper()

    # -------------------------------------------------
    # OTM STRIKE  (1 step OTM by default)
    # -------------------------------------------------
    @staticmethod
    def get_otm_strike(spot: float, signal: str, distance: int = 1) -> int:
        step = OptionSelector.STRIKE_STEP
        atm = OptionSelector.get_atm_strike(spot)

        if signal == "CALL":
            return atm + (distance * step)
        if signal == "PUT":
            return atm - (distance * step)
        return atm

    # -------------------------------------------------
    # STRIKE RANGE  (for chain scanning)
    # -------------------------------------------------
    @staticmethod
    def get_strike_range(spot: float, steps: int = 5) -> list[int]:
        atm = OptionSelector.get_atm_strike(spot)
        step = OptionSelector.STRIKE_STEP
        return [atm + i * step for i in range(-steps, steps + 1)]
