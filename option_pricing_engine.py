"""
Calibrated Realistic Synthetic Option Pricing Engine for Lords Bot

Goal:
- Avoid fake 100% wins.
- Avoid fake 100% instant stop-losses.
- Debit starts near entry credit.
- Theta slowly helps.
- Direction, gamma, IV/range, breach, and friction hurt.

Important:
This is NOT real option-chain replay.
This is a calibrated synthetic model using real NIFTY spot candles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PricingConfig:
    session_minutes: int = 375

    # Remaining debit baseline:
    # Starts near entry credit and decays slowly through the day.
    theta_decay_strength: float = 0.28
    min_theta_floor_pct: float = 0.58

    # Directional risk:
    directional_multiplier: float = 8.0
    directional_noise_floor_pct: float = 0.0008

    # Gamma risk:
    gamma_safe_pct: float = 0.015
    gamma_danger_zone_pct: float = 0.018
    gamma_near_zone_pct: float = 0.009
    gamma_danger_multiple: float = 0.65
    gamma_near_multiple: float = 1.25

    # IV / range expansion:
    iv_move_multiplier: float = 7.0
    iv_range_multiplier: float = 5.0
    iv_noise_floor_move_pct: float = 0.0010
    iv_noise_floor_range_pct: float = 0.0015
    iv_max_pct: float = 0.45

    # Breach risk:
    breach_base_multiple: float = 1.45
    breach_point_multiplier: float = 0.45

    # Friction:
    spread_pct: float = 0.025
    slippage_pct: float = 0.015

    # Trend penalty:
    trend_threshold_pct: float = 0.0045
    trend_multiple: float = 0.35


MODE_CONFIG = {
    "conservative": PricingConfig(
        theta_decay_strength=0.22,
        min_theta_floor_pct=0.64,
        directional_multiplier=9.5,
        gamma_danger_multiple=0.75,
        gamma_near_multiple=1.45,
        iv_move_multiplier=8.0,
        iv_range_multiplier=6.0,
        spread_pct=0.035,
        slippage_pct=0.020,
        trend_multiple=0.45,
    ),
    "balanced": PricingConfig(),
    "realistic": PricingConfig(),
    "aggressive": PricingConfig(
        theta_decay_strength=0.34,
        min_theta_floor_pct=0.52,
        directional_multiplier=6.5,
        gamma_danger_multiple=0.55,
        gamma_near_multiple=1.05,
        iv_move_multiplier=6.0,
        iv_range_multiplier=4.0,
        spread_pct=0.020,
        slippage_pct=0.012,
        trend_multiple=0.28,
    ),
}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def get_config(mode: str) -> PricingConfig:
    return MODE_CONFIG.get(str(mode).lower(), MODE_CONFIG["realistic"])


def estimate_dynamic_entry_credit(
    entry_spot: float,
    short_call: float,
    short_put: float,
    wing_width: float = 300.0,
    mode: str = "realistic",
) -> float:
    """
    Estimate Iron Condor entry credit per unit.

    This should not be constant.
    It depends on:
    - spot level
    - distance to short strikes
    - wing width
    - mode
    """

    if entry_spot <= 0:
        return 0.0

    upper_distance = abs(short_call - entry_spot)
    lower_distance = abs(entry_spot - short_put)
    avg_distance = max((upper_distance + lower_distance) / 2.0, 1.0)

    distance_pct = avg_distance / entry_spot

    # Weekly-ish IC credit approximation.
    base_credit = entry_spot * 0.00225

    # Closer strikes collect more premium.
    distance_adjustment = math.exp(-distance_pct * 9.0)

    # Wing width effect.
    width_adjustment = clamp(wing_width / 300.0, 0.80, 1.25)

    credit = base_credit * (0.85 + distance_adjustment) * width_adjustment

    if mode == "conservative":
        credit *= 0.92
    elif mode == "aggressive":
        credit *= 1.06

    return round(clamp(credit, 45.0, 105.0), 2)


def theta_component(entry_credit: float, minutes_elapsed: float, mode: str) -> float:
    """
    Remaining debit caused by time value.

    At entry:
        roughly entry_credit

    By end:
        reduced, but not zero.
    """

    cfg = get_config(mode)
    progress = clamp(minutes_elapsed / cfg.session_minutes, 0.0, 1.0)

    remaining = entry_credit * (1.0 - cfg.theta_decay_strength * progress)
    floor = entry_credit * cfg.min_theta_floor_pct

    return max(floor, remaining)


def directional_component(
    entry_credit: float,
    entry_spot: float,
    spot: float,
    mode: str,
) -> float:
    """
    Penalizes spot movement away from entry.

    Tiny first-minute noise is ignored.
    """

    cfg = get_config(mode)
    move_pct = abs(spot - entry_spot) / max(entry_spot, 1.0)

    if move_pct < cfg.directional_noise_floor_pct:
        return 0.0

    adjusted_move = move_pct - cfg.directional_noise_floor_pct
    return entry_credit * ((adjusted_move * cfg.directional_multiplier) ** 1.35)


def gamma_component(
    entry_credit: float,
    spot: float,
    short_call: float,
    short_put: float,
    mode: str,
) -> float:
    """
    Premium expands more aggressively near short strikes.
    """

    cfg = get_config(mode)

    nearest_pct = min(
        abs(short_call - spot),
        abs(spot - short_put),
    ) / max(spot, 1.0)

    # Far from short strikes: almost no gamma penalty.
    if nearest_pct > cfg.gamma_danger_zone_pct:
        return entry_credit * cfg.gamma_safe_pct

    # Very near short strikes: gamma expansion.
    if nearest_pct <= cfg.gamma_near_zone_pct:
        proximity = (cfg.gamma_near_zone_pct - nearest_pct) / cfg.gamma_near_zone_pct
        return entry_credit * cfg.gamma_near_multiple * (proximity ** 2)

    # Danger zone but not very near.
    proximity = (cfg.gamma_danger_zone_pct - nearest_pct) / (
        cfg.gamma_danger_zone_pct - cfg.gamma_near_zone_pct
    )
    return entry_credit * cfg.gamma_danger_multiple * (proximity ** 2)


def iv_component(
    entry_credit: float,
    entry_spot: float,
    spot: float,
    day_high: float | None,
    day_low: float | None,
    mode: str,
) -> float:
    """
    Models IV/range expansion.

    If day range expands, option debit increases.
    """

    cfg = get_config(mode)

    move_pct = abs(spot - entry_spot) / max(entry_spot, 1.0)

    if day_high and day_low and day_high > 0 and day_low > 0:
        range_pct = abs(day_high - day_low) / max(entry_spot, 1.0)
    else:
        range_pct = move_pct

    move_excess = max(0.0, move_pct - cfg.iv_noise_floor_move_pct)
    range_excess = max(0.0, range_pct - cfg.iv_noise_floor_range_pct)

    iv_pct = (
        move_excess * cfg.iv_move_multiplier
        + range_excess * cfg.iv_range_multiplier
    )

    iv_pct = clamp(iv_pct, 0.0, cfg.iv_max_pct)

    return entry_credit * iv_pct


def breach_component(
    entry_credit: float,
    spot: float,
    short_call: float,
    short_put: float,
    mode: str,
) -> float:
    """
    Strong penalty only after actual short strike breach.
    """

    cfg = get_config(mode)

    if spot >= short_call:
        points = spot - short_call
        return entry_credit * cfg.breach_base_multiple + points * cfg.breach_point_multiplier

    if spot <= short_put:
        points = short_put - spot
        return entry_credit * cfg.breach_base_multiple + points * cfg.breach_point_multiplier

    return 0.0


def trend_component(
    entry_credit: float,
    entry_spot: float,
    spot: float,
    mode: str,
) -> float:
    """
    Extra penalty after meaningful trend movement.
    """

    cfg = get_config(mode)
    move_pct = abs(spot - entry_spot) / max(entry_spot, 1.0)

    if move_pct <= cfg.trend_threshold_pct:
        return 0.0

    excess = move_pct - cfg.trend_threshold_pct
    return entry_credit * cfg.trend_multiple * (1.0 + excess * 80.0)


def friction_component(entry_credit: float, mode: str) -> float:
    """
    Bid/ask + slippage approximation.
    """

    cfg = get_config(mode)
    return entry_credit * (cfg.spread_pct + cfg.slippage_pct)


def estimate_ic_debit(
    entry_credit: float,
    entry_spot: float,
    spot: float,
    sc: float,
    sp: float,
    minutes: float,
    mode: str = "realistic",
    day_high: float | None = None,
    day_low: float | None = None,
) -> float:
    """
    Current cost/debit to close Iron Condor.

    Debit = theta + direction + gamma + IV/range + breach + trend + friction
    """

    if entry_credit <= 0 or entry_spot <= 0 or spot <= 0:
        return max(entry_credit, 0.0)

    theta = theta_component(entry_credit, minutes, mode)
    direction = directional_component(entry_credit, entry_spot, spot, mode)
    gamma = gamma_component(entry_credit, spot, sc, sp, mode)
    iv = iv_component(entry_credit, entry_spot, spot, day_high, day_low, mode)
    breach = breach_component(entry_credit, spot, sc, sp, mode)
    trend = trend_component(entry_credit, entry_spot, spot, mode)
    friction = friction_component(entry_credit, mode)

    debit = theta + direction + gamma + iv + breach + trend + friction

    return round(max(1.0, debit), 2)