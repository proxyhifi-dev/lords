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
    # DYNAMIC STRIKE
    # -------------------------------------------------

    @staticmethod
    def get_dynamic_strike(spot: float, vwap: float, signal: str) -> int:

        step = OptionSelector.STRIKE_STEP

        atm = OptionSelector.get_atm_strike(spot)

        if signal == "CALL":

            if spot > vwap:
                return atm

            return atm - step

        if signal == "PUT":

            if spot < vwap:
                return atm

            return atm + step

        return atm

    # -------------------------------------------------
    # OPTION TYPE
    # -------------------------------------------------

    @staticmethod
    def get_option_type(signal: str) -> str:

        return "CE" if signal == "CALL" else "PE"

    # -------------------------------------------------
    # NEXT THURSDAY EXPIRY (LEGACY SUPPORT)
    # -------------------------------------------------

    @staticmethod
    def get_next_thursday_iso():

        today = datetime.now()

        days_ahead = 3 - today.weekday()

        if days_ahead <= 0:
            days_ahead += 7

        expiry = today + timedelta(days=days_ahead)

        return expiry.strftime("%d-%b-%Y").upper()

    # -------------------------------------------------
    # NEXT TUESDAY EXPIRY (NIFTY CURRENT)
    # -------------------------------------------------

    @staticmethod
    def get_next_tuesday_iso():

        today = datetime.now()

        days_ahead = 1 - today.weekday()

        if days_ahead <= 0:
            days_ahead += 7

        expiry = today + timedelta(days=days_ahead)

        return expiry.strftime("%d-%b-%Y").upper()

    # -------------------------------------------------
    # SMART EXPIRY SELECTOR
    # -------------------------------------------------

    @staticmethod
    def get_expiry():

        """
        Ultra-safe expiry selector.
        Automatically selects nearest expiry.
        """

        today = datetime.now()

        # If Monday or Tuesday -> use Tuesday expiry
        if today.weekday() <= 1:
            return OptionSelector.get_next_tuesday_iso()

        # Otherwise fallback Thursday
        return OptionSelector.get_next_thursday_iso()

    # -------------------------------------------------
    # STRIKE RANGE GENERATOR
    # -------------------------------------------------

    @staticmethod
    def get_strike_range(spot: float, steps: int = 5):

        atm = OptionSelector.get_atm_strike(spot)

        step = OptionSelector.STRIKE_STEP

        strikes = []

        for i in range(-steps, steps + 1):

            strikes.append(atm + (i * step))

        return strikes

    # -------------------------------------------------
    # SMART OTM STRIKE
    # -------------------------------------------------

    @staticmethod
    def get_otm_strike(spot: float, signal: str, distance: int = 1):

        step = OptionSelector.STRIKE_STEP

        atm = OptionSelector.get_atm_strike(spot)

        if signal == "CALL":
            return atm + (distance * step)

        if signal == "PUT":
            return atm - (distance * step)

        return atm