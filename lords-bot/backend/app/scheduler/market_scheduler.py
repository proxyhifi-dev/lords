from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.app.core.event_bus import EventBus
from backend.config import settings


class MarketScheduler:
    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus
        self.scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

    def start(self) -> None:
        start_hour, start_min = [int(x) for x in settings.orb_start.split(":")]
        end_hour, end_min = [int(x) for x in settings.orb_end.split(":")]
        sq_hour, sq_min = [int(x) for x in settings.square_off.split(":")]

        self.scheduler.add_job(self._emit_start, "cron", hour=start_hour, minute=start_min)
        self.scheduler.add_job(self._emit_orb_freeze, "cron", hour=end_hour, minute=end_min)
        self.scheduler.add_job(self._emit_square_off, "cron", hour=sq_hour, minute=sq_min)
        self.scheduler.start()

    async def _emit_start(self) -> None:
        await self.event_bus.publish("SCHEDULE_START", {})

    async def _emit_orb_freeze(self) -> None:
        await self.event_bus.publish("SCHEDULE_FREEZE_ORB", {})

    async def _emit_square_off(self) -> None:
        await self.event_bus.publish("SCHEDULE_SQUARE_OFF", {})
