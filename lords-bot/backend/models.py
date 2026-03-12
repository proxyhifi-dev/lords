from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class OptionRow(BaseModel):
    strike_price: float
    call_oi: float
    put_oi: float
    call_change_oi: float
    put_change_oi: float
    call_ltp: float
    put_ltp: float
    volume: float


class AnalysisResult(BaseModel):
    symbol: str
    expiry: str
    pcr: float
    trend: Literal['BULLISH', 'BEARISH', 'SIDEWAYS']
    support: float
    resistance: float
    atm_strike: float
    volume_spikes: list[float] = Field(default_factory=list)
    smart_money_bias: Literal['CALL_BUYING', 'PUT_BUYING', 'NEUTRAL']
    generated_at: str


class SignalResult(BaseModel):
    signal: Literal['BUY CALL', 'BUY PUT', 'NO TRADE']
    confidence: float
    reason: str
    generated_at: str


class Trade(BaseModel):
    id: int
    position_type: Literal['CALL', 'PUT']
    symbol: str
    strike: float
    entry_price: float
    exit_price: float | None = None
    quantity: int = 1
    status: Literal['OPEN', 'CLOSED'] = 'OPEN'
    entry_time: str
    exit_time: str | None = None
    pnl: float = 0.0


class PnLSnapshot(BaseModel):
    capital: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    open_positions: int
