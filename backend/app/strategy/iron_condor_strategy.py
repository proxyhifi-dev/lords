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
        self.wing_width = self._cfg_int("IC_WING_WIDTH", "ic_wing_width", 150)
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
            0.00035,
        )
        self.sebi_rate = self._cfg_float("IC_SEBI_RATE", "ic_sebi_rate", 0.000001)
        self.gst_rate = self._cfg_float("IC_GST_RATE", "ic_gst_rate", 0.18)
        self.stamp_duty_rate = self._cfg_float(
            "IC_STAMP_DUTY_RATE",
            "ic_stamp_duty_rate",
            0.00003,
        )

        # ── Quantitative / advanced strategy ────────────────────────────────
        self.target_short_delta = self._cfg_float(
            "IC_TARGET_SHORT_DELTA", "ic_target_short_delta", 0.16,
        )  # 16-delta ≈ 80% PoP; better premium/charge ratio than 10-delta
        self.min_entry_score = self._cfg_float(
            "IC_MIN_ENTRY_SCORE", "ic_min_entry_score", 60.0,
        )  # gate: don't enter if composite score < this
        self.partial_exit_enabled = self._cfg_bool(
            "IC_PARTIAL_EXIT_ENABLED", "ic_partial_exit_enabled", True,
        )
        self.partial_exit_25_qty_pct = self._cfg_float(
            "IC_PARTIAL_EXIT_25_QTY_PCT", "ic_partial_exit_25_qty_pct", 0.25,
        )
        self.partial_exit_50_qty_pct = self._cfg_float(
            "IC_PARTIAL_EXIT_50_QTY_PCT", "ic_partial_exit_50_qty_pct", 0.50,
        )
        self.roll_delta_threshold = self._cfg_float(
            "IC_ROLL_DELTA_THRESHOLD", "ic_roll_delta_threshold", 0.30,
        )
        self.min_iv_rank_entry = self._cfg_float(
            "IC_MIN_IV_RANK_ENTRY", "ic_min_iv_rank_entry", 25.0,
        )
        self.max_iv_rank_entry = self._cfg_float(
            "IC_MAX_IV_RANK_ENTRY", "ic_max_iv_rank_entry", 75.0,
        )

        # ── Cached runtime-hot config values ────────────────────────────────
        # Loaded once here so they are not re-parsed on every tick.
        self.ic_breach_noise_buffer = self._cfg_float(
            "IC_BREACH_NOISE_BUFFER", "ic_breach_noise_buffer", 3.0,
        )
        _eod_loss_cut_raw = self._cfg_float(
            "IC_EOD_LOSS_CUT", "ic_eod_loss_cut", 0.0,
        )
        # 0 means "use default" = 60% of max_loss_per_trade
        self.ic_eod_loss_cut = (
            _eod_loss_cut_raw if _eod_loss_cut_raw > 0 else self.max_loss_per_trade * 0.6
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

    # ── Black-Scholes-Merton engine ──────────────────────────────────────────

    @staticmethod
    def _norm_cdf(x: float) -> float:
        """Standard normal CDF — Abramowitz & Stegun approximation (error < 7.5e-8)."""
        if x >= 8.0:
            return 1.0
        if x <= -8.0:
            return 0.0
        t = 1.0 / (1.0 + 0.2316419 * abs(x))
        d = 0.3989422820 * math.exp(-0.5 * x * x)
        p = d * t * (
            0.3193815
            + t * (-0.3565638 + t * (1.7814779 + t * (-1.8212560 + t * 1.3302744)))
        )
        return 1.0 - p if x > 0.0 else p

    @staticmethod
    def _norm_pdf(x: float) -> float:
        """Standard normal PDF."""
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

    def _bsm(
        self,
        spot: float,
        strike: float,
        iv: float,
        dte_days: float,
        opt_type: str,
    ) -> dict[str, float]:
        """
        Full Black-Scholes-Merton calculation for NIFTY index options.
        Risk-free rate = 0 (standard for index options in India).
        Returns: price, delta, gamma, theta (daily ₹), vega (per 1% IV move).
        """
        if spot <= 0 or strike <= 0 or iv <= 0:
            intrinsic = max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
            delta = (1.0 if spot > strike else 0.0) if opt_type == "CE" else (
                -1.0 if spot < strike else 0.0
            )
            return {"price": intrinsic, "delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

        t = max(dte_days / 365.0, 1e-6)
        sqrt_t = math.sqrt(t)

        try:
            d1 = (math.log(spot / strike) + 0.5 * iv * iv * t) / (iv * sqrt_t)
        except (ValueError, ZeroDivisionError):
            intrinsic = max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
            return {"price": intrinsic, "delta": 0.5, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

        d2 = d1 - iv * sqrt_t
        nd1 = self._norm_cdf(d1)
        nd2 = self._norm_cdf(d2)
        pdf_d1 = self._norm_pdf(d1)

        if opt_type == "CE":
            price = spot * nd1 - strike * nd2
            delta = nd1
        else:
            price = strike * self._norm_cdf(-d2) - spot * self._norm_cdf(-d1)
            delta = nd1 - 1.0

        gamma = pdf_d1 / (spot * iv * sqrt_t) if (spot * iv * sqrt_t) > 0 else 0.0
        vega = spot * pdf_d1 * sqrt_t / 100.0          # ₹ per 1% IV
        theta_daily = -(spot * pdf_d1 * iv) / (2.0 * sqrt_t) / 365.0  # daily ₹

        return {
            "price": max(0.0, price),
            "delta": delta,
            "gamma": gamma,
            "theta": theta_daily,
            "vega": vega,
        }

    def implied_vol(
        self,
        market_price: float,
        spot: float,
        strike: float,
        dte_days: float,
        opt_type: str,
        tol: float = 1e-5,
        max_iter: int = 60,
    ) -> float | None:
        """
        Newton-Raphson implied volatility solver.

        Uses the BSM vega as the derivative.  The vega returned by _bsm() is
        "₹ per 1 vol-point (1% IV change)", so the derivative with respect to
        sigma in decimal form is vega_code × 100.

        Returns None when the solver fails to converge (e.g. deep ITM/OTM with
        no time value) so callers can fall back to assumed_iv gracefully.
        """
        if market_price <= 0 or spot <= 0 or strike <= 0 or dte_days <= 0:
            return None

        intrinsic = (
            max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
        )
        if market_price < intrinsic:
            return None  # below intrinsic — no real solution

        sigma = 0.20  # starting guess: 20% IV
        for _ in range(max_iter):
            res = self._bsm(spot, strike, sigma, dte_days, opt_type)
            diff = res["price"] - market_price
            if abs(diff) < tol:
                return round(max(0.01, min(sigma, 2.0)), 4)
            # vega_code = ΔP per 1 vol-point; ∂P/∂sigma_decimal = vega_code * 100
            dv = res["vega"] * 100.0
            if dv < 1e-8:
                break
            sigma -= diff / dv
            sigma = max(0.005, min(sigma, 2.0))

        return None

    def bsm_price(
        self, spot: float, strike: float, iv: float, dte_days: float, opt_type: str
    ) -> float:
        return self._bsm(spot, strike, iv, dte_days, opt_type)["price"]

    def bsm_delta(
        self, spot: float, strike: float, iv: float, dte_days: float, opt_type: str
    ) -> float:
        return self._bsm(spot, strike, iv, dte_days, opt_type)["delta"]

    def _strike_for_delta(
        self,
        spot: float,
        target_delta: float,
        iv: float,
        dte_days: float,
        opt_type: str,
    ) -> int:
        """
        Binary search for the strike that achieves target_delta.
        CE: target_delta > 0 (e.g. 0.10 for 10-delta OTM call).
        PE: target_delta < 0 (e.g. -0.10 for 10-delta OTM put).
        Delta is monotonic in strike so binary search always converges.
        """
        lo = int(spot * 0.55)
        hi = int(spot * 1.45)

        for _ in range(64):
            if lo > hi:
                break
            mid = (lo + hi) // 2
            delta = self._bsm(spot, mid, iv, dte_days, opt_type)["delta"]
            # Unified: both CE and PE — delta increases as strike decreases
            if delta > target_delta:
                lo = mid + 1
            else:
                hi = mid - 1

        return self._round_to_step((lo + hi) / 2.0, self.strike_rounding)

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
            logger.warning(
                "Failed to evaluate pre-expiry entry cutoff — blocking entry fail-closed: %s", exc
            )
            return False

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
        iv_rank: float | None = None,
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

        # IV Rank gate: only sell premium when IV is in the upper half of its
        # recent range (rank >= 0.50). Below this the credit is historically thin
        # and reward/risk is poor for short-premium strategies.
        if iv_rank is not None:
            min_iv_rank = float(getattr(self.settings, "ic_min_iv_rank", 0.50))
            diagnostics["iv_rank"] = round(iv_rank, 3)
            diagnostics["min_iv_rank"] = round(min_iv_rank, 3)
            if iv_rank < min_iv_rank:
                return False, "iv_rank_too_low_for_premium_selling", diagnostics

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

    def calculate_strikes_by_delta(
        self,
        spot: float,
        iv: float,
        dte_days: float,
        target_delta: float | None = None,
    ) -> dict[str, int]:
        """
        Delta-targeted IC strike selection — the professional standard.

        Places short strikes at target_delta (default 10-delta = ~85% PoP).
        At NIFTY 24000, 7 DTE, IV=15%: 10-delta short strikes land ~600 pts OTM
        vs the fixed-distance approach at ~250 pts (24-delta, ~65% PoP).

        Falls back to fixed-distance calculate_strikes() on any failure.
        """
        if not spot or spot <= 0 or not iv or iv <= 0 or not dte_days or dte_days <= 0:
            logger.warning(
                "Delta-strike requires valid inputs — falling back: spot=%s iv=%s dte=%s",
                spot, iv, dte_days,
            )
            return self.calculate_strikes(spot, live_iv=iv)

        td = max(0.05, min(0.30, target_delta if target_delta is not None else self.target_short_delta))

        sc_strike = self._strike_for_delta(spot, td, iv, dte_days, "CE")
        sp_strike = self._strike_for_delta(spot, -td, iv, dte_days, "PE")
        wing = max(self.strike_rounding, self.wing_width)
        lc_strike = sc_strike + wing
        lp_strike = sp_strike - wing

        if sc_strike <= spot or sp_strike >= spot:
            logger.warning(
                "Delta strikes out of range SC=%d SP=%d spot=%.0f — falling back",
                sc_strike, sp_strike, spot,
            )
            return self.calculate_strikes(spot, live_iv=iv)

        if sc_strike >= lc_strike or sp_strike <= lp_strike:
            return self.calculate_strikes(spot, live_iv=iv)

        sc_delta = round(abs(self._bsm(spot, sc_strike, iv, dte_days, "CE")["delta"]), 3)
        sp_delta = round(abs(self._bsm(spot, sp_strike, iv, dte_days, "PE")["delta"]), 3)

        logger.info(
            "Delta-strikes target=%.2f SC=%d(Δ%.3f) SP=%d(Δ%.3f) LC=%d LP=%d",
            td, sc_strike, sc_delta, sp_strike, sp_delta, lc_strike, lp_strike,
        )
        return {
            "short_call": sc_strike,
            "long_call": lc_strike,
            "short_put": sp_strike,
            "long_put": lp_strike,
            "call_width": lc_strike - sc_strike,
            "put_width": sp_strike - lp_strike,
        }

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
        tick_buffer = self.ic_breach_noise_buffer
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

    def estimate_dynamic_entry_credit(
        self, spot: float, dte_days: float | None = None
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

        mode = str(getattr(self.settings, "mode", "paper")).lower()
        if mode == "conservative":
            credit *= 0.92
        elif mode == "aggressive":
            credit *= 1.06

        # DTE scaling: premium is proportional to sqrt(T) — 30 DTE is the baseline.
        # 7 DTE → 0.48×, 1 DTE → 0.18×, 60 DTE → capped at 1.25×.
        if dte_days is not None and dte_days > 0:
            dte_scale = min(math.sqrt(dte_days / 30.0), 1.25)
            credit *= dte_scale
            logger.debug(
                "DTE-scaled credit dte=%.1f scale=%.3f credit_after=%.2f",
                dte_days, dte_scale, credit,
            )

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

    def calculate_probability_of_profit(
        self,
        spot: float,
        short_call: int,
        short_put: int,
        net_premium: float,
        iv: float,
        dte_days: float,
    ) -> dict[str, float]:
        """
        Statistical probability of profit using the log-normal distribution.

        PoP = P(lower_BE < spot_at_expiry < upper_BE)
            = N(d2_upper) − N(d2_lower)   (risk-neutral measure)

        This is the same calculation institutional desks use for IC risk management.
        """
        if spot <= 0 or iv <= 0 or dte_days <= 0:
            return {
                "pop": 50.0, "p_max_profit": 50.0,
                "upper_breakeven": float(short_call) + net_premium,
                "lower_breakeven": float(short_put) - net_premium,
            }

        t = max(dte_days / 365.0, 1e-6)
        sqrt_t = math.sqrt(t)

        def _prob_below(k: float) -> float:
            if k <= 0:
                return 0.0
            try:
                d2 = (math.log(k / spot) - 0.5 * iv * iv * t) / (iv * sqrt_t)
                return self._norm_cdf(d2)
            except (ValueError, ZeroDivisionError):
                return 0.5

        upper_be = short_call + net_premium
        lower_be = short_put - net_premium

        pop = max(0.0, min(100.0, (_prob_below(upper_be) - _prob_below(lower_be)) * 100.0))
        p_max = max(0.0, min(100.0, (_prob_below(short_call) - _prob_below(short_put)) * 100.0))

        return {
            "pop": round(pop, 1),
            "p_max_profit": round(p_max, 1),
            "upper_breakeven": round(upper_be, 2),
            "lower_breakeven": round(lower_be, 2),
        }

    def evaluate_iv_rank(
        self,
        current_iv: float,
        iv_52w_high: float,
        iv_52w_low: float,
    ) -> dict[str, Any]:
        """
        IV Rank for premium-selling regime timing.

        IV Rank 30–70 is the sweet spot for Iron Condors:
        - Too low (<20): premium crushed, not worth selling
        - Too high (>80): tail risk, avoid
        """
        if iv_52w_high <= 0 or iv_52w_high <= iv_52w_low:
            return {"iv_rank": 50.0, "signal": "data_unavailable", "entry_ok": True}

        iv_rank = max(0.0, min(100.0,
            (current_iv - iv_52w_low) / (iv_52w_high - iv_52w_low) * 100.0
        ))
        entry_ok = self.min_iv_rank_entry <= iv_rank <= self.max_iv_rank_entry

        if iv_rank < 20.0:
            signal = "iv_crushed_avoid"
        elif iv_rank < 30.0:
            signal = "iv_low_marginal"
        elif iv_rank <= 50.0:
            signal = "iv_optimal_low"
        elif iv_rank <= 70.0:
            signal = "iv_optimal_high"
        elif iv_rank <= 80.0:
            signal = "iv_elevated_caution"
        else:
            signal = "iv_extreme_avoid"

        return {
            "iv_rank": round(iv_rank, 1),
            "signal": signal,
            "entry_ok": entry_ok,
            "min_threshold": self.min_iv_rank_entry,
            "max_threshold": self.max_iv_rank_entry,
        }

    def score_entry(
        self,
        spot: float,
        iv: float,
        dte_days: float,
        iv_rank: float | None = None,
        trend_strength: float = 0.0,
    ) -> dict[str, Any]:
        """
        Composite IC entry score (0–100). Score >= min_entry_score required for entry.

        Component weights:
          PoP          30%  — statistical probability finishing between breakevens
          Theta/Vega   25%  — daily theta earned per unit of vega risk
          IV regime    20%  — IV in optimal 15–25% range for NIFTY
          Trend        15%  — market must be directionless (0=flat, 1=strong trend)
          IV rank      10%  — rank within 52-week IV range

        This is the same multi-factor scoring used by professional options desks.
        """
        if not spot or spot <= 0 or not iv or iv <= 0 or not dte_days or dte_days <= 0:
            return {"score": 0.0, "verdict": "invalid_inputs", "entry_ok": False}

        td = self.target_short_delta
        sc_s = self._strike_for_delta(spot, td, iv, dte_days, "CE")
        sp_s = self._strike_for_delta(spot, -td, iv, dte_days, "PE")
        lc_s = sc_s + self.wing_width
        lp_s = sp_s - self.wing_width

        sc = self._bsm(spot, sc_s, iv, dte_days, "CE")
        sp = self._bsm(spot, sp_s, iv, dte_days, "PE")
        lc = self._bsm(spot, lc_s, iv, dte_days, "CE")
        lp = self._bsm(spot, lp_s, iv, dte_days, "PE")

        net_premium = max(0.0, (sc["price"] + sp["price"]) - (lc["price"] + lp["price"]))

        # 1. PoP: 60% PoP → score 0;  85% PoP → score 100
        pop_data = self.calculate_probability_of_profit(spot, sc_s, sp_s, net_premium, iv, dte_days)
        pop = pop_data["pop"]
        pop_score = min(100.0, max(0.0, (pop - 60.0) / 25.0 * 100.0))

        # 2. Net theta/vega: daily ₹ earned per 1% IV risk
        net_theta = abs(sc["theta"] + sp["theta"] - lc["theta"] - lp["theta"])
        net_vega = max(1e-6, abs(lc["vega"] + lp["vega"] - sc["vega"] - sp["vega"]))
        tv_ratio = net_theta / net_vega
        tv_score = min(100.0, tv_ratio * 300.0)   # tv_ratio ~0.33 = score 100

        # 3. IV regime: 15–25% optimal for NIFTY weekly IC
        iv_pct = iv * 100.0
        if 15.0 <= iv_pct <= 25.0:
            iv_score = 100.0
        elif iv_pct < 15.0:
            iv_score = max(0.0, iv_pct / 15.0 * 80.0)
        else:
            iv_score = max(0.0, 100.0 - (iv_pct - 25.0) / 10.0 * 100.0)

        # 4. Trend neutrality: IC requires non-directional market
        trend_score = max(0.0, (1.0 - min(1.0, trend_strength)) * 100.0)

        # 5. IV rank: 30–65 optimal zone
        if iv_rank is not None:
            if 30.0 <= iv_rank <= 65.0:
                rank_score = 100.0
            else:
                rank_score = max(0.0, 100.0 - abs(iv_rank - 47.5) * 2.5)
        else:
            rank_score = 50.0

        score = round(min(100.0, max(0.0,
            pop_score * 0.30
            + tv_score  * 0.25
            + iv_score  * 0.20
            + trend_score * 0.15
            + rank_score * 0.10
        )), 1)

        if score >= 80:
            verdict = "excellent"
        elif score >= 65:
            verdict = "good"
        elif score >= 50:
            verdict = "marginal"
        else:
            verdict = "avoid"

        logger.info(
            "Entry score=%.1f verdict=%s pop=%.1f%% theta_vega=%.4f iv=%.1f%% trend=%.2f rank=%s",
            score, verdict, pop, tv_ratio, iv_pct, trend_strength, iv_rank,
        )

        return {
            "score": score,
            "verdict": verdict,
            "entry_ok": score >= self.min_entry_score,
            "pop": pop,
            "p_max_profit": pop_data["p_max_profit"],
            "net_premium_bsm": round(net_premium, 2),
            "net_theta_daily": round(net_theta, 2),
            "net_vega": round(net_vega, 4),
            "tv_ratio": round(tv_ratio, 4),
            "iv_pct": round(iv_pct, 1),
            "upper_breakeven": pop_data["upper_breakeven"],
            "lower_breakeven": pop_data["lower_breakeven"],
            "strikes": {"short_call": sc_s, "long_call": lc_s, "short_put": sp_s, "long_put": lp_s},
            "component_scores": {
                "pop_score": round(pop_score, 1),
                "tv_score": round(tv_score, 1),
                "iv_score": round(iv_score, 1),
                "trend_score": round(trend_score, 1),
                "rank_score": round(rank_score, 1),
            },
        }

    def estimate_net_premium(self, spot: float, days: int = 30) -> float:
        net = self.estimate_dynamic_entry_credit(spot, dte_days=float(days))

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
        current_iv: float | None = None,
        dte_days: float | None = None,
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
        long_call = float(strikes.get("long_call", 0))
        long_put = float(strikes.get("long_put", 0))

        if short_call <= 0 or short_put <= 0:
            return round(float(entry_premium), 2)

        # BSM path: use live IV when available — most accurate estimate
        if current_iv is not None and current_iv > 0 and long_call > 0 and long_put > 0:
            effective_dte = dte_days if (dte_days is not None and dte_days > 0) else max(
                0.5 / 365.0,
                (
                    entry_time.replace(
                        hour=self.exit_time.hour,
                        minute=self.exit_time.minute,
                        second=0,
                        microsecond=0,
                    ) - current_time
                ).total_seconds() / 86400.0,
            )
            try:
                bsm_premium = max(0.1, (
                    self.bsm_price(current_spot, short_call, current_iv, effective_dte, "CE")
                    + self.bsm_price(current_spot, short_put, current_iv, effective_dte, "PE")
                    - self.bsm_price(current_spot, long_call, current_iv, effective_dte, "CE")
                    - self.bsm_price(current_spot, long_put, current_iv, effective_dte, "PE")
                ))
                logger.debug(
                    "BSM premium spot=%.2f iv=%.3f dte=%.3f bsm=%.2f",
                    current_spot, current_iv, effective_dte, bsm_premium,
                )
                return round(bsm_premium, 2)
            except Exception as exc:
                logger.warning("BSM premium failed, using heuristic: %s", exc)

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
        dte_days: float | None = None,
    ) -> str | None:
        if not entry_premium or entry_premium <= 0:
            return None

        # Force exit when within 1 DTE to avoid expiry-day gamma risk.
        # Weekly NIFTY Tuesday expiry concentrates extreme gamma Monday-Tuesday;
        # holding through this window is the leading cause of condor blow-ups.
        if dte_days is not None:
            force_exit_dte = float(getattr(self.settings, "ic_force_exit_dte", 1.0))
            if dte_days < force_exit_dte:
                logger.warning(
                    "GAMMA_RISK_EXIT: dte_days=%.2f < threshold=%.2f", dte_days, force_exit_dte
                )
                return "GAMMA_RISK_EXIT"

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
            eod_loss_cut = -abs(self.ic_eod_loss_cut)
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

    def get_partial_exit_signal(
        self,
        entry_premium: float,
        current_premium: float,
        qty: int,
        elapsed_pct: float = 0.0,
    ) -> dict[str, Any]:
        """
        Multi-level profit scale-out for Iron Condors.

        Research shows that closing at 50% of max profit captures ~80% of the
        expected value while cutting the time-in-trade (and therefore tail risk)
        in half. Scale-out levels:
          25% profit → exit partial_exit_25_qty_pct of position
          50% profit → exit partial_exit_50_qty_pct of position
          75% profit → exit all remaining

        Returns action and lot sizes for the trading engine to act on.
        """
        if not self.partial_exit_enabled or entry_premium <= 0 or qty <= 0:
            return {"action": "hold", "partial_qty": 0, "profit_pct": 0.0}

        profit_pct = max(0.0, 1.0 - (current_premium / entry_premium))

        # Near end-of-session: lock in any meaningful profit rather than waiting
        # for a full threshold — theta has done its work and risk/reward shifts.
        if elapsed_pct >= 0.80 and profit_pct >= 0.15:
            logger.info(
                "EOD profit lock: elapsed=%.0f%% profit=%.1f%% — exiting all",
                elapsed_pct * 100, profit_pct * 100,
            )
            return {
                "action": "scale_exit_eod_lock",
                "partial_qty": qty,
                "remaining_qty": 0,
                "profit_pct": round(profit_pct * 100.0, 1),
                "current_premium": current_premium,
                "entry_premium": entry_premium,
            }

        if profit_pct >= 0.75:
            partial_qty = qty
            action = "scale_exit_75pct"
        elif profit_pct >= 0.50:
            partial_qty = max(1, round(qty * self.partial_exit_50_qty_pct))
            action = "scale_exit_50pct"
        elif profit_pct >= 0.25:
            partial_qty = max(1, round(qty * self.partial_exit_25_qty_pct))
            action = "scale_exit_25pct"
        else:
            return {
                "action": "hold",
                "partial_qty": 0,
                "profit_pct": round(profit_pct * 100.0, 1),
                "remaining_qty": qty,
            }

        partial_qty = min(partial_qty, qty)
        logger.info(
            "Partial exit signal action=%s qty=%d/%d profit=%.1f%%",
            action, partial_qty, qty, profit_pct * 100.0,
        )
        return {
            "action": action,
            "partial_qty": partial_qty,
            "remaining_qty": max(0, qty - partial_qty),
            "profit_pct": round(profit_pct * 100.0, 1),
            "current_premium": current_premium,
            "entry_premium": entry_premium,
        }

    def should_roll_leg(
        self,
        spot: float,
        threatened_strike: int,
        opt_type: str,
        iv: float,
        dte_days: float,
        original_credit: float,
    ) -> dict[str, Any]:
        """
        Analyze whether to roll a threatened IC leg instead of closing the full position.

        Rolling logic (standard institutional approach):
        - Roll when leg delta >= roll_delta_threshold (default 0.30 = 30-delta)
        - Find new strike at original target_delta (default 10-delta)
        - Roll is viable if cost <= 20% of original credit (i.e., net debit is small)
        - Requires >= 2 DTE (not worth rolling on expiry day)

        A successful roll extends the trade at better strikes, recovering potential
        losses rather than taking them. This is the single highest-impact IC adjustment.
        """
        if spot <= 0 or iv <= 0 or dte_days <= 0 or threatened_strike <= 0:
            return {"should_roll": False, "reason": "invalid_inputs"}

        current = self._bsm(spot, threatened_strike, iv, dte_days, opt_type)
        current_delta = abs(current["delta"])
        current_price = current["price"]

        td = self.target_short_delta
        if opt_type == "CE":
            new_strike = self._strike_for_delta(spot, td, iv, dte_days, "CE")
            roll_direction = "up"
        else:
            new_strike = self._strike_for_delta(spot, -td, iv, dte_days, "PE")
            roll_direction = "down"

        new = self._bsm(spot, new_strike, iv, dte_days, opt_type)
        new_price = new["price"]
        roll_cost = current_price - new_price   # positive = net debit to roll
        max_roll_debit = original_credit * 0.20

        should_roll = (
            current_delta >= self.roll_delta_threshold
            and dte_days >= 2.0
            and new_strike != threatened_strike
            and roll_cost <= max_roll_debit
        )

        if current_delta < self.roll_delta_threshold:
            reason = "delta_ok_no_roll_needed"
        elif dte_days < 2.0:
            reason = "dte_too_low_to_roll"
        elif roll_cost > max_roll_debit:
            reason = "roll_too_expensive"
        elif new_strike == threatened_strike:
            reason = "no_better_strike_available"
        else:
            reason = "roll_recommended"

        logger.info(
            "Roll analysis %s strike=%d delta=%.3f new_strike=%d roll_cost=%.2f should_roll=%s",
            opt_type, threatened_strike, current_delta, new_strike, roll_cost, should_roll,
        )
        return {
            "should_roll": should_roll,
            "reason": reason,
            "current_strike": threatened_strike,
            "current_delta": round(current_delta, 3),
            "current_price": round(current_price, 2),
            "new_strike": new_strike,
            "new_delta": round(abs(new["delta"]), 3),
            "new_price": round(new_price, 2),
            "roll_cost": round(roll_cost, 2),
            "roll_direction": roll_direction,
            "max_allowed_debit": round(max_roll_debit, 2),
            "dte_days": round(dte_days, 2),
        }

    def estimate_round_trip_charges(
        self,
        entry_premium: float,
        exit_premium: float,
        qty: int,
        entry_legs: list[dict] | None = None,
        exit_legs: list[dict] | None = None,
    ) -> dict[str, float]:
        if entry_premium <= 0 or exit_premium < 0 or qty <= 0:
            return self._zero_charges()

        entry_turnover = entry_premium * qty
        exit_turnover = exit_premium * qty
        total_turnover = entry_turnover + exit_turnover

        brokerage = self.brokerage_per_order * (self.entry_order_count + self.exit_order_count)

        # STT: NSE charges on the sell side only.
        # Per-leg basis (accurate): at entry we SELL shorts; at exit we SELL longs (to close).
        # Net-premium basis (approximation): used when leg data unavailable.
        if entry_legs and exit_legs:
            entry_sell = sum(
                float(l.get("fill_price") or l.get("entry_price") or l.get("price") or 0.0)
                for l in entry_legs
                if str(l.get("side", "")).upper() == "SELL"
            )
            # At exit: legs where exit_side==SELL (the longs we sell back to close)
            exit_sell = sum(
                float(l.get("exit_price") or l.get("current_close_price") or l.get("current_price") or 0.0)
                for l in exit_legs
                if str(l.get("exit_side", "")).upper() == "SELL"
                or (not l.get("exit_side") and str(l.get("side", "")).upper() == "BUY")
            )
            stt = (entry_sell + exit_sell) * qty * self.stt_sell_rate
        else:
            # Approximation without leg prices — understates entry STT, overstates exit STT
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
        entry_legs: list[dict] | None = None,
        exit_legs: list[dict] | None = None,
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
            entry_legs=entry_legs,
            exit_legs=exit_legs,
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
