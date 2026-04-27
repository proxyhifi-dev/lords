# backend/app/strategy/option_selector.py

from __future__ import annotations

from backend.app.core.config_loader import get_settings

settings = get_settings()


class OptionSelector:
    """
    Utility class for selecting option strikes and metadata.
    Production-safe:
    - validates inputs
    - prevents recursion bugs
    - handles edge cases
    """

    # ─────────────────────────────
    # ATM STRIKE
    # ─────────────────────────────
    @staticmethod
    def get_atm_strike(spot: float, step: int = 50) -> int:
        if spot is None:
            raise ValueError("Spot price is None")

        if step <= 0:
            raise ValueError("Step must be > 0")

        return int(round(spot / step) * step)

    # ─────────────────────────────
    # OTM STRIKE
    # ─────────────────────────────
    @staticmethod
    def get_otm_strike(
        spot: float,
        signal: str,
        distance: int | None = None,
        step: int = 50
    ) -> int:

        if signal is None:
            raise ValueError("Signal is None")

        signal = signal.upper()

        if signal not in ("CALL", "PUT"):
            raise ValueError(f"Invalid signal: {signal}")

        if spot is None:
            raise ValueError("Spot price is None")

        if step <= 0:
            raise ValueError("Step must be > 0")

        dist = distance if distance is not None else settings.otm_distance

        if dist < 0:
            raise ValueError("Distance must be >= 0")

        atm = OptionSelector.get_atm_strike(spot, step)

        if signal == "CALL":
            return atm + dist * step
        else:
            return atm - dist * step

    # ─────────────────────────────
    # OPTION TYPE
    # ─────────────────────────────
    @staticmethod
    def get_option_type(signal: str) -> str:
        if signal is None:
            raise ValueError("Signal is None")

        signal = signal.upper()

        if signal == "CALL":
            return "CE"
        elif signal == "PUT":
            return "PE"
        else:
            raise ValueError(f"Invalid signal: {signal}")

    # ─────────────────────────────
    # EXPIRY
    # ─────────────────────────────
    @staticmethod
    def get_expiry_api() -> str:
        """
        Delegates to broker expiry logic safely
        (avoids recursion bug)
        """
        from backend.app.broker.samco_client import get_expiry_api as _get_expiry

        return _get_expiry()