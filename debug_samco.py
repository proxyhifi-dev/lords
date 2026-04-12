"""
Debug script — checks exactly what Samco returns
Run: python debug_samco.py
"""
import asyncio
import sys
from pathlib import Path
from datetime import date

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from backend.app.broker.samco_client import SamcoClient

async def main():
    client = SamcoClient()
    print("Logging in...")
    await client.login()
    print("Login OK\n")

    # Test date — we know Mar 02 2026 worked before
    test_date = "2026-03-02"
    test_date2 = "2025-04-14"  # failing date

    for d in [test_date, test_date2]:
        print(f"\n{'='*55}")
        print(f"Testing: {d}")
        print(f"{'='*55}")

        result = await asyncio.to_thread(
            client.samco.get_index_intraday_candle_data,
            index_name="NIFTY 50",
            from_date=f"{d} 09:15:00",
            to_date=f"{d} 15:30:00",
        )
        response = SamcoClient._parse_response(result)
        print(f"Status  : {response.get('status')}")
        print(f"Message : {response.get('message')}")
        print(f"Errors  : {response.get('validationErrors')}")
        print(f"Keys    : {list(response.keys())}")

        # Show first 200 chars of raw response
        raw = str(result)[:300] if result else "None"
        print(f"Raw     : {raw}")

        # Check what data keys exist
        for key in ["data","intradayCandleData","indexCandleData","candles"]:
            val = response.get(key)
            if val:
                print(f"Found data key '{key}': {len(val)} items")
                if val:
                    print(f"First item: {val[0]}")
                break

asyncio.run(main())