from __future__ import annotations

import logging

from models import OptionChainSnapshot, SignalData

logger = logging.getLogger("strategy")


class StrategyEngine:
    @staticmethod
    def generate_signal(snapshot: OptionChainSnapshot) -> SignalData:
        atm_row = min(
            snapshot.strikes,
            key=lambda row: abs(row.strike_price - snapshot.atm_strike),
            default=None,
        )
        if atm_row is None:
            return SignalData(signal="NEUTRAL", reason="No strikes available")

        if atm_row.ce_oi > atm_row.pe_oi:
            signal = "CALL"
            reason = f"CE_OI {atm_row.ce_oi:.0f} > PE_OI {atm_row.pe_oi:.0f} at ATM {snapshot.atm_strike:.0f}"
        else:
            signal = "PUT"
            reason = f"PE_OI {atm_row.pe_oi:.0f} >= CE_OI {atm_row.ce_oi:.0f} at ATM {snapshot.atm_strike:.0f}"

        logger.info("strategy_signal", extra={"signal": signal, "reason": reason})
        return SignalData(signal=signal, reason=reason)
