from __future__ import annotations

from datetime import datetime, timedelta


class OptionSelector:
    """
    Select ATM NIFTY option contract
    """

    # --------------------------------
    # ATM STRIKE
    # --------------------------------

    @staticmethod
    def get_atm_strike(spot: float) -> int:
        step = 50
        return int(round(spot / step) * step)

    # --------------------------------
    # NEXT THURSDAY EXPIRY
    # --------------------------------

    @staticmethod
    def get_expiry() -> str:

        today = datetime.now()

        # Thursday = 3
        days_ahead = 3 - today.weekday()

        if days_ahead < 0:
            days_ahead += 7

        expiry_date = today + timedelta(days=days_ahead)

        return expiry_date.strftime("%d%b%y").upper()

    # --------------------------------
    # BUILD SYMBOL
    # --------------------------------

    @staticmethod
    def build_symbol(strike: int, option_type: str) -> str:

        expiry = OptionSelector.get_expiry()

        # Example
        # NIFTY04APR2422500CE

        return f"NIFTY{expiry}{strike}{option_type}"

    # --------------------------------
    # SELECT OPTION
    # --------------------------------

    @staticmethod
    def select(spot: float, signal: str):

        strike = OptionSelector.get_atm_strike(spot)

        if signal == "CALL":
            option_type = "CE"
        else:
            option_type = "PE"

        symbol = OptionSelector.build_symbol(
            strike,
            option_type
        )

        return symbol, strike