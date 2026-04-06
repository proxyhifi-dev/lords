from __future__ import annotations

from datetime import datetime, timedelta


class OptionSelector:
    """
    Utility helpers for option selection.
    Actual trading symbol resolution is handled by SamcoClient.get_option_symbol()
    via the option chain API — NOT by constructing symbol strings here.
    """

    @staticmethod
    def get_atm_strike(spot: float) -> int:
        """Round spot to nearest 50-point Nifty strike."""
        step = 50
        return int(round(spot / step) * step)

    @staticmethod
    def get_option_type(signal: str) -> str:
        """CALL → CE, PUT → PE"""
        return "CE" if signal == "CALL" else "PE"

    @staticmethod
    def get_next_thursday_iso() -> str:
        """Returns nearest upcoming Thursday as ISO string (YYYY-MM-DD)."""
        today = datetime.now().date()
        days_ahead = (3 - today.weekday()) % 7
        expiry_date = today + timedelta(days=days_ahead)
        return expiry_date.isoformat()
