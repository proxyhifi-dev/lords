from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from engine.market_feed_engine import MarketFeedEngine


class FakeMarketDataService:
    def __init__(self) -> None:
        self.calls = 0

    async def get_nifty_spot(self) -> float:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError('transient')
        return 22123.5


async def _run_test() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    service = FakeMarketDataService()
    engine = MarketFeedEngine(service, queue)
    task = asyncio.create_task(engine.run())
    await asyncio.sleep(6.5)
    engine.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert not queue.empty()
    tick = queue.get_nowait()
    assert tick.price == 22123.5


def test_market_feed_recovers_after_error() -> None:
    asyncio.run(_run_test())
