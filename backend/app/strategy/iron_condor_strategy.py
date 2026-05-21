# backend/app/strategy/iron_condor_strategy.py
from __future__ import annotations

import math
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.app.core.config_loader import get_settings
from backend.app.core.logging_config import setup_file_logging

logger = setup_file_logging("iron_condor_strategy")
IST = ZoneInfo("Asia/Kolkata")
_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


class IronCondorStrategy:
    """
    NIFTY Iron Condor strategy.

    Uses broker quotes for real entry/exit pricing.
    Model functions are fallback/analytics only.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._env = self._load_env_file()

        self.entry_window_start = self._parse_time(
            self._cfg("IC_ENTRY_WINDOW_START", "ic_entry_window_start", "10:00"),
            default=time(10, 0),
        )
        self.entry_window_end = self._parse_time(
            self._cfg("IC_ENTRY_WINDOW_END", "ic_entry_window_end", "10:30"),
            default=time(10, 30),
        )
        self.exit_time = self._parse_time(
            self._cfg("IC_EXIT_TIME", "ic_exit_time", "14:45"),
            default=time(14, 45),
        )

        self.target_profit_pct = self._cfg_float(
            "IC_TARGET_PROFIT_PCT",
            "ic_target_profit_pct",
            0.25,
            aliases=("IC_TARGET_PROFIT",),
        )
        self.stop_loss_multiple = self._cfg_float(
            "IC_STOP_LOSS_MULTIPLE",
            "ic_stop_loss_multiple",
            1.35,
        )
        self.extreme_loss_multiple = self._cfg_float(
            "IC_EXTREME_LOSS_MULTIPLE",
            "ic_extreme_loss_multiple",
            1.80,
        )

        self.short_distance = self._cfg_int("IC_SHORT_DISTANCE", "ic_short_distance", 250)
        self.wing_width = self._cfg_int("IC_WING_WIDTH", "ic_wing_width", 100)
        self.strike_rounding = max(
            1,
            self._cfg_int("IC_STRIKE_ROUNDING", "ic_strike_rounding", 50),
        )

        self.days_to_expiry_value = max(
            1,
            self._cfg_int("IC_DAYS_TO_EXPIRY", "ic_days_to_expiry", 30),
        )
        self.min_option_premium = self._cfg_float(
            "IC_MIN_OPTION_PREMIUM",
            "ic_min_option_premium",
            0.05,
        )
        self.min_entry_premium = self._cfg_float(
            "IC_MIN_ENTRY_PREMIUM",
            "ic_min_entry_premium",
            30.0,
        )
        self.min_entry_premium_pct = self._cfg_float(
            "IC_MIN_ENTRY_PREMIUM_PCT",
            "ic_min_entry_premium_pct",
            0.0012,
        )

        self.min_reward_risk = self._cfg_float(
            "IC_MIN_REWARD_RISK",
            "ic_min_reward_risk",
            0.25,
        )
        self.min_net_after_cost_buffer = self._cfg_float(
            "IC_MIN_NET_AFTER_COST_BUFFER",
            "ic_min_net_after_cost_buffer",
            50.0,
        )
        self.min_credit_to_cost_ratio = self._cfg_float(
            "IC_MIN_CREDIT_TO_COST_RATIO",
            "ic_min_credit_to_cost_ratio",
            1.75,
        )
        self.entry_cost_buffer_pct = self._cfg_float(
            "IC_ENTRY_COST_BUFFER_PCT",
            "ic_entry_cost_buffer_pct",
            0.10,
        )

        self.one_per_day = bool(getattr(self.settings, "ic_one_per_day", True))
        self.skip_expiry_day_entry = bool(getattr(self.settings, "ic_skip_expiry_day_entry", True))
        self.skip_expiry_day_entry_use_next_week = bool(
            getattr(self.settings, "ic_skip_expiry_day_entry_use_next_week", True)
        )
        self.skip_one_day_before_expiry_after_time = self._parse_time(
            self._cfg(
                "IC_SKIP_ONE_DAY_BEFORE_EXPIRY_AFTER_TIME",
                "ic_skip_one_day_before_expiry_after_time",
                "11:15",
            ),
            default=time(11, 15),
        )

        self.expected_move_buffer = self._cfg_float(
            "IC_EXPECTED_MOVE_BUFFER",
            "ic_expected_move_buffer",
            1.10,
        )
        self.min_safety_buffer_points = self._cfg_float(
            "IC_MIN_SAFETY_BUFFER_POINTS",
            "ic_min_safety_buffer_points",
            50.0,
        )
        self.charges_buffer_multiplier = self._cfg_float(
            "IC_CHARGES_BUFFER_MULTIPLIER",
            "ic_charges_buffer_multiplier",
            1.25,
        )
        self.min_gross_profit = self._cfg_float(
            "IC_MIN_GROSS_PROFIT",
            "ic_min_gross_profit",
            250.0,
        )
        self.min_gross_target_profit = self._cfg_float(
            "IC_MIN_GROSS_TARGET_PROFIT",
            "ic_min_gross_target_profit",
            250.0,
        )
        self.min_net_target_profit = self._cfg_float(
            "IC_MIN_NET_TARGET_PROFIT",
            "ic_min_net_target_profit",
            100.0,
        )
        self.high_probability_mode = bool(
            getattr(self.settings, "ic_high_probability_mode", True)
        )
        self.require_live_iv = bool(
            getattr(self.settings, "ic_require_live_iv", False)
        )
        self.min_live_iv = self._cfg_float(
            "IC_MIN_LIVE_IV",
            "ic_min_live_iv",
            0.12,
        )
        self.max_live_iv = self._cfg_float(
            "IC_MAX_LIVE_IV",
            "ic_max_live_iv",
            0.24,
        )

        self.assumed_iv = self._cfg_float("IC_ASSUMED_IV", "ic_assumed_iv", 0.15)
        self.decay_rate = self._cfg_float("IC_DECAY_RATE", "ic_decay_rate", 0.08)
        self.min_decay_factor = self._cfg_float(
            "IC_MIN_DECAY_FACTOR",
            "ic_min_decay_factor",
            0.45,
        )
        self.max_loss_per_trade = self._cfg_float(
            "IC_MAX_LOSS_PER_TRADE",
            "ic_max_loss_per_trade",
            2500.0,
        )
        self.eod_decision_time = self._parse_time(
            self._cfg("IC_EOD_DECISION_TIME", "ic_eod_decision_time", "14:35"),
            default=time(14, 35),
        )
        self.eod_min_net_profit = self._cfg_float(
            "IC_EOD_MIN_NET_PROFIT",
            "ic_eod_min_net_profit",
            75.0,
        )

        self.proximity_exit_enabled = self._cfg_bool(
            "IC_PROXIMITY_EXIT_ENABLED",
            "ic_proximity_exit_enabled",
            True,
        )
        self.proximity_exit_ratio = self._cfg_float(
            "IC_PROXIMITY_EXIT_RATIO",
            "ic_proximity_exit_ratio",
            0.40,
        )
        self.proximity_exit_min_points = self._cfg_float(
            "IC_PROXIMITY_EXIT_MIN_POINTS",
            "ic_proximity_exit_min_points",
            75.0,
        )
        self.proximity_exit_after_time = self._parse_time(
            self._cfg(
                "IC_PROXIMITY_EXIT_AFTER",
                "ic_proximity_exit_after",
                "10:00",
            ),
            default=time(10, 0),
        )

        self.brokerage_per_order = self._cfg_float(
            "IC_BROKERAGE_PER_ORDER",
            "ic_brokerage_per_order",
            20.0,
        )
        self.entry_order_count = max(
            4,
            self._cfg_int("IC_ENTRY_ORDER_COUNT", "ic_entry_order_count", 4),
        )
        self.exit_order_count = max(
            4,
            self._cfg_int("IC_EXIT_ORDER_COUNT", "ic_exit_order_count", 4),
        )
        self.stt_sell_rate = self._cfg_float(
            "IC_STT_SELL_RATE",
            "ic_stt_sell_rate",
            0.0005,
            aliases=("IC_STT_RATE",),
        )
        self.exchange_txn_rate = self._cfg_float(
            "IC_EXCHANGE_TXN_RATE",
            "ic_exchange_txn_rate",
            0.00053,
        )
        self.sebi_rate = self._cfg_float("IC_SEBI_RATE", "ic_sebi_rate", 0.000001)
        self.gst_rate = self._cfg_float("IC_GST_RATE", "ic_gst_rate", 0.18)
        self.stamp_duty_rate = self._cfg_float(
            "IC_STAMP_DUTY_RATE",
            "ic_stamp_duty_rate",
            0.00003,
        )

        logger.info(
            (
                "IronCondorStrategy initialized | entry=%s-%s exit=%s "
                "target=%.3f sl=%.2f extreme=%.2f short_distance=%s "
                "wing=%s min_entry=%.2f proximity_exit=%s ratio=%.2f"
            ),
            self.entry_window_start,
            self.entry_window_end,
            self.exit_time,
            self.target_profit_pct,
            self.stop_loss_multiple,
            self.extreme_loss_multiple,
            self.short_distance,
            self.wing_width,
            self.min_entry_premium,
            self.proximity_exit_enabled,
            self.proximity_exit_ratio,
        )

    @staticmethod
    def _strip_value(value: Any) -> str:
        if value is None:
            return ""

        text = str(value).strip()

        if (
            (text.startswith('"') and text.endswith('"'))
            or (text.startswith("'") and text.endswith("'"))
        ):
            text = text[1:-1].strip()

        if " #" in text:
            text = text.split(" #", 1)[0].strip()

        return text

    @classmethod
    def _load_env_file(cls) -> dict[str, str]:
        if not _ENV_PATH.exists():
            return {}

        env: dict[str, str] = {}

        try:
            for raw_line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()

                if not line or line.startswith("#") or "=" not in line:
                    continue

                key, _, value = line.partition("=")
                key = key.strip()

                if key:
                    env[key] = cls._strip_value(value)
        except Exception as exc:
            logger.warning("Failed to read .env for strategy overrides: %s", exc)

        return env

    def _cfg(
        self,
        env_key: str,
        attr_name: str,
        default: Any,
        aliases: tuple[str, ...] = (),
    ) -> Any:
        if hasattr(self.settings, attr_name):
            value = getattr(self.settings, attr_name)
            if value not in ("", None):
                return value

        keys = (env_key, *aliases)

        for key in keys:
            value = os.getenv(key)
            if value not in ("", None):
                return self._strip_value(value)

        for key in keys:
            value = self._env.get(key)
            if value not in ("", None):
                return value

        return default

    def _cfg_float(
        self,
        env_key: str,
        attr_name: str,
        default: float,
        aliases: tuple[str, ...] = (),
    ) -> float:
        return self._safe_float(self._cfg(env_key, attr_name, default, aliases), default)

    def _cfg_int(
        self,
        env_key: str,
        attr_name: str,
        default: int,
        aliases: tuple[str, ...] = (),
    ) -> int:
        return self._safe_int(self._cfg(env_key, attr_name, default, aliases), default)

    def _cfg_bool(
        self,
        env_key: str,
        attr_name: str,
        default: bool,
        aliases: tuple[str, ...] = (),
    ) -> bool:
        value = self._cfg(env_key, attr_name, default, aliases)

        if isinstance(value, bool):
            return value

        text = self._strip_value(value).lower()

        if text in {"1", "true", "yes", "on", "y"}:
            return True

        if text in {"0", "false", "no", "off", "n"}:
            return False

        return default

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        try:
            return float(str(value).strip().replace(",", ""))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(float(str(value).strip().replace(",", "")))
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
            logger.error("Failed to parse time %r: %s", value, exc)
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

    @staticmethod
    def _ceil_to_step(value: float, step: int) -> int:
        return int(math.ceil(value / step) * step)

    @staticmethod
    def _floor_to_step(value: float, step: int) -> int:
        return int(math.floor(value / step) * step)

    @staticmethod
    def _round_to_step(value: float, step: int) -> int:
        return int(round(value / step) * step)

    @property
    def days_to_expiry(self) -> int:
        return self.days_to_expiry_value

    @property
    def min_premium(self) -> float:
        return self.min_entry_premium

    def dynamic_min_entry_premium(self, spot: float | None = None) -> float:
        if spot is None or spot <= 0:
            return round(self.min_entry_premium, 2)

        scaled_min = spot * self.min_entry_premium_pct
        return round(max(self.min_entry_premium, scaled_min), 2)

    def can_enter_cycle(self, current_time: datetime, state: Any) -> bool:
        current_time = self._to_ist(current_time)

        if not bool(getattr(self.settings, "iron_condor_enabled", True)):
            logger.info("IC entry blocked: IRON_CONDOR_ENABLED=false")
            return False

        if current_time.weekday() >= 5:
            logger.info("IC entry blocked: weekend=%s", current_time.strftime("%A"))
            return False

        now_time = current_time.time()

        if not (self.entry_window_start <= now_time < self.entry_window_end):
            logger.info(
                "IC entry blocked: outside window now=%s allowed=%s-%s",
                now_time,
                self.entry_window_start,
                self.entry_window_end,
            )
            return False

        if getattr(state, "active_trade", None) is not None:
            logger.info("IC entry blocked: active trade exists")
            return False

        if self.skip_expiry_day_entry and self.is_expiry_day(current_time):
            if self.skip_expiry_day_entry_use_next_week:
                next_expiry = self.resolve_entry_expiry(current_time)
                logger.info(
                    "IC entry allowed: expiry day %s using next week expiry %s",
                    current_time.date().isoformat(),
                    next_expiry.isoformat(),
                )
            else:
                logger.info(
                    "Expiry-day entry blocked for %s",
                    current_time.date().isoformat(),
                )
                return False

        try:
            from backend.app.broker.samco_client import get_weekly_expiry

            nearest_expiry = get_weekly_expiry(current_time.date())
            if (
                nearest_expiry == current_time.date() + timedelta(days=1)
                and now_time >= self.skip_one_day_before_expiry_after_time
            ):
                logger.info(
                    "IC entry blocked: one day before expiry after cutoff now=%s cutoff=%s expiry=%s",
                    now_time,
                    self.skip_one_day_before_expiry_after_time,
                    nearest_expiry.isoformat(),
                )
                return False
        except Exception as exc:
            logger.warning("Failed to evaluate pre-expiry entry cutoff: %s", exc)

        monthly_only = bool(getattr(self.settings, "ic_monthly_only", False))

        if monthly_only:
            start_day = self._cfg_int("IC_ENTRY_DAY_START", "ic_entry_day_start", 1)
            end_day = self._cfg_int("IC_ENTRY_DAY_END", "ic_entry_day_end", 5)

            if not (start_day <= current_time.day <= end_day):
                logger.info(
                    "IC entry blocked: monthly day filter day=%s allowed=%s-%s",
                    current_time.day,
                    start_day,
                    end_day,
                )
                return False

            if getattr(state, "last_iron_condor_month", None) == current_time.month:
                logger.info(
                    "IC entry blocked: already traded month=%s field=last_iron_condor_month",
                    current_time.month,
                )
                return False

            logger.info("Iron Condor monthly entry allowed")
            return True

        today_key = current_time.date().isoformat()
        if self.one_per_day:
            possible_last_dates = {
                "last_iron_condor_date": getattr(state, "last_iron_condor_date", None),
                "last_trade_date": getattr(state, "last_trade_date", None),
                "last_ic_trade_date": getattr(state, "last_ic_trade_date", None),
                "iron_condor_trade_date": getattr(state, "iron_condor_trade_date", None),
            }

            for field_name, last_value in possible_last_dates.items():
                if self._date_key(last_value) == today_key:
                    logger.info(
                        "IC entry blocked: already traded today field=%s value=%s",
                        field_name,
                        last_value,
                    )
                    return False

        logger.info("Iron Condor entry allowed")
        return True

    def _effective_iv(self, live_iv: float | None = None) -> float:
        if live_iv is not None and live_iv > 0.0:
            return float(live_iv)
        return float(self.assumed_iv)

    def is_expiry_day(self, current_time: datetime | date | None = None) -> bool:
        if current_time is None:
            current_date = datetime.now(IST).date()
        elif isinstance(current_time, datetime):
            current_date = self._to_ist(current_time).date()
        else:
            current_date = current_time

        try:
            from backend.app.broker.samco_client import get_weekly_expiry

            return get_weekly_expiry(current_date) == current_date
        except Exception as exc:
            logger.warning("Failed to evaluate expiry-day status: %s", exc)
            return False

    def _next_weekly_expiry(self, current_date: date) -> date:
        """
        Return current/nearest weekly expiry using broker expiry helper.
        If current_date is expiry day and next-week is needed,
        caller should pass current_date + 1 day.
        """
        from backend.app.broker.samco_client import get_weekly_expiry

        return get_weekly_expiry(current_date)

    def resolve_entry_expiry(self, current_time: datetime | date | None = None) -> date:
        """
        Resolve actual expiry to be used for IC leg symbols.

        Rules:
        - Normal day: nearest weekly expiry.
        - Expiry day + use_next_week=True: next weekly expiry.
        - Expiry day + use_next_week=False: current expiry, but can_enter_cycle blocks entry.
        """
        if current_time is None:
            day = datetime.now(IST).date()
        elif isinstance(current_time, datetime):
            day = self._to_ist(current_time).date()
        else:
            day = current_time

        expiry = self._next_weekly_expiry(day)

        if (
            expiry == day
            and self.skip_expiry_day_entry
            and self.skip_expiry_day_entry_use_next_week
        ):
            next_expiry = self._next_weekly_expiry(day + timedelta(days=1))
            logger.info(
                "IC expiry day: using next week expiry current=%s next=%s",
                day.isoformat(),
                next_expiry.isoformat(),
            )
            return next_expiry

        return expiry

    def evaluate_entry_regime(
        self,
        *,
        spot: float,
        live_iv: float | None = None,
    ) -> tuple[bool, str, dict[str, float | bool | None]]:
        """
        Entry regime filter for Iron Condor.

        Blocks unsafe IV conditions when high-probability mode is enabled.
        Returns:
            (allowed, reason, diagnostics)
        """
        try:
            spot_value = float(spot or 0.0)
        except (TypeError, ValueError):
            spot_value = 0.0

        iv = live_iv if live_iv is not None and live_iv > 0 else None

        diagnostics: dict[str, float | bool | None] = {
            "spot": round(spot_value, 2),
            "live_iv": round(float(iv), 4) if iv is not None else None,
            "effective_iv": round(float(self._effective_iv(live_iv)), 4),
            "high_probability_mode": bool(self.high_probability_mode),
            "require_live_iv": bool(self.require_live_iv),
            "min_live_iv": round(float(self.min_live_iv), 4),
            "max_live_iv": round(float(self.max_live_iv), 4),
        }

        if spot_value <= 0:
            return False, "invalid_spot", diagnostics

        # require_live_iv applies unconditionally regardless of high_probability_mode
        if self.require_live_iv and iv is None:
            return False, "live_iv_required", diagnostics

        # Hard ceiling: extreme IV signals tail-risk regime — never safe to sell premium
        if iv is not None and iv > 0.50:
            diagnostics["threshold"] = 0.50
            return False, "iv_extreme_tail_risk", diagnostics

        if not self.high_probability_mode:
            return True, "ok", diagnostics

        if iv is not None and iv < self.min_live_iv:
            diagnostics["threshold"] = round(float(self.min_live_iv), 4)
            return False, "iv_too_low_for_premium_selling", diagnostics

        if iv is not None and iv > self.max_live_iv:
            diagnostics["threshold"] = round(float(self.max_live_iv), 4)
            return False, "iv_too_high_for_high_probability_entry", diagnostics

        return True, "ok", diagnostics

    def calculate_target_metrics(
        self,
        entry_premium: float,
        qty: int,
    ) -> dict[str, float]:
        if entry_premium <= 0 or qty <= 0:
            return {
                "target_possible": 0.0,
                "target_close_premium": 0.0,
                "target_gross_profit": 0.0,
                "target_net_profit": 0.0,
                "required_gross_profit": float(self.min_gross_target_profit),
                "estimated_charges": 0.0,
                "charges_buffer_multiplier": float(self.charges_buffer_multiplier),
            }

        target_close_premium = round(entry_premium * (1.0 - self.target_profit_pct), 2)
        target_gross_profit = round((entry_premium - target_close_premium) * qty, 2)

        charges = self.estimate_round_trip_charges(
            entry_premium=entry_premium,
            exit_premium=target_close_premium,
            qty=qty,
        )
        total_charges = float(charges["total_charges"]) * float(self.charges_buffer_multiplier)
        target_net_profit = round(target_gross_profit - total_charges, 2)
        required_gross_profit = round(
            max(
                float(self.min_gross_target_profit),
                float(self.min_net_target_profit) + total_charges,
            ),
            2,
        )

        target_possible = 1.0
        if target_gross_profit < required_gross_profit:
            target_possible = 0.0
        if target_net_profit < float(self.min_net_target_profit):
            target_possible = 0.0

        return {
            "target_possible": float(target_possible),
            "target_close_premium": target_close_premium,
            "target_gross_profit": target_gross_profit,
            "target_net_profit": target_net_profit,
            "required_gross_profit": required_gross_profit,
            "estimated_charges": round(total_charges, 2),
            "charges_buffer_multiplier": float(self.charges_buffer_multiplier),
        }

    def is_expected_move_safe(
        self,
        *,
        spot: float,
        short_distance: float,
        live_iv: float | None = None,
        days: int = 1,
    ) -> tuple[bool, dict[str, float | bool | None]]:
        if spot <= 0 or short_distance <= 0 or days <= 0:
            return False, {
                "spot": float(spot),
                "short_distance": float(short_distance),
                "days": float(days),
                "min_safety_buffer_points": float(self.min_safety_buffer_points),
                "actual_margin": 0.0,
            }

        iv = self._effective_iv(live_iv)
        expected_move = spot * iv * math.sqrt(days / 365.0) * float(self.expected_move_buffer)
        actual_margin = short_distance - expected_move

        diagnostics = {
            "spot": round(float(spot), 2),
            "short_distance": round(float(short_distance), 2),
            "live_iv": round(float(iv), 4),
            "expected_move": round(expected_move, 2),
            "min_safety_buffer_points": float(self.min_safety_buffer_points),
            "actual_margin": round(actual_margin, 2),
        }

        if actual_margin < float(self.min_safety_buffer_points):
            return False, diagnostics

        return True, diagnostics

    def calculate_strikes(
        self,
        spot: float,
        live_iv: float | None = None,
    ) -> dict[str, int]:
        """
        Calculate Iron Condor strikes.

        Uses symmetric ATM rounding and IV-adaptive widening when live_iv
        exceeds assumed_iv (up to 30% wider strikes at elevated volatility).
        """
        if not spot or spot <= 0:
            logger.error("Invalid spot price: %s", spot)
            return {}

        rounding = self.strike_rounding
        short_distance = max(0, self.short_distance)
        wing_width = max(rounding, self.wing_width)

        if short_distance > 0 and wing_width > 0:
            atm = self._round_to_step(spot, rounding)
            effective_short_distance = short_distance
            effective_wing_width = wing_width

            # IV-adaptive widening: increase strike distances when live IV is elevated
            if live_iv is not None and live_iv > self.assumed_iv and self.assumed_iv > 0:
                iv_excess_ratio = (live_iv - self.assumed_iv) / self.assumed_iv
                widen_factor = 1.0 + min(iv_excess_ratio * 0.5, 0.30)  # cap at 30% wider
                effective_short_distance = self._round_to_step(short_distance * widen_factor, rounding)
                effective_wing_width = self._round_to_step(wing_width * widen_factor, rounding)
                logger.info(
                    "IV-adaptive strike widening live_iv=%.3f assumed_iv=%.3f factor=%.3f "
                    "short_dist %d→%d wing %d→%d",
                    live_iv, self.assumed_iv, widen_factor,
                    short_distance, effective_short_distance,
                    wing_width, effective_wing_width,
                )

            short_call = atm + effective_short_distance
            short_put = atm - effective_short_distance
            long_call = short_call + effective_wing_width
            long_put = short_put - effective_wing_width
        else:
            short_otm_pct = self._cfg_float("IC_SHORT_OTM_PCT", "ic_short_otm_pct", 0.024)
            long_otm_pct = self._cfg_float("IC_LONG_OTM_PCT", "ic_long_otm_pct", 0.036)
            short_call = self._round_to_step(spot * (1 + short_otm_pct), rounding)
            long_call = self._round_to_step(spot * (1 + long_otm_pct), rounding)
            short_put = self._round_to_step(spot * (1 - short_otm_pct), rounding)
            long_put = self._round_to_step(spot * (1 - long_otm_pct), rounding)

        if short_call >= long_call:
            logger.error("Invalid call spread: short_call=%s long_call=%s", short_call, long_call)
            return {}

        if short_put <= long_put:
            logger.error("Invalid put spread: short_put=%s long_put=%s", short_put, long_put)
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

    def get_spot_proximity_exit_reason(
        self,
        current_time: datetime,
        current_spot: float | None,
        strikes: dict[str, Any] | None,
    ) -> str | None:
        if not self.proximity_exit_enabled:
            return None

        if current_spot is None or current_spot <= 0 or not strikes:
            return None

        current_time = self._to_ist(current_time)

        if current_time.time() < self.proximity_exit_after_time:
            return None

        short_call = self._safe_float(strikes.get("short_call"), 0.0)
        short_put = self._safe_float(strikes.get("short_put"), 0.0)

        if short_call <= 0 or short_put <= 0:
            return None

        base_distance = self.short_distance
        if base_distance <= 0:
            base_distance = min(abs(short_call - current_spot), abs(current_spot - short_put))

        danger_points = max(
            self.proximity_exit_min_points,
            base_distance * self.proximity_exit_ratio,
        )
        distance_to_call = short_call - current_spot
        distance_to_put = current_spot - short_put

        # Require spot to be tick_buffer points past the short strike before calling a
        # confirmed breach — prevents a single noisy tick AT the strike from triggering.
        tick_buffer = self._cfg_float(
            "IC_BREACH_NOISE_BUFFER", "ic_breach_noise_buffer", 3.0
        )
        if distance_to_call < -tick_buffer:
            logger.warning(
                "IC proximity exit: spot breached short_call spot=%.2f short_call=%.2f",
                current_spot,
                short_call,
            )
            return "SPOT_BREACHED_SHORT_CALL"

        if distance_to_put < -tick_buffer:
            logger.warning(
                "IC proximity exit: spot breached short_put spot=%.2f short_put=%.2f",
                current_spot,
                short_put,
            )
            return "SPOT_BREACHED_SHORT_PUT"

        nearest = min(distance_to_call, distance_to_put)

        if nearest <= danger_points:
            logger.warning(
                "IC proximity exit: spot near short strike spot=%.2f nearest=%.2f danger=%.2f",
                current_spot,
                nearest,
                danger_points,
            )
            return "SPOT_PROXIMITY_EXIT"

        return None

    def estimate_dynamic_entry_credit(self, spot: float) -> float:
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

        return round(max(4.0, min(credit, spot * 0.006)), 2)

    def estimate_option_premium(
        self,
        spot: float,
        strike: float,
        opt_type: str,
        days: int = 30,
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

        sqrt_t = math.sqrt(days / 365.0)
        base_time_value = spot * self.assumed_iv * sqrt_t
        distance_discount = math.exp(-otm_pct * 18.0)
        premium = intrinsic + (base_time_value * distance_discount)

        return round(max(self.min_option_premium, premium), 2)

    def estimate_leg_premiums(self, spot: float, days: int = 30) -> dict[str, float]:
        strikes = self.calculate_strikes(spot)
        if not strikes:
            return {}

        sc_prem = self.estimate_option_premium(spot, strikes["short_call"], "CE", days)
        lc_prem = self.estimate_option_premium(spot, strikes["long_call"], "CE", days)
        sp_prem = self.estimate_option_premium(spot, strikes["short_put"], "PE", days)
        lp_prem = self.estimate_option_premium(spot, strikes["long_put"], "PE", days)

        premiums = {
            "short_call": round(sc_prem, 2),
            "long_call": round(lc_prem, 2),
            "short_put": round(sp_prem, 2),
            "long_put": round(lp_prem, 2),
        }

        if any(premium <= 0 for premium in premiums.values()):
            logger.error("Invalid leg premium estimate: %s", premiums)
            return {}

        return premiums

    def estimate_net_premium(self, spot: float, days: int = 30) -> float:
        net = self.estimate_dynamic_entry_credit(spot)

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
            hours_passed = max(minutes / 60.0, 0.0)
            decay_factor = math.exp(-self.decay_rate * hours_passed)
            decay_factor = max(self.min_decay_factor, decay_factor)
            current = entry_premium * decay_factor
            return round(max(0.1, current), 2)

        short_call = float(strikes.get("short_call", 0))
        short_put = float(strikes.get("short_put", 0))

        if short_call <= 0 or short_put <= 0:
            return round(float(entry_premium), 2)

        # Session window: from entry to scheduled exit (not fixed 375 min market day)
        exit_dt = entry_time.replace(
            hour=self.exit_time.hour,
            minute=self.exit_time.minute,
            second=0,
            microsecond=0,
        )
        session_minutes = max(1.0, (exit_dt - entry_time).total_seconds() / 60.0)
        progress = max(0.0, min(minutes / session_minutes, 1.0))
        # theta_decay_strength=0.50 allows full theta decay to ~50% at EOD
        # theta_floor=0.28 ensures model never shows <28% of entry (floor below target)
        theta_decay_strength = 0.50
        theta_floor = 0.28
        theta = max(
            entry_premium * theta_floor,
            entry_premium * (1.0 - theta_decay_strength * progress),
        )

        move_pct = abs(current_spot - entry_spot) / max(entry_spot, 1.0)
        direction_noise_floor = 0.0008
        direction = 0.0

        if move_pct > direction_noise_floor:
            adjusted_move = move_pct - direction_noise_floor
            direction = entry_premium * ((adjusted_move * 8.0) ** 1.30)

        nearest_points = min(
            abs(short_call - current_spot),
            abs(current_spot - short_put),
        )
        nearest_pct = nearest_points / max(current_spot, 1.0)
        gamma_danger_zone_pct = 0.012
        gamma_near_zone_pct = 0.006

        if nearest_pct > gamma_danger_zone_pct:
            gamma = entry_premium * 0.02
        elif nearest_pct <= gamma_near_zone_pct:
            proximity = (gamma_near_zone_pct - nearest_pct) / gamma_near_zone_pct
            gamma = entry_premium * 1.30 * (proximity**2)
        else:
            proximity = (gamma_danger_zone_pct - nearest_pct) / (
                gamma_danger_zone_pct - gamma_near_zone_pct
            )
            gamma = entry_premium * 0.70 * (proximity**2)

        if day_high and day_low and day_high > 0 and day_low > 0:
            range_pct = abs(day_high - day_low) / max(entry_spot, 1.0)
        else:
            range_pct = move_pct

        move_excess = max(0.0, move_pct - 0.0010)
        range_excess = max(0.0, range_pct - 0.0015)
        iv = entry_premium * max(0.0, min(move_excess * 7.0 + range_excess * 5.0, 0.55))

        spread_width = float(
            max(
                strikes.get("call_width", self.wing_width),
                strikes.get("put_width", self.wing_width),
            )
        )
        breach = 0.0
        if current_spot >= short_call:
            breach = entry_premium * 1.60 + (current_spot - short_call) * 0.55
        elif current_spot <= short_put:
            breach = entry_premium * 1.60 + (short_put - current_spot) * 0.55
        # Cap breach at theoretical max loss (spread_width - entry_premium)
        if breach > 0 and spread_width > entry_premium:
            breach = min(breach, spread_width - entry_premium)

        trend = 0.0
        trend_threshold_pct = 0.0040
        if move_pct > trend_threshold_pct:
            excess = move_pct - trend_threshold_pct
            trend = entry_premium * 0.35 * (1.0 + excess * 90.0)

        # Scale friction by session progress so it is 0 at entry (progress=0) and
        # reaches 3.5% of entry_premium only at end-of-day (progress=1).  Without
        # this the model shows >100% of entry immediately, causing false P&L readings.
        friction = entry_premium * 0.035 * progress
        current = theta + direction + gamma + iv + breach + trend + friction

        return round(max(0.1, current), 2)

    def get_exit_reason(
        self,
        entry_time: datetime,
        current_time: datetime,
        entry_premium: float,
        current_premium: float,
        qty: int = 0,
        current_spot: float | None = None,
        strikes: dict[str, Any] | None = None,
    ) -> str | None:
        if not entry_premium or entry_premium <= 0:
            return None

        current_time = self._to_ist(current_time)
        current_t = current_time.time()

        proximity_reason = self.get_spot_proximity_exit_reason(
            current_time=current_time,
            current_spot=current_spot,
            strikes=strikes,
        )
        if proximity_reason:
            return proximity_reason

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
            logger.info("TARGET hit current=%.2f target=%.2f", current_premium, target_premium)
            return "TARGET"

        stop_loss_premium = entry_premium * self.stop_loss_multiple
        if current_premium >= stop_loss_premium:
            logger.warning("STOP_LOSS hit current=%.2f stop=%.2f", current_premium, stop_loss_premium)
            return "STOP_LOSS"

        if current_t >= self.eod_decision_time:
            pnl = self.compute_pnl(entry_premium, current_premium, qty)
            net_pnl = float(pnl.get("net_pnl", 0.0))

            if net_pnl >= self.eod_min_net_profit:
                logger.info(
                    "EOD_PROFIT_LOCK net_pnl=%.2f threshold=%.2f current=%.2f",
                    net_pnl,
                    self.eod_min_net_profit,
                    current_premium,
                )
                return "EOD_PROFIT_LOCK"

            if current_premium < entry_premium:
                logger.info(
                    "EOD_NO_POSITIVE_TARGET net_pnl=%.2f threshold=%.2f current=%.2f entry=%.2f",
                    net_pnl,
                    self.eod_min_net_profit,
                    current_premium,
                    entry_premium,
                )
                return "EOD_NO_POSITIVE_TARGET"

            # Position is in loss during EOD window — cut if loss exceeds threshold
            eod_loss_cut = -abs(
                self._cfg_float("IC_EOD_LOSS_CUT", "ic_eod_loss_cut", self.max_loss_per_trade * 0.6)
            )
            if net_pnl <= eod_loss_cut:
                logger.warning(
                    "EOD_LOSS_CUT net_pnl=%.2f threshold=%.2f current=%.2f entry=%.2f",
                    net_pnl,
                    eod_loss_cut,
                    current_premium,
                    entry_premium,
                )
                return "EOD_LOSS_CUT"

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
        # STT applies to sell-side only (NSE options: 0.05% of premium on sell leg).
        # At entry: selling short_call + short_put → gross shorts ≈ net * qty * 1.6
        # At exit (closing): selling long_call + long_put → gross longs ≈ exit_net * qty * 0.4
        # Using net turnover for both is the best approximation without per-leg prices.
        stt = (entry_turnover + exit_turnover) * self.stt_sell_rate
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

    def is_entry_credit_viable(
        self,
        entry_premium: float,
        qty: int,
        spread_width: float | None = None,
        spot: float | None = None,
    ) -> tuple[bool, str, dict[str, float]]:
        if entry_premium <= 0 or qty <= 0:
            return False, "invalid_input", {}

        conservative_exit_premium = max(
            entry_premium,
            entry_premium * self.stop_loss_multiple,
            0.05,
        )
        charges = self.estimate_round_trip_charges(
            entry_premium=entry_premium,
            exit_premium=conservative_exit_premium,
            qty=qty,
        )

        total_charges = float(charges["total_charges"])
        gross_credit = entry_premium * qty
        net_after_costs = gross_credit - total_charges
        cost_ratio = gross_credit / total_charges if total_charges > 0 else float("inf")
        min_entry = self.dynamic_min_entry_premium(spot)

        diagnostics = {
            "gross_credit": round(gross_credit, 2),
            "estimated_round_trip_charges": round(total_charges, 2),
            "net_after_costs": round(net_after_costs, 2),
            "cost_ratio": round(cost_ratio, 3) if math.isfinite(cost_ratio) else 999999.0,
            "min_entry_premium": round(min_entry, 2),
            "conservative_exit_premium": round(conservative_exit_premium, 2),
        }

        if gross_credit < float(self.min_gross_profit):
            diagnostics["min_gross_profit"] = float(self.min_gross_profit)
            return False, "target_gross_profit_below_minimum", diagnostics

        if entry_premium < min_entry:
            diagnostics["reason_threshold"] = round(min_entry, 2)
            return False, "credit_below_min_entry_premium", diagnostics

        min_required_credit = max(
            min_entry * qty,
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
        platform_fee = float(getattr(self.settings, "ic_platform_charges", 0.0) or 0.0)
        total_charges = float(charges["total_charges"]) + platform_fee
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
            "platform_charges": round(platform_fee, 2),
            "total_charges": round(total_charges, 2),
            "net_pnl": round(net_pnl, 2),
            "risk_breached": risk_breached,
        }

    def compute_risk_metrics(
        self,
        spot: float,
        qty: int,
        days: int | None = None,
    ) -> dict[str, Any]:
        if days is None:
            try:
                expiry = self.resolve_entry_expiry()
                days = max(1, (expiry - datetime.now(IST).date()).days)
            except Exception:
                days = self.days_to_expiry_value

        strikes = self.calculate_strikes(spot)
        if not strikes:
            return {}

        net_credit = self.estimate_net_premium(spot, days)
        if net_credit <= 0:
            return {}

        spread_width = max(strikes["call_width"], strikes["put_width"])
        max_profit = net_credit * qty
        max_loss = max(0.0, (spread_width - net_credit) * qty)

        viable, viability_reason, viability = self.is_entry_credit_viable(
            entry_premium=net_credit,
            qty=qty,
            spread_width=spread_width,
            spot=spot,
        )

        return {
            "net_credit": round(net_credit, 2),
            "max_profit": round(max_profit, 2),
            "max_loss": round(max_loss, 2),
            "upper_breakeven": round(strikes["short_call"] + net_credit, 2),
            "lower_breakeven": round(strikes["short_put"] - net_credit, 2),
            "reward_risk": round(max_profit / max_loss, 3) if max_loss > 0 else 0.0,
            "strikes": strikes,
            "premiums": self.estimate_leg_premiums(spot, days),
            "entry_viable": viable,
            "entry_viability_reason": viability_reason,
            "entry_viability": viability,
        }

    @staticmethod
    def _zero_charges() -> dict[str, float]:
        return {
            "brokerage": 0.0,
            "stt": 0.0,
            "exchange_txn": 0.0,
            "sebi": 0.0,
            "gst": 0.0,
            "stamp_duty": 0.0,
            "total_charges": 0.0,
        }

    @staticmethod
    def _zero_pnl() -> dict[str, float | bool]:
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
