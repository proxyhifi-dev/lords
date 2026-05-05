"""
Iron Condor Strategy Implementation for Lords Bot
=================================================

Copy this file into:
backend/app/strategy/iron_condor_strategy.py

This version is aligned with your best paper-backtest config:

MODE=paper
STRATEGY_TYPE=iron_condor
IC_ENTRY_WINDOW_START=10:00
IC_ENTRY_WINDOW_END=10:05
IC_EXIT_TIME=15:00
IC_TARGET_PROFIT_PCT=0.13
IC_STOP_LOSS_MULTIPLE=2.10
IC_SHORT_DISTANCE=600
IC_WING_WIDTH=300
IC_STRIKE_ROUNDING=50
IC_SKIP_GAP_PCT=0.007
IC_SKIP_OPEN_RANGE_PCT=0.007

Important:
- This class is strategy/risk logic only.
- Actual broker execution should use real option LTPs when available.
- Synthetic premium estimation is only fallback/paper support.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from backend.app.core.config_loader import get_settings
from backend.app.core.logging_config import setup_file_logging

logger = setup_file_logging("iron_condor_strategy")
IST = ZoneInfo("Asia/Kolkata")


class IronCondorStrategy:
    """
    NIFTY Iron Condor premium selling strategy.

    Entry:
        - Weekday only
        - Configured entry time window
        - No active trade
        - Daily by default
        - Optional monthly-only mode if IC_MONTHLY_ONLY=true is added to config

    Strikes:
        - Uses distance-based strikes by default:
            short_call = ceil(spot / rounding) * rounding + short_distance
            short_put  = floor(spot / rounding) * rounding - short_distance
            long_call  = short_call + wing_width
            long_put   = short_put - wing_width

    Exit:
        - Extreme loss
        - Target profit
        - Stop loss
        - Configured time exit / EOD
    """

    def __init__(self):
        self.settings = get_settings()

        self.entry_window_start = self._parse_time(
            getattr(self.settings, "ic_entry_window_start", "10:00"),
            default=time(10, 0),
        )

        self.entry_window_end = self._parse_time(
            getattr(self.settings, "ic_entry_window_end", "10:05"),
            default=time(10, 5),
        )

        self.exit_time = self._parse_time(
            getattr(self.settings, "ic_exit_time", "15:00"),
            default=time(15, 0),
        )

        logger.info(
            "IronCondorStrategy initialized | entry=%s-%s exit=%s target=%.3f sl=%.2f short_distance=%s wing=%s",
            self.entry_window_start,
            self.entry_window_end,
            self.exit_time,
            getattr(self.settings, "ic_target_profit_pct", 0.13),
            getattr(self.settings, "ic_stop_loss_multiple", 2.10),
            getattr(self.settings, "ic_short_distance", 600),
            getattr(self.settings, "ic_wing_width", 300),
        )

    def _parse_time(self, value: str | time | None, default: time) -> time:
        if isinstance(value, time):
            return value

        try:
            text = str(value or "").strip()
            h, m = map(int, text.split(":")[:2])
            return time(h, m)
        except Exception as exc:
            logger.error("[ERROR] Failed to parse time '%s': %s", value, exc)
            return default

    def _to_ist(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=IST)
        return value.astimezone(IST)

    def _date_key(self, value: datetime | date | str | None) -> str | None:
        if value is None:
            return None

        if isinstance(value, datetime):
            return value.date().isoformat()

        if isinstance(value, date):
            return value.isoformat()

        text = str(value).strip()
        if not text:
            return None

        return text[:10]

    def _ceil_to_step(self, value: float, step: int) -> int:
        return int(math.ceil(value / step) * step)

    def _floor_to_step(self, value: float, step: int) -> int:
        return int(math.floor(value / step) * step)

    def _round_to_step(self, value: float, step: int) -> int:
        return int(round(value / step) * step)

    def can_enter_cycle(self, current_time: datetime, state) -> bool:
        """
        Decide whether a new Iron Condor cycle can be entered.

        Default behavior:
            daily paper trading candidate

        Optional monthly behavior:
            Add IC_MONTHLY_ONLY=true support in config_loader/settings if needed.
            If settings.ic_monthly_only exists and is True, this blocks after one
            IC trade per month and obeys ic_entry_day_start/end.
        """

        current_time = self._to_ist(current_time)

        if not getattr(self.settings, "iron_condor_enabled", True):
            logger.debug("Iron Condor disabled by config")
            return False

        if current_time.weekday() >= 5:
            logger.debug("Weekend blocked: %s", current_time.strftime("%A"))
            return False

        now_time = current_time.time()

        if not (self.entry_window_start <= now_time < self.entry_window_end):
            logger.debug(
                "Entry time blocked: now=%s allowed=%s-%s",
                now_time,
                self.entry_window_start,
                self.entry_window_end,
            )
            return False

        if getattr(state, "active_trade", None) is not None:
            logger.debug("Active trade exists; entry blocked")
            return False

        monthly_only = bool(getattr(self.settings, "ic_monthly_only", False))

        if monthly_only:
            start_day = getattr(self.settings, "ic_entry_day_start", 1)
            end_day = getattr(self.settings, "ic_entry_day_end", 5)

            if not (start_day <= current_time.day <= end_day):
                logger.debug(
                    "Monthly entry day blocked: day=%s allowed=%s-%s",
                    current_time.day,
                    start_day,
                    end_day,
                )
                return False

            if getattr(state, "last_iron_condor_month", None) == current_time.month:
                logger.debug("Already traded this month; entry blocked")
                return False

        else:
            today_key = current_time.date().isoformat()

            possible_last_dates = [
                getattr(state, "last_iron_condor_date", None),
                getattr(state, "last_trade_date", None),
                getattr(state, "last_ic_trade_date", None),
                getattr(state, "iron_condor_trade_date", None),
            ]

            for last_value in possible_last_dates:
                if self._date_key(last_value) == today_key:
                    logger.debug("Already traded today; entry blocked")
                    return False

        logger.info("Iron Condor entry allowed")
        return True

    def calculate_strikes(self, spot: float) -> dict:
        """
        Calculate distance-based Iron Condor strikes.

        Preferred config:
            IC_SHORT_DISTANCE=600
            IC_WING_WIDTH=300
            IC_STRIKE_ROUNDING=50

        Fallback:
            If distance config is missing/invalid, falls back to OTM percentage.
        """

        if not spot or spot <= 0:
            logger.error("[ERROR] Invalid spot price: %s", spot)
            return {}

        rounding = int(getattr(self.settings, "ic_strike_rounding", 50) or 50)
        short_distance = int(getattr(self.settings, "ic_short_distance", 0) or 0)
        wing_width = int(getattr(self.settings, "ic_wing_width", 0) or 0)

        if short_distance > 0 and wing_width > 0:
            atm_up = self._ceil_to_step(spot, rounding)
            atm_down = self._floor_to_step(spot, rounding)

            short_call = atm_up + short_distance
            short_put = atm_down - short_distance
            long_call = short_call + wing_width
            long_put = short_put - wing_width

        else:
            short_otm_pct = float(getattr(self.settings, "ic_short_otm_pct", 0.024))
            long_otm_pct = float(getattr(self.settings, "ic_long_otm_pct", 0.036))

            short_call = self._round_to_step(spot * (1 + short_otm_pct), rounding)
            long_call = self._round_to_step(spot * (1 + long_otm_pct), rounding)
            short_put = self._round_to_step(spot * (1 - short_otm_pct), rounding)
            long_put = self._round_to_step(spot * (1 - long_otm_pct), rounding)

        if short_call >= long_call:
            logger.error(
                "[ERROR] Invalid call spread: short_call=%s long_call=%s",
                short_call,
                long_call,
            )
            return {}

        if short_put <= long_put:
            logger.error(
                "[ERROR] Invalid put spread: short_put=%s long_put=%s",
                short_put,
                long_put,
            )
            return {}

        call_width = long_call - short_call
        put_width = short_put - long_put

        if call_width < rounding or put_width < rounding:
            logger.error(
                "[ERROR] Invalid wing width: call_width=%s put_width=%s minimum=%s",
                call_width,
                put_width,
                rounding,
            )
            return {}

        strikes = {
            "short_call": int(short_call),
            "long_call": int(long_call),
            "short_put": int(short_put),
            "long_put": int(long_put),
            "call_width": int(call_width),
            "put_width": int(put_width),
        }

        logger.info("IC strikes calculated for spot=%.2f -> %s", spot, strikes)
        return strikes

    def estimate_dynamic_entry_credit(self, spot: float) -> float:
        """
        Calibrated synthetic IC credit.

        This is aligned with your backtest range:
            Around NIFTY 25k, IC credit roughly 90-105.

        Real live/paper execution should prefer actual option LTPs from broker.
        """

        if not spot or spot <= 0:
            return 0.0

        strikes = self.calculate_strikes(spot)
        if not strikes:
            return 0.0

        short_call = strikes["short_call"]
        short_put = strikes["short_put"]
        wing_width = max(strikes["call_width"], strikes["put_width"])

        upper_distance = abs(short_call - spot)
        lower_distance = abs(spot - short_put)
        avg_distance = max((upper_distance + lower_distance) / 2.0, 1.0)

        distance_pct = avg_distance / spot

        base_credit = spot * 0.00225
        distance_adjustment = math.exp(-distance_pct * 9.0)
        width_adjustment = max(0.80, min(wing_width / 300.0, 1.25))

        credit = base_credit * (0.85 + distance_adjustment) * width_adjustment

        mode = str(getattr(self.settings, "mode", "paper")).lower()
        if mode == "conservative":
            credit *= 0.92
        elif mode == "aggressive":
            credit *= 1.06

        return round(max(45.0, min(credit, 105.0)), 2)

    def estimate_option_premium(
        self,
        spot: float,
        strike: float,
        opt_type: str,
        days: int = 30,
    ) -> float:
        """
        Fallback synthetic single-leg premium.

        This is not meant to replace real option LTPs.
        It only exists for paper/simulation support.
        """

        if not spot or spot <= 0:
            logger.warning("[WARN] Invalid spot: %s", spot)
            return 0.0

        if not strike or strike <= 0:
            logger.warning("[WARN] Invalid strike: %s", strike)
            return 0.0

        if opt_type not in {"CE", "PE"}:
            logger.warning("[WARN] Invalid option type: %s", opt_type)
            return 0.0

        if not days or days <= 0:
            days = max(1, int(getattr(self.settings, "ic_days_to_expiry", 7) or 7))

        if opt_type == "CE":
            intrinsic = max(0.0, spot - strike)
            otm_pct = max(0.0, (strike - spot) / spot)
        else:
            intrinsic = max(0.0, strike - spot)
            otm_pct = max(0.0, (spot - strike) / spot)

        assumed_iv = float(getattr(self.settings, "ic_assumed_iv", 0.15))
        sqrt_t = math.sqrt(days / 365.0)

        base_time_value = spot * assumed_iv * sqrt_t
        distance_discount = math.exp(-otm_pct * 18.0)
        time_value = base_time_value * distance_discount

        premium = intrinsic + time_value

        min_option_premium = float(getattr(self.settings, "ic_min_option_premium", 5.0))
        return round(max(min_option_premium, premium), 2)

    def estimate_leg_premiums(self, spot: float, days: int = 30) -> dict:
        """
        Create synthetic leg premium breakdown where net credit matches
        estimate_dynamic_entry_credit().

        This avoids the old issue where percentage/IV model produced unrealistic
        credits for the distance-based strategy.
        """

        strikes = self.calculate_strikes(spot)
        if not strikes:
            return {}

        net_credit = self.estimate_dynamic_entry_credit(spot)
        if net_credit <= 0:
            return {}

        short_call = round(net_credit * 0.58, 2)
        short_put = round(net_credit * 0.58, 2)
        long_call = round(net_credit * 0.08, 2)
        long_put = round(short_call + short_put - long_call - net_credit, 2)

        long_put = max(0.05, long_put)

        premiums = {
            "short_call": short_call,
            "long_call": long_call,
            "short_put": short_put,
            "long_put": long_put,
        }

        for leg, premium in premiums.items():
            if premium <= 0:
                logger.error("[ERROR] Invalid premium: %s=%s", leg, premium)
                return {}

        return premiums

    def estimate_net_premium(self, spot: float, days: int = 30) -> float:
        """
        Estimated net credit.

        Real execution should use:
            short_call_ltp + short_put_ltp - long_call_ltp - long_put_ltp
        """

        net = self.estimate_dynamic_entry_credit(spot)

        if net <= 0:
            logger.warning("[WARN] Invalid net credit: %.2f", net)
            return 0.0

        min_premium = float(getattr(self.settings, "ic_min_entry_premium", 80.0))
        if net < min_premium:
            logger.warning(
                "[WARN] Net premium too low: %.2f < %.2f",
                net,
                min_premium,
            )
            return 0.0

        logger.info("Estimated IC net premium: %.2f", net)
        return round(net, 2)

    def estimate_current_premium(
        self,
        entry_premium: float,
        entry_time: datetime,
        current_time: datetime,
        entry_spot: float | None = None,
        current_spot: float | None = None,
        strikes: dict | None = None,
        day_high: float | None = None,
        day_low: float | None = None,
    ) -> float:
        """
        Estimate current debit to close the IC.

        Backward-compatible:
            estimate_current_premium(entry_premium, entry_time, current_time)

        Better paper usage:
            estimate_current_premium(
                entry_premium, entry_time, current_time,
                entry_spot=..., current_spot=..., strikes=...
            )

        Live usage:
            Prefer real current net debit from actual option legs.
        """

        if not entry_premium or entry_premium <= 0:
            logger.warning("[WARN] Invalid entry premium: %s", entry_premium)
            return float(entry_premium or 0.0)

        entry_time = self._to_ist(entry_time)
        current_time = self._to_ist(current_time)

        if current_time < entry_time:
            logger.warning("[WARN] Clock skew detected")
            return round(float(entry_premium), 2)

        minutes = (current_time - entry_time).total_seconds() / 60.0

        if not entry_spot or not current_spot or not strikes:
            hours_passed = minutes / 60.0
            decay_rate = float(getattr(self.settings, "ic_decay_rate", 0.15))
            decay_factor = math.exp(-decay_rate * hours_passed)
            min_decay_factor = float(getattr(self.settings, "ic_min_decay_factor", 0.70))
            decay_factor = max(min_decay_factor, decay_factor)

            current = entry_premium * decay_factor
            return round(max(0.1, current), 2)

        short_call = float(strikes.get("short_call", 0))
        short_put = float(strikes.get("short_put", 0))

        if short_call <= 0 or short_put <= 0:
            return round(float(entry_premium), 2)

        session_minutes = 375.0

        theta_decay_strength = 0.34
        min_theta_floor_pct = 0.52
        progress = max(0.0, min(minutes / session_minutes, 1.0))
        theta = max(
            entry_premium * min_theta_floor_pct,
            entry_premium * (1.0 - theta_decay_strength * progress),
        )

        move_pct = abs(current_spot - entry_spot) / max(entry_spot, 1.0)
        direction_noise_floor = 0.0008
        if move_pct <= direction_noise_floor:
            direction = 0.0
        else:
            adjusted_move = move_pct - direction_noise_floor
            direction = entry_premium * ((adjusted_move * 6.5) ** 1.35)

        nearest_pct = min(
            abs(short_call - current_spot),
            abs(current_spot - short_put),
        ) / max(current_spot, 1.0)

        gamma_danger_zone_pct = 0.018
        gamma_near_zone_pct = 0.009

        if nearest_pct > gamma_danger_zone_pct:
            gamma = entry_premium * 0.015
        elif nearest_pct <= gamma_near_zone_pct:
            proximity = (gamma_near_zone_pct - nearest_pct) / gamma_near_zone_pct
            gamma = entry_premium * 1.05 * (proximity ** 2)
        else:
            proximity = (gamma_danger_zone_pct - nearest_pct) / (
                gamma_danger_zone_pct - gamma_near_zone_pct
            )
            gamma = entry_premium * 0.55 * (proximity ** 2)

        if day_high and day_low and day_high > 0 and day_low > 0:
            range_pct = abs(day_high - day_low) / max(entry_spot, 1.0)
        else:
            range_pct = move_pct

        move_excess = max(0.0, move_pct - 0.0010)
        range_excess = max(0.0, range_pct - 0.0015)
        iv_pct = move_excess * 6.0 + range_excess * 4.0
        iv_pct = max(0.0, min(iv_pct, 0.45))
        iv = entry_premium * iv_pct

        breach = 0.0
        if current_spot >= short_call:
            breach = entry_premium * 1.45 + (current_spot - short_call) * 0.45
        elif current_spot <= short_put:
            breach = entry_premium * 1.45 + (short_put - current_spot) * 0.45

        trend = 0.0
        trend_threshold_pct = 0.0045
        if move_pct > trend_threshold_pct:
            excess = move_pct - trend_threshold_pct
            trend = entry_premium * 0.28 * (1.0 + excess * 80.0)

        friction = entry_premium * (0.020 + 0.012)

        current = theta + direction + gamma + iv + breach + trend + friction
        return round(max(0.1, current), 2)

    def get_exit_reason(
        self,
        entry_time: datetime,
        current_time: datetime,
        entry_premium: float,
        current_premium: float,
    ) -> str | None:
        if not entry_premium or entry_premium <= 0:
            return None

        current_time = self._to_ist(current_time)
        current_t = current_time.time()

        extreme_loss_multiple = float(
            getattr(self.settings, "ic_extreme_loss_multiple", 2.80)
        )
        extreme_loss_premium = entry_premium * extreme_loss_multiple

        if current_premium >= extreme_loss_premium:
            logger.critical(
                "[CRITICAL] EXTREME_LOSS current=%.2f threshold=%.2f",
                current_premium,
                extreme_loss_premium,
            )
            return "EXTREME_LOSS"

        target_profit_pct = float(
            getattr(
                self.settings,
                "ic_target_profit",
                getattr(self.settings, "ic_target_profit_pct", 0.13),
            )
        )
        target_premium = entry_premium * (1 - target_profit_pct)

        if current_premium <= target_premium:
            logger.info(
                "TARGET hit current=%.2f target=%.2f",
                current_premium,
                target_premium,
            )
            return "TARGET"

        stop_loss_multiple = float(
            getattr(self.settings, "ic_stop_loss_multiple", 2.10)
        )
        stop_loss_premium = entry_premium * stop_loss_multiple

        if current_premium >= stop_loss_premium:
            logger.warning(
                "STOP_LOSS hit current=%.2f stop=%.2f",
                current_premium,
                stop_loss_premium,
            )
            return "STOP_LOSS"

        if current_t >= self.exit_time:
            logger.info("EOD exit current_time=%s exit_time=%s", current_t, self.exit_time)
            return "EOD"

        return None

    def compute_pnl(
        self,
        entry_premium: float,
        exit_premium: float,
        qty: int,
    ) -> dict:
        if not entry_premium or entry_premium <= 0:
            logger.error("[ERROR] Invalid entry premium: %s", entry_premium)
            return self._zero_pnl()

        if exit_premium is None or exit_premium < 0:
            logger.error("[ERROR] Invalid exit premium: %s", exit_premium)
            return self._zero_pnl()

        if not qty or qty <= 0:
            logger.error("[ERROR] Invalid quantity: %s", qty)
            return self._zero_pnl()

        premium_profit = entry_premium - exit_premium
        gross_pnl = premium_profit * qty

        stt_rate = float(getattr(self.settings, "ic_stt_rate", 0.0015))
        platform_charges = float(getattr(self.settings, "ic_platform_charges", 100.0))

        stt = entry_premium * qty * stt_rate
        exchange_txn = (entry_premium + exit_premium) * qty * 0.00053
        sebi = (entry_premium + exit_premium) * qty * 0.000001
        gst = (platform_charges + exchange_txn) * 0.18

        total_charges = platform_charges + stt + exchange_txn + sebi + gst
        net_pnl = gross_pnl - total_charges

        max_loss_per_trade = float(
            getattr(self.settings, "ic_max_loss_per_trade", 3000.0)
        )

        risk_breached = net_pnl <= -abs(max_loss_per_trade)

        if risk_breached:
            logger.warning(
                "IC max loss threshold breached: net_pnl=%.2f threshold=-%.2f",
                net_pnl,
                max_loss_per_trade,
            )

        return {
            "premium_profit": round(premium_profit, 2),
            "gross_pnl": round(gross_pnl, 2),
            "stt": round(stt, 2),
            "exchange_txn": round(exchange_txn, 2),
            "sebi": round(sebi, 2),
            "gst": round(gst, 2),
            "platform_charges": round(platform_charges, 2),
            "total_charges": round(total_charges, 2),
            "net_pnl": round(net_pnl, 2),
            "risk_breached": risk_breached,
        }

    def _zero_pnl(self) -> dict:
        return {
            "premium_profit": 0.0,
            "gross_pnl": 0.0,
            "stt": 0.0,
            "exchange_txn": 0.0,
            "sebi": 0.0,
            "gst": 0.0,
            "platform_charges": 0.0,
            "total_charges": 0.0,
            "net_pnl": 0.0,
            "risk_breached": False,
        }

    def compute_risk_metrics(
        self,
        spot: float,
        qty: int,
        days: int = 30,
    ) -> dict:
        strikes = self.calculate_strikes(spot)

        if not strikes:
            return {}

        net_credit = self.estimate_net_premium(spot, days)

        if net_credit <= 0:
            return {}

        spread_width = max(strikes["call_width"], strikes["put_width"])
        max_profit = net_credit * qty
        max_loss = max(0.0, (spread_width - net_credit) * qty)

        return {
            "net_credit": round(net_credit, 2),
            "max_profit": round(max_profit, 2),
            "max_loss": round(max_loss, 2),
            "upper_breakeven": round(strikes["short_call"] + net_credit, 2),
            "lower_breakeven": round(strikes["short_put"] - net_credit, 2),
            "reward_risk": round(max_profit / max_loss, 3) if max_loss > 0 else 0,
            "strikes": strikes,
            "premiums": self.estimate_leg_premiums(spot, days),
        }


IronCondorStrategy.days_to_expiry = property(
    lambda self: getattr(self.settings, "ic_days_to_expiry", 30)
)

IronCondorStrategy.min_premium = property(
    lambda self: getattr(self.settings, "ic_min_entry_premium", 80.0)
)