import asyncio
import sys
import os

# Add the backend to the path
sys.path.insert(0, 'backend')

from backend.app.broker.samco_client import SamcoClient
from backend.app.core.config_loader import get_settings

async def test_broker():
    settings = get_settings()
    broker = SamcoClient()

    print(f"📊 Testing broker in {settings.mode.upper()} mode...")

    try:
        # Test login
        print("🔐 Logging in...")
        await broker.login()
        print("✅ Login successful")

        # Test market data
        print("📈 Getting NIFTY index quote...")
        quote = await broker.get_index_quote("NIFTY 50")
        print(f"📊 Raw quote: {quote}")

        spot = SamcoClient.parse_spot(quote)
        print(f"💰 Parsed spot: {spot}")

        if spot:
            print("✅ Market data working!")
        else:
            print("❌ Market data failed - spot is None")

    except Exception as e:
        print(f"❌ Broker test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_broker())