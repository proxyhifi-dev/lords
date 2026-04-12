"""
Debug v2 — find exact params for get_historical_candle_data
Run: python debug_historical2.py
"""
import asyncio
import sys
import inspect
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from backend.app.broker.samco_client import SamcoClient

async def main():
    client = SamcoClient()
    print("Logging in...")
    await client.login()
    print("Login OK\n")

    # Print full source/signature of get_historical_candle_data
    print("=" * 60)
    print("get_historical_candle_data signature:")
    try:
        src = inspect.getsource(client.samco.get_historical_candle_data)
        print(src[:2000])
    except Exception as e:
        print(f"Can't get source: {e}")

    # Try calling with different param combos
    print("\n" + "="*60)
    print("Testing get_historical_candle_data param combos:")

    combos = [
        {"symbol_name": "NIFTY 50", "from_date": "2025-06-02"},
        {"symbol_name": "NIFTY 50", "from_date": "2025-06-02 09:15:00"},
        {"symbol_name": "NIFTY 50", "from_date": "2025-06-02", "exchange": "NSE"},
        {"symbol_name": "NIFTY 50", "from_date": "2025-06-02 09:15:00",
         "to_date": "2025-06-02 15:30:00"},
        {"symbol_name": "NIFTY 50", "from_date": "2025-06-02",
         "to_date": "2025-06-02"},
    ]

    for i, params in enumerate(combos, 1):
        print(f"\n  Combo {i}: {params}")
        try:
            r = await asyncio.to_thread(
                client.samco.get_historical_candle_data, **params
            )
            resp = SamcoClient._parse_response(r)
            status = resp.get("status")
            msg    = resp.get("statusMessage") or resp.get("message") or ""
            keys   = list(resp.keys())
            print(f"  Result: status={status} | {msg}")
            print(f"  Keys:   {keys}")
            # Check for data in any key
            for k in keys:
                v = resp.get(k)
                if isinstance(v, list) and v:
                    print(f"  DATA found in '{k}': {len(v)} items")
                    print(f"  First: {v[0]}")
                    break
        except Exception as e:
            print(f"  Error: {e}")

    # Also check get_index_candle_data signature
    print("\n" + "="*60)
    print("get_index_candle_data signature:")
    try:
        src = inspect.getsource(client.samco.get_index_candle_data)
        print(src[:2000])
    except Exception as e:
        print(f"Can't get source: {e}")

    # Try get_index_candle_data combos
    print("\nTesting get_index_candle_data param combos:")
    idx_combos = [
        {"index_name": "NIFTY 50", "from_date": "2025-06-02"},
        {"index_name": "NIFTY 50", "from_date": "2025-06-02 09:15:00",
         "to_date": "2025-06-02 15:30:00"},
        {"index_name": "NIFTY 50", "from_date": "2025-06-02",
         "to_date": "2025-06-02"},
    ]
    for i, params in enumerate(idx_combos, 1):
        print(f"\n  Combo {i}: {params}")
        try:
            r = await asyncio.to_thread(
                client.samco.get_index_candle_data, **params
            )
            resp = SamcoClient._parse_response(r)
            status = resp.get("status")
            msg    = resp.get("statusMessage") or resp.get("message") or ""
            keys   = list(resp.keys())
            print(f"  Result: status={status} | {msg}")
            print(f"  Keys:   {keys}")
            for k in keys:
                v = resp.get(k)
                if isinstance(v, list) and v:
                    print(f"  DATA found in '{k}': {len(v)} items")
                    print(f"  First: {v[0]}")
                    break
        except Exception as e:
            print(f"  Error: {e}")

asyncio.run(main())