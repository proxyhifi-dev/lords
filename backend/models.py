from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class OptionStrikeData(BaseModel):
    strike_price: float
    ce_oi: float = 0.0
    pe_oi: float = 0.0
    ce_ltp: float = 0.0
    pe_ltp: float = 0.0


class OptionChainSnapshot(BaseModel):
    symbol: str
    exchange: str
    expiry_date: str
    spot_price: float
    atm_strike: float
    strikes: list[OptionStrikeData] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SignalData(BaseModel):
    signal: Literal["CALL", "PUT", "NEUTRAL"]
    reason: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class DashboardState(BaseModel):
    snapshot: OptionChainSnapshot | None = None
    signal: SignalData | None = None
    last_error: str | None = None
