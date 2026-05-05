from __future__ import annotations

import asyncio
from datetime import datetime, time
from zoneinfo import ZoneInfo

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings
from backend.app.core.event_bus import EventBus
from backend.app.engine.state_manager import state_manager
from backend.app.utils.logger import get_logger

settings = get_settings()
IST = ZoneInfo("Asia/Kolkata")


class MarketEngine:
    def __init__(self, event_bus: EventBus, samco_client: SamcoClient) -> None:
        self.event_bus = event_bus
        self.samco_client = samco_client
        self.state = state_manager
        self.logger = get_logger("market_engine")
        self._last_signal_ts = 0.0

    async def run(self) -> None:
        self.logger.info("MarketEngine started")

        while True:
            try:
                quote = await self.samco_client.get_quote(
                    symbol_name=settings.nifty_symbol,
                    exchange=settings.nifty_exchange,
                )

                spot = self.samco_client.parse_ltp(quote)
                if spot is None:
                    spot = self.samco_client.parse_spot(quote)

                await self.event_bus.publish("MARKET_QUOTE", {"quote": quote, "spot": spot})

                if spot is not None:
                    await self.state.update(spot_price=spot)
                    await self._maybe_emit_iron_condor_signal(spot)

            except Exception as exc:
                self.logger.error("MarketEngine loop error: %s", exc, exc_info=True)

            await asyncio.sleep(settings.poll_seconds)

    async def _maybe_emit_iron_condor_signal(self, spot: float) -> None:
        now = datetime.now(IST)
        state = await self.state.snapshot()

        if not self._can_emit_signal(now, state):
            return

        payload = {
            "signal": "IRON_CONDOR",
            "spot_price": spot,
            "size_label": "FULL",
            "trend_score": 0,
        }

        await self.state.update(signal="IRON_CONDOR", signal_meta=payload)
        await self.event_bus.publish("SIGNAL", payload)
        self._last_signal_ts = now.timestamp()

        self.logger.info(
            "IRON_CONDOR SIGNAL emitted time=%s spot=%.2f",
            now.isoformat(),
            spot,
        )

    def _can_emit_signal(self, now: datetime, state) -> bool:
        if str(getattr(settings, "strategy_type", "")).lower() != "iron_condor":
            self.logger.debug("Signal blocked: strategy_type is not iron_condor")
            return False

        if not bool(getattr(settings, "iron_condor_enabled", False)):
            self.logger.debug("Signal blocked: iron_condor_enabled=False")
            return False

        if now.weekday() >= 5:
            self.logger.debug("Signal blocked: weekend")
            return False

        if not getattr(state, "trading_enabled", False):
            self.logger.debug("Signal blocked: trading_enabled=False")
            return False

        if getattr(state, "active_trade", None):
            self.logger.debug("Signal blocked: active_trade exists")
            return False

        monthly_only = bool(getattr(settings, "ic_monthly_only", False))
        if monthly_only:
            start_day = int(getattr(settings, "ic_entry_day_start", 1))
            end_day = int(getattr(settings, "ic_entry_day_end", 5))
            if not (start_day <= now.day <= end_day):
                self.logger.debug(
                    "Signal blocked: monthly day gate start=%d end=%d today=%d",
                    start_day,
                    end_day,
                    now.day,
                )
                return False
            if getattr(state, "last_iron_condor_month", None) == now.month:
                self.logger.debug("Signal blocked: already traded this month")
                return False

        try:
            sh, sm = map(int, str(settings.ic_entry_window_start).split(":"))
            eh, em = map(int, str(settings.ic_entry_window_end).split(":"))
        except Exception:
            sh, sm, eh, em = 10, 0, 10, 5
            self.logger.warning("Bad IC window config, using default 10:00-10:05")

        if not (time(sh, sm) <= now.time() < time(eh, em)):
            self.logger.debug(
                "Signal blocked: outside IC window %02d:%02d-%02d:%02d now=%s",
                sh,
                sm,
                eh,
                em,
                now.strftime("%H:%M:%S"),
            )
            return False

        if self._last_signal_ts and now.timestamp() - self._last_signal_ts < 60:
            self.logger.debug("Signal blocked: cooldown active")
            return False

        self.logger.info("Signal gate passed time=%s", now.strftime("%H:%M:%S"))
        return True