from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

strategy_logger = logging.getLogger("lords_bot.strategy")
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class TickState:
    high: float | None = None
    low: float | None = None
    last_price: float | None = None
    last_ts: dt.datetime | None = None
    samples: list[float] = field(default_factory=list)


class ORBStrategy:
    """ORB strategy fed primarily by websocket ticks to avoid history overload at market open."""

    def __init__(self, client) -> None:
        self.client = client
        self.range_date: dt.date | None = None
        self.range_high: float | None = None
        self.range_low: float | None = None
        self.signal: dict[str, Any] | None = None
        self.tick_state = TickState()

    def on_tick(self, tick: dict[str, Any]) -> None:
        """Consume incoming ticks; safe parser supports both flattened and nested FYERS frames."""
        now = dt.datetime.now(tz=IST)
        try:
            price = float(tick.get("ltp") or tick.get("lp") or tick.get("v", {}).get("lp"))
        except Exception:
            return

        self.tick_state.last_price = price
        self.tick_state.last_ts = now
        if dt.time(9, 15) <= now.time() < dt.time(9, 30):
            self.tick_state.samples.append(price)
            self.tick_state.high = price if self.tick_state.high is None else max(self.tick_state.high, price)
            self.tick_state.low = price if self.tick_state.low is None else min(self.tick_state.low, price)

        # Lock ORB once window closes.
        if now.time() >= dt.time(9, 30) and self.range_date != now.date() and self.tick_state.samples:
            self.range_date = now.date()
            self.range_high = self.tick_state.high
            self.range_low = self.tick_state.low
            strategy_logger.info("ORB locked from ticks: high=%s low=%s", self.range_high, self.range_low)

    async def _fallback_quote(self) -> float | None:
        """Fallback uses quotes endpoint, never history, so bot still works if websocket has a gap."""
        try:
            quote = await self.client.request("GET", "/quotes", params={"symbols": "NSE:NIFTY50-INDEX"})
            return float(quote.get("d", [{}])[0].get("v", {}).get("lp"))
        except Exception as exc:  # noqa: BLE001
            strategy_logger.warning("Quote fallback unavailable: %s", exc)
            return None

    async def check_breakout(self) -> dict[str, Any] | None:
        now = dt.datetime.now(tz=IST)
        if now.time() < dt.time(9, 30) or now.time() > dt.time(15, 15):
            return None

        if self.range_date != now.date() or self.range_high is None or self.range_low is None:
            # Safe fallback if websocket never provided enough opening ticks.
            if self.tick_state.samples:
                self.range_date = now.date()
                self.range_high = self.tick_state.high
                self.range_low = self.tick_state.low
            else:
                strategy_logger.info("Skipping breakout; ORB range unavailable yet")
                return None

        ltp = self.tick_state.last_price or await self._fallback_quote()
        if ltp is None:
            return None

        if ltp > float(self.range_high):
            self.signal = {
                "direction": "CALL",
                "price": ltp,
                "range_high": self.range_high,
                "range_low": self.range_low,
                "source": "websocket_or_quote",
            }
            return self.signal

        if ltp < float(self.range_low):
            self.signal = {
                "direction": "PUT",
                "price": ltp,
                "range_high": self.range_high,
                "range_low": self.range_low,
                "source": "websocket_or_quote",
            }
            return self.signal

        return None
