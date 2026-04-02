from __future__ import annotations

from datetime import datetime


class OptionSelector:

    """
    Select ATM NIFTY option
    """

    @staticmethod
    def get_atm_strike(spot: float) -> int:

        step = 50

        return round(spot / step) * step

    @staticmethod
    def get_expiry():

        today = datetime.now()

        weekday = today.weekday()

        days_to_thursday = (3 - weekday) % 7

        expiry = today.replace(day=today.day + days_to_thursday)

        return expiry.strftime("%d%b%y").upper()

    @staticmethod
    def build_symbol(strike: int, option_type: str):

        expiry = OptionSelector.get_expiry()

        return f"NIFTY{expiry}{strike}{option_type}"

    @staticmethod
    def select(spot: float, signal: str):

        strike = OptionSelector.get_atm_strike(spot)

        option_type = "CE" if signal == "CALL" else "PE"

        symbol = OptionSelector.build_symbol(strike, option_type)

        return symbol, strike