import asyncio
import sys
import os

# Add the backend to the path
sys.path.insert(0, 'backend')

from backend.app.core.event_bus import EventBus
from backend.app.core.config_loader import get_settings
from backend.app.engine.state_manager import state_manager

async def update_state_and_test():
    settings = get_settings()
    event_bus = EventBus()

    print("🔄 Updating state with current market data...")

    # Update spot price
    await state_manager.update(spot_price=24148.4)
    print("✅ Updated spot_price to 24148.4")

    # Check state
    state = await state_manager.snapshot()
    print(f"📊 State check: spot={state.spot_price}, orb_high={state.orb_high}")
    breakout = state.spot_price > state.orb_high if state.spot_price and state.orb_high else False
    print(f"🚀 Breakout condition: {breakout}")

    print("🧪 Testing signal trigger...")
    await event_bus.publish("RISK_APPROVED", {
        "signal": "LONG",
        "size_label": "FULL"
    })
    print("✅ RISK_APPROVED event published")

    # Wait and check state again
    await asyncio.sleep(3)
    state = await state_manager.snapshot()
    has_trade = state.active_trade is not None
    print(f"📊 Post-signal check: active_trade={has_trade}")

if __name__ == "__main__":
    asyncio.run(update_state_and_test())