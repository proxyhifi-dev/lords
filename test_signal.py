import asyncio
import sys
import os

# Add the backend to the path
sys.path.insert(0, 'backend')

from backend.app.core.event_bus import EventBus
from backend.app.core.config_loader import get_settings

async def test_signal():
    settings = get_settings()
    event_bus = EventBus()

    print("🧪 Testing manual signal trigger...")

    # Publish a test RISK_APPROVED event
    await event_bus.publish("RISK_APPROVED", {
        "signal": "LONG",
        "size_label": "FULL"
    })

    print("✅ RISK_APPROVED event published")
    print("Check logs for trading engine response...")

    # Wait a bit to see if anything happens
    await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(test_signal())