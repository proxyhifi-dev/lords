from __future__ import annotations

import asyncio

from backend.app.broker.samco_client import SamcoClient
from backend.app.data.option_store import OptionChainCollector


async def _main() -> None:
    client = SamcoClient()
    collector = OptionChainCollector(client)
    await collector.run_forever()


if __name__ == "__main__":
    asyncio.run(_main())
