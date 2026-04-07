from __future__ import annotations

from datetime import datetime, timedelta


class OptionSelector:
    """
    Utility helpers for option selection.

    Actual trading symbol resolution is handled by
    SamcoClient.get_option_symbol() via the option chain API.
    """

    # -------------------------------------------------
    # ATM STRIKE
    # -------------------------------------------------

    @staticmethod
    def get_atm_strike(spot: float) -> int:
        """
        Round spot price to nearest Nifty strike.
        Default NIFTY step = 50
        """

        step = 50

        return int(round(spot / step) * step)

    # -------------------------------------------------
    # OPTION TYPE
    # -------------------------------------------------

    @staticmethod
    def get_option_type(signal: str) -> str:
        """
        Convert trading signal → option type
        CALL → CE
        PUT → PE
        """

        if signal == "CALL":
            return "CE"

        if signal == "PUT":
            return "PE"

        raise ValueError(f"Invalid signal type: {signal}")

    # -------------------------------------------------
    # NEXT THURSDAY EXPIRY
    # -------------------------------------------------

    @staticmethod
    def get_next_thursday_iso() -> str:
        """
        Returns nearest upcoming Thursday expiry
        in ISO format (YYYY-MM-DD)
        """

        today = datetime.now().date()

        weekday = today.weekday()

        # Thursday = 3
        days_ahead = 3 - weekday

        if days_ahead <= 0:
            days_ahead += 7

        expiry = today + timedelta(days=days_ahead)

        return expiry.isoformat()