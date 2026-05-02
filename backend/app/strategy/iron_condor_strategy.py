from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Dict

import numpy as np

from backend.app.core.config_loader import get_settings
from backend.app.utils.logger import get_logger

settings = get_settings()
logger = get_logger("iron_condor_strategy")
IST = ZoneInfo("Asia/Kolkata")


class IronCondorStrategy:
    """Monthly Iron Condor strategy helper.

    This strategy is designed for one monthly cycle:
    - Entry only on day 1-5 of month, between 09:20 and 10:00 IST
    - Exit at 50% of premium decay, 1.5x loss, 14:00 theta peak, or 15:25 EOD
    - Uses 15-delta short strikes and 5-delta protective long legs
    """

    def __init__(self):
        self.entry_window_start = self._parse_time(settings.ic_entry_window_start)
        self.entry_window_end = self._parse_time(settings.ic_entry_window_end)
        self.target_profit_pct = settings.ic_target_profit_pct
        self.stop_loss_multiple = settings.ic_stop_loss_multiple
        self.days_to_expiry = settings.ic_days_to_expiry
        self.decay_rate = settings.ic_decay_rate
        self.short_otm_pct = settings.ic_short_otm_pct
        self.long_otm_pct = settings.ic_long_otm_pct
        self.strike_rounding = settings.ic_strike_rounding
        self.platform_charges = settings.ic_platform_charges
        self.stt_rate = settings.ic_stt_rate
        self.min_premium = settings.ic_min_entry_premium

        logger.info("🚀 IronCondorStrategy helper initialized")

    @staticmethod
    def _parse_time(value: str) -> time:
        try:
            hours, minutes = map(int, value.split(":"))
            return time(hours, minutes)
        except Exception:
            return time(9, 20)

    def can_enter_cycle(self, current_dt: datetime, state) -> bool:
        if current_dt.weekday() >= 5:
            return False
        if current_dt.day < settings.ic_entry_day_start or current_dt.day > settings.ic_entry_day_end:
            return False
        if not (self.entry_window_start <= current_dt.time() < self.entry_window_end):
            return False
        if state.active_trade:
            return False
        if state.last_iron_condor_month == current_dt.month:
            return False
        return True

    def calculate_strikes(self, spot_price: float) -> Dict[str, int]:
        short_call = int(round((spot_price * (1 + self.short_otm_pct)) / self.strike_rounding) * self.strike_rounding)
        short_put = int(round((spot_price * (1 - self.short_otm_pct)) / self.strike_rounding) * self.strike_rounding)
        long_call = int(round((spot_price * (1 + self.long_otm_pct)) / self.strike_rounding) * self.strike_rounding)
        long_put = int(round((spot_price * (1 - self.long_otm_pct)) / self.strike_rounding) * self.strike_rounding)

        return {
            "short_call": short_call,
            "long_call": long_call,
            "short_put": short_put,
            "long_put": long_put,
            "call_width": long_call - short_call,
            "put_width": short_put - long_put,
        }

    def estimate_option_premium(self, spot: float, strike: int, opt_type: str, days: int) -> float:
        intrinsic = max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
        if intrinsic > 0.1:
            return intrinsic + intrinsic * 0.05

        sqrt_t = np.sqrt(days / 365)
        time_value = spot * settings.ic_assumed_iv * sqrt_t
        otm_pct = ((strike - spot) / spot) if opt_type == "CE" else ((spot - strike) / spot)
        discount = max(0.1, 1 - otm_pct * 5)
        premium = time_value * discount
        return max(5.0, premium)

    def estimate_net_premium(self, spot_price: float) -> float:
        strikes = self.calculate_strikes(spot_price)
        short_call = self.estimate_option_premium(spot_price, strikes["short_call"], "CE", self.days_to_expiry)
        long_call = self.estimate_option_premium(spot_price, strikes["long_call"], "CE", self.days_to_expiry)
        short_put = self.estimate_option_premium(spot_price, strikes["short_put"], "PE", self.days_to_expiry)
        long_put = self.estimate_option_premium(spot_price, strikes["long_put"], "PE", self.days_to_expiry)
        return (short_call + short_put) - (long_call + long_put)

    def estimate_current_premium(self, entry_premium: float, entry_time: datetime, current_time: datetime) -> float:
        hours_passed = (current_time - entry_time).total_seconds() / 3600
        decay_factor = np.exp(-self.decay_rate * hours_passed)
        return max(0.1, entry_premium * decay_factor)

    def get_exit_reason(self, current_time: datetime, entry_time: datetime, entry_premium: float, current_premium: float) -> str | None:
        target_value = entry_premium * (1 - self.target_profit_pct)
        stop_loss_value = entry_premium * self.stop_loss_multiple
        if current_premium <= target_value:
            return "TARGET"
        if current_premium >= stop_loss_value:
            return "STOP_LOSS"
        if current_time.time() >= time(14, 0):
            return "THETA_PEAK"
        if current_time.time() >= time(15, 25):
            return "EOD"
        return None

    def compute_pnl(self, entry_premium: float, exit_premium: float, qty: int) -> dict:
        premium_profit = entry_premium - exit_premium
        gross_pnl = premium_profit * qty
        short_premium = (entry_premium + exit_premium) / 2
        stt = short_premium * qty * self.stt_rate
        charges = stt + self.platform_charges
        net_pnl = gross_pnl - charges
        return {
            "premium_profit": premium_profit,
            "gross_pnl": gross_pnl,
            "stt": stt,
            "charges": charges,
            "net_pnl": net_pnl,
        }
