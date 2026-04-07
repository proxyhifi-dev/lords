from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal


class OptionSelector:
    """
    Utility helpers for option selection.

    Responsible only for:
    - strike selection
    - option type selection
    - expiry date calculation

    Actual trading symbol resolution is handled by SamcoClient
    via the option chain API.
    """

    STRIKE_STEP = 50

    # -------------------------------------------------
    # ATM STRIKE
    # -------------------------------------------------

    @staticmethod
    def get_atm_strike(spot: float) -> int:
        """
        Round spot price to nearest NIFTY strike.

        Example:
        22436 → 22450
        22424 → 22400
        """

        step = OptionSelector.STRIKE_STEP

        return int(round(spot / step) * step)

    # -------------------------------------------------
    # OPTION TYPE
    # -------------------------------------------------

    @staticmethod
    def get_option_type(signal: Literal["CALL", "PUT"]) -> str:
        """
        Convert strategy signal → option type.

        CALL → CE
        PUT → PE
        """

        if signal == "CALL":
            return "CE"

        if signal == "PUT":
            return "PE"

        raise ValueError(f"Invalid signal type: {signal}")

    # -------------------------------------------------
    # NEXT EXPIRY DATE
    # -------------------------------------------------

    @staticmethod
    def get_next_expiry_iso() -> str:
        """
        Returns next weekly expiry date in ISO format.

        NOTE:
        NIFTY weekly expiry typically occurs on Tuesday.
        """

        today = datetime.now().date()

        # Tuesday = 1
        expiry_weekday = 1

        days_ahead = expiry_weekday - today.weekday()

        if days_ahead <= 0:
            days_ahead += 7

        expiry = today + timedelta(days=days_ahead)

        return expiry.isoformat()

    # -------------------------------------------------
    # STRIKE RANGE GENERATOR
    # -------------------------------------------------

    @staticmethod
    def get_strike_range(spot: float, steps: int = 5) -> list[int]:
        """
        Generate strikes around ATM.

        Useful for option chain scanning.

        Example:
        spot=22430 →

        [22200,22250,22300,22350,22400,22450,22500,22550]
        """

        atm = OptionSelector.get_atm_strike(spot)

        step = OptionSelector.STRIKE_STEP

        strikes = []

        for i in range(-steps, steps + 1):
            strikes.append(atm + (i * step))

        return strikes