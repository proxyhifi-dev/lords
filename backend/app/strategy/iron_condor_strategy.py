# backend/app/strategy/iron_condor_strategy.py
from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from backend.app.core.config_loader import get_settings
from backend.app.core.logging_config import setup_file_logging

logger = setup_file_logging("iron_condor_strategy")
IST = ZoneInfo("Asia/Kolkata")


class IronCondorStrategy:
    """
    NIFTY Iron Condor premium selling strategy.

    Responsibilities:
    - entry gating
    - strike construction
    - fallback premium estimation
    - live/paper exit rules
    - gross/net pnl and charge estimation
    - minimum viable credit filtering
    """

    def __init__(self) -> None:
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

        self.target_profit_pct = self._safe_float(
            getattr(self.settings, "ic_target_profit_pct", 0.13),
            0.13,
        )
        self.stop_loss_multiple = self._safe_float(
            getattr(self.settings, "ic_stop_loss_multiple", 2.10),
            2.10,
        )
        self.extreme_loss_multiple = self._safe_float(
            getattr(self.settings, "ic_extreme_loss_multiple", 2.80),
            2.80,
        )

        self.short_distance = self._safe_int(
            getattr(self.settings, "ic_short_distance", 600),
            600,
        )
        self.wing_width = self._safe_int(
            getattr(self.settings, "ic_wing_width", 300),
            300,
        )
        self.strike_rounding = max(
            1,
            self._safe_int(getattr(self.settings, "ic_strike_rounding", 50), 50),
        )

        self.days_to_expiry_value = max(
            1,
            self._safe_int(getattr(self.settings, "ic_days_to_expiry", 7), 7),
        )

        self.min_option_premium = self._safe_float(
            getattr(self.settings, "ic_min_option_premium", 0.05),
            0.05,
        )
        self.min_entry_premium = self._safe_float(
            getattr(self.settings, "ic_min_entry_premium", 8.0),
            8.0,
        )
        self.min_reward_risk = self._safe_float(
            getattr(self.settings, "ic_min_reward_risk", 0.20),
            0.20,
        )
        self.min_net_after_cost_buffer = self._safe_float(
            getattr(self.settings, "ic_min_net_after_cost_buffer", 40.0),
            40.0,
        )
        self.min_credit_to_cost_ratio = self._safe_float(
            getattr(self.settings, "ic_min_credit_to_cost_ratio", 1.50),
            1.50,
        )
        self.entry_cost_buffer_pct = self._safe_float(
            getattr(self.settings, "ic_entry_cost_buffer_pct", 0.10),
            0.10,
        )

        self.assumed_iv = self._safe_float(
            getattr(self.settings, "ic_assumed_iv", 0.15),
            0.15,
        )
        self.decay_rate = self._safe_float(
            getattr(self.settings, "ic_decay_rate", 0.15),
            0.15,
        )
        self.min_decay_factor = self._safe_float(
            getattr(self.settings, "ic_min_decay_factor", 0.70),
            0.70,
        )

        self.max_loss_per_trade = self._safe_float(
            getattr(self.settings, "ic_max_loss_per_trade", 3000.0),
            3000.0,
        )

        self.brokerage_per_order = self._safe_float(
            getattr(self.settings, "ic_brokerage_per_order", 20.0),
            20.0,
        )
        self.entry_order_count = max(
            4,
            self._safe_int(getattr(self.settings, "ic_entry_order_count", 4), 4),
        )
        self.exit_order_count = max(
            4,
            self._safe_int(getattr(self.settings, "ic_exit_order_count", 4), 4),
        )
        self.stt_sell_rate = self._safe_float(
            getattr(self.settings, "ic_stt_sell_rate", 0.0005),
            0.0005,
        )
        self.exchange_txn_rate = self._safe_float(
            getattr(self.settings, "ic_exchange_txn_rate", 0.00053),
            0.00053,
        )
        self.sebi_rate = self._safe_float(
            getattr(self.settings, "ic_sebi_rate", 0.000001),
            0.000001,
        )
        self.gst_rate = self._safe_float(
            getattr(self.settings, "ic_gst_rate", 0.18),
            0.18,
        )
        self.stamp_duty_rate = self._safe_float(
            getattr(self.settings, "ic_stamp_duty_rate", 0.00003),
            0.00003,
        )

        logger.info(
            "IronCondorStrategy initialized | entry=%s-%s exit=%s target=%.3f sl=%.2f short_distance=%s wing=%s min_entry=%.2f",
            self.entry_window_start,
            self.entry_window_end,
            self.exit_time,
            self.target_profit_pct,
            self.stop_loss_multiple,
            self.short_distance,
            self.wing_width,
            self.min_entry_premium,
        )

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def _parse_time(self, value: str | time | None, default: time) -> time:
        if isinstance(value, time):
            return value
        try:
            text = str(value or "").strip()
            hour, minute = map(int, text.split(":")[:2])
            return time(hour, minute)
        except Exception as exc:
            logger.error("Failed to parse time '%s': %s", value, exc)
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

    def _effective_iv(self, live_iv: float | None) -> float:
        if live_iv is not None and live_iv > 0:
            return float(live_iv)
        return self.assumed_iv

    def _iv_factor(self, live_iv: float | None) -> float:
        if not self.assumed_iv or self.assumed_iv <= 0:
            return 1.0
        return self._effective_iv(live_iv) / self.assumed_iv

    def _ceil_to_step(self, value: float, step: int) -> int:
        return int(math.ceil(value / step) * step)

    def _floor_to_step(self, value: float, step: int) -> int:
        return int(math.floor(value / step) * step)

    def _round_to_step(self, value: float, step: int) -> int:
        return int(round(value / step) * step)

    def can_enter_cycle(self, current_time: datetime, state: Any) -> bool:
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
            start_day = self._safe_int(getattr(self.settings, "ic_entry_day_start", 1), 1)
            end_day = self._safe_int(getattr(self.settings, "ic_entry_day_end", 5), 5)

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

    def calculate_strikes(self, spot: float, live_iv: float | None = None) -> dict[str, int]:
        if not spot or spot <= 0:
            logger.error("Invalid spot price: %s", spot)
            return {}

        rounding = self.strike_rounding
        iv_factor = self._iv_factor(live_iv)
        scaled = int(round(self.short_distance * iv_factor))
        short_distance = max(0, (scaled // rounding) * rounding) if rounding > 0 else max(0, scaled)
        wing_width = max(rounding, self.wing_width)

        if short_distance > 0 and wing_width > 0:
            atm_up = self._ceil_to_step(spot, rounding)
            atm_down = self._floor_to_step(spot, rounding)
            short_call = atm_up + short_distance
            short_put = atm_down - short_distance
            long_call = short_call + wing_width
            long_put = short_put - wing_width
        else:
            short_otm_pct = self._safe_float(
                getattr(self.settings, "ic_short_otm_pct", 0.024),
                0.024,
            )
            long_otm_pct = self._safe_float(
                getattr(self.settings, "ic_long_otm_pct", 0.036),
                0.036,
            )
            short_call = self._round_to_step(spot * (1 + short_otm_pct), rounding)
            long_call = self._round_to_step(spot * (1 + long_otm_pct), rounding)
            short_put = self._round_to_step(spot * (1 - short_otm_pct), rounding)
            long_put = self._round_to_step(spot * (1 - long_otm_pct), rounding)

        if short_call >= long_call:
            logger.error(
                "Invalid call spread: short_call=%s long_call=%s",
                short_call,
                long_call,
            )
            return {}

        if short_put <= long_put:
            logger.error(
                "Invalid put spread: short_put=%s long_put=%s",
                short_put,
                long_put,
            )
            return {}

        call_width = long_call - short_call
        put_width = short_put - long_put

        if call_width < rounding or put_width < rounding:
            logger.error(
                "Invalid wing width: call_width=%s put_width=%s minimum=%s",
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

    def estimate_dynamic_entry_credit(
        self,
        spot: float,
        live_iv: float | None = None,
    ) -> float:
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
        credit *= self._iv_factor(live_iv)

        mode = str(getattr(self.settings, "mode", "paper")).lower()
        if mode == "conservative":
            credit *= 0.92
        elif mode == "aggressive":
            credit *= 1.06

        return round(max(4.0, min(credit, 105.0)), 2)

    def estimate_option_premium(
        self,
        spot: float,
        strike: float,
        opt_type: str,
        days: int = 30,
        live_iv: float | None = None,
    ) -> float:
        if not spot or spot <= 0:
            logger.warning("Invalid spot: %s", spot)
            return 0.0
        if not strike or strike <= 0:
            logger.warning("Invalid strike: %s", strike)
            return 0.0
        if opt_type not in {"CE", "PE"}:
            logger.warning("Invalid option type: %s", opt_type)
            return 0.0

        if not days or days <= 0:
            days = self.days_to_expiry_value

        if opt_type == "CE":
            intrinsic = max(0.0, spot - strike)
            otm_pct = max(0.0, (strike - spot) / spot)
        else:
            intrinsic = max(0.0, strike - spot)
            otm_pct = max(0.0, (spot - strike) / spot)

        iv = self._effective_iv(live_iv)
        sqrt_t = math.sqrt(days / 365.0)
        base_time_value = spot * iv * sqrt_t
        distance_discount = math.exp(-otm_pct * 18.0)
        time_value = base_time_value * distance_discount
        premium = intrinsic + time_value

        return round(max(self.min_option_premium, premium), 2)

    def estimate_leg_premiums(
        self,
        spot: float,
        days: int = 30,
        live_iv: float | None = None,
    ) -> dict[str, float]:
        strikes = self.calculate_strikes(spot)
        if not strikes:
            return {}

        net_credit = self.estimate_dynamic_entry_credit(spot, live_iv=live_iv)
        if net_credit <= 0:
            return {}

        short_call = round(net_credit * 0.58, 2)
        short_put = round(net_credit * 0.58, 2)
        long_call = round(net_credit * 0.08, 2)
        long_put = round(short_call + short_put - long_call - net_credit, 2)
        long_put = max(self.min_option_premium, long_put)

        premiums = {
            "short_call": short_call,
            "long_call": long_call,
            "short_put": short_put,
            "long_put": long_put,
        }

        if any(premium <= 0 for premium in premiums.values()):
            logger.error("Invalid synthetic premium split: %s", premiums)
            return {}

        return premiums

    def estimate_net_premium(
        self,
        spot: float,
        days: int = 30,
        live_iv: float | None = None,
    ) -> float:
        net = self.estimate_dynamic_entry_credit(spot, live_iv=live_iv)
        if net <= 0:
            logger.warning("Invalid net credit: %.2f", net)
            return 0.0
        return round(net, 2)

    def estimate_current_premium(
        self,
        entry_premium: float,
        entry_time: datetime,
        current_time: datetime,
        entry_spot: float | None = None,
        current_spot: float | None = None,
        strikes: dict[str, Any] | None = None,
        day_high: float | None = None,
        day_low: float | None = None,
    ) -> float:
        if not entry_premium or entry_premium <= 0:
            logger.warning("Invalid entry premium: %s", entry_premium)
            return float(entry_premium or 0.0)

        entry_time = self._to_ist(entry_time)
        current_time = self._to_ist(current_time)

        if current_time < entry_time:
            logger.warning("Clock skew detected")
            return round(float(entry_premium), 2)

        minutes = (current_time - entry_time).total_seconds() / 60.0

        if not entry_spot or not current_spot or not strikes:
            hours_passed = minutes / 60.0
            decay_factor = math.exp(-self.decay_rate * hours_passed)
            decay_factor = max(self.min_decay_factor, decay_factor)
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
        iv_pct = max(0.0, min(move_excess * 6.0 + range_excess * 4.0, 0.45))
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

        extreme_loss_premium = entry_premium * self.extreme_loss_multiple
        if current_premium >= extreme_loss_premium:
            logger.critical(
                "EXTREME_LOSS current=%.2f threshold=%.2f",
                current_premium,
                extreme_loss_premium,
            )
            return "EXTREME_LOSS"

        target_premium = entry_premium * (1 - self.target_profit_pct)
        if current_premium <= target_premium:
            logger.info(
                "TARGET hit current=%.2f target=%.2f",
                current_premium,
                target_premium,
            )
            return "TARGET"

        stop_loss_premium = entry_premium * self.stop_loss_multiple
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

    def estimate_round_trip_charges(
        self,
        entry_premium: float,
        exit_premium: float,
        qty: int,
    ) -> dict[str, float]:
        if entry_premium <= 0 or exit_premium < 0 or qty <= 0:
            return self._zero_charges()

        entry_turnover = entry_premium * qty
        exit_turnover = exit_premium * qty
        total_turnover = entry_turnover + exit_turnover

        brokerage = self.brokerage_per_order * (self.entry_order_count + self.exit_order_count)
        stt = exit_turnover * self.stt_sell_rate
        exchange_txn = total_turnover * self.exchange_txn_rate
        sebi = total_turnover * self.sebi_rate
        stamp_duty = entry_turnover * self.stamp_duty_rate
        gst = (brokerage + exchange_txn) * self.gst_rate

        total_charges = brokerage + stt + exchange_txn + sebi + stamp_duty + gst

        return {
            "brokerage": round(brokerage, 2),
            "stt": round(stt, 2),
            "exchange_txn": round(exchange_txn, 2),
            "sebi": round(sebi, 2),
            "stamp_duty": round(stamp_duty, 2),
            "gst": round(gst, 2),
            "total_charges": round(total_charges, 2),
        }

    def is_expected_move_safe(
        self,
        spot: float,
        short_distance: float,
        live_iv: float | None = None,
        days: int = 1,
    ) -> tuple[bool, dict[str, float]]:
        iv = self._effective_iv(live_iv)
        if iv <= 0 or short_distance <= 0:
            return True, {"expected_move": 0.0, "short_distance": float(short_distance)}

        expected_move = spot * iv * math.sqrt(max(days, 1) / 365.0)
        buffer = self._safe_float(
            getattr(self.settings, "ic_expected_move_buffer", 1.10),
            1.10,
        )
        required_distance = expected_move * buffer
        is_safe = float(short_distance) >= required_distance

        return is_safe, {
            "expected_move": round(expected_move, 2),
            "short_distance": round(float(short_distance), 2),
            "buffer": round(buffer, 3),
            "required_distance": round(required_distance, 2),
            "iv": round(iv, 4),
        }

    def is_entry_credit_viable(
        self,
        entry_premium: float,
        qty: int,
        spread_width: float | None = None,
    ) -> tuple[bool, str, dict[str, float]]:
        if entry_premium <= 0 or qty <= 0:
            return False, "invalid_input", {}

        charges = self.estimate_round_trip_charges(
            entry_premium=entry_premium,
            exit_premium=max(entry_premium * (1 - self.target_profit_pct), 0.05),
            qty=qty,
        )
        total_charges = float(charges["total_charges"])
        gross_credit = entry_premium * qty
        net_after_costs = gross_credit - total_charges
        cost_ratio = gross_credit / total_charges if total_charges > 0 else float("inf")

        diagnostics = {
            "gross_credit": round(gross_credit, 2),
            "estimated_round_trip_charges": round(total_charges, 2),
            "net_after_costs": round(net_after_costs, 2),
            "cost_ratio": round(cost_ratio, 3) if math.isfinite(cost_ratio) else 999999.0,
            "min_entry_premium": round(self.min_entry_premium, 2),
        }

        if entry_premium < self.min_entry_premium:
            diagnostics["reason_threshold"] = round(self.min_entry_premium, 2)
            return False, "credit_below_min_entry_premium", diagnostics

        min_required_credit = max(
            self.min_entry_premium * qty,
            total_charges * self.min_credit_to_cost_ratio,
            total_charges + self.min_net_after_cost_buffer,
        )
        buffered_required_credit = min_required_credit * (1 + self.entry_cost_buffer_pct)

        diagnostics["min_required_credit"] = round(min_required_credit, 2)
        diagnostics["buffered_required_credit"] = round(buffered_required_credit, 2)

        if gross_credit < buffered_required_credit:
            return False, "credit_not_worth_costs", diagnostics

        if spread_width is not None and spread_width > 0:
            max_profit = gross_credit
            max_loss = max((spread_width - entry_premium) * qty, 0.0)
            reward_risk = max_profit / max_loss if max_loss > 0 else 0.0
            diagnostics["reward_risk"] = round(reward_risk, 3)
            diagnostics["max_loss"] = round(max_loss, 2)

            if reward_risk < self.min_reward_risk:
                return False, "reward_risk_too_low", diagnostics

        return True, "ok", diagnostics

    def compute_pnl(
        self,
        entry_premium: float,
        exit_premium: float,
        qty: int,
    ) -> dict[str, float | bool]:
        if not entry_premium or entry_premium <= 0:
            logger.error("Invalid entry premium: %s", entry_premium)
            return self._zero_pnl()
        if exit_premium is None or exit_premium < 0:
            logger.error("Invalid exit premium: %s", exit_premium)
            return self._zero_pnl()
        if not qty or qty <= 0:
            logger.error("Invalid quantity: %s", qty)
            return self._zero_pnl()

        premium_profit = entry_premium - exit_premium
        gross_pnl = premium_profit * qty

        charges = self.estimate_round_trip_charges(
            entry_premium=entry_premium,
            exit_premium=exit_premium,
            qty=qty,
        )
        total_charges = float(charges["total_charges"])
        net_pnl = gross_pnl - total_charges

        risk_breached = net_pnl <= -abs(self.max_loss_per_trade)
        if risk_breached:
            logger.warning(
                "IC max loss threshold breached: net_pnl=%.2f threshold=-%.2f",
                net_pnl,
                self.max_loss_per_trade,
            )

        return {
            "premium_profit": round(premium_profit, 2),
            "gross_pnl": round(gross_pnl, 2),
            "brokerage": charges["brokerage"],
            "stt": charges["stt"],
            "exchange_txn": charges["exchange_txn"],
            "sebi": charges["sebi"],
            "gst": charges["gst"],
            "stamp_duty": charges["stamp_duty"],
            "platform_charges": charges["brokerage"],
            "total_charges": round(total_charges, 2),
            "net_pnl": round(net_pnl, 2),
            "risk_breached": risk_breached,
        }

    def _zero_charges(self) -> dict[str, float]:
        return {
            "brokerage": 0.0,
            "stt": 0.0,
            "exchange_txn": 0.0,
            "sebi": 0.0,
            "gst": 0.0,
            "stamp_duty": 0.0,
            "total_charges": 0.0,
        }

    def _zero_pnl(self) -> dict[str, float | bool]:
        return {
            "premium_profit": 0.0,
            "gross_pnl": 0.0,
            "brokerage": 0.0,
            "stt": 0.0,
            "exchange_txn": 0.0,
            "sebi": 0.0,
            "gst": 0.0,
            "stamp_duty": 0.0,
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
        live_iv: float | None = None,
    ) -> dict[str, Any]:
        strikes = self.calculate_strikes(spot)
        if not strikes:
            return {}

        net_credit = self.estimate_net_premium(spot, days, live_iv=live_iv)
        if net_credit <= 0:
            return {}

        spread_width = max(strikes["call_width"], strikes["put_width"])
        max_profit = net_credit * qty
        max_loss = max(0.0, (spread_width - net_credit) * qty)

        viable, viability_reason, viability = self.is_entry_credit_viable(
            entry_premium=net_credit,
            qty=qty,
            spread_width=spread_width,
        )

        return {
            "net_credit": round(net_credit, 2),
            "max_profit": round(max_profit, 2),
            "max_loss": round(max_loss, 2),
            "upper_breakeven": round(strikes["short_call"] + net_credit, 2),
            "lower_breakeven": round(strikes["short_put"] - net_credit, 2),
            "reward_risk": round(max_profit / max_loss, 3) if max_loss > 0 else 0.0,
            "strikes": strikes,
            "premiums": self.estimate_leg_premiums(spot, days, live_iv=live_iv),
            "entry_viable": viable,
            "entry_viability_reason": viability_reason,
            "entry_viability": viability,
        }


IronCondorStrategy.days_to_expiry = property(
    lambda self: self.days_to_expiry_value
)

IronCondorStrategy.min_premium = property(
    lambda self: self.min_entry_premium
)
