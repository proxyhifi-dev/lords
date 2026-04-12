"""
Debug — finds which Samco API works for historical dates
Run: python debug_historical.py
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

    test_dates = ["2025-06-02", "2025-03-03", "2026-03-02"]

    for d in test_dates:
        print(f"\n{'='*60}")
        print(f"Testing date: {d}")
        print(f"{'='*60}")

        # Test 1: get_index_intraday_candle_data
        print("\n1. get_index_intraday_candle_data")
        try:
            r = await asyncio.to_thread(
                client.samco.get_index_intraday_candle_data,
                index_name="NIFTY 50",
                from_date=f"{d} 09:15:00",
                to_date=f"{d} 15:30:00",
            )
            resp = SamcoClient._parse_response(r)
            status = resp.get("status")
            msg    = resp.get("statusMessage") or resp.get("message") or ""
            data   = resp.get("intradayCandleData") or []
            print(f"   Status: {status} | {msg} | candles: {len(data)}")
        except Exception as e:
            print(f"   Error: {e}")

        # Test 2: get_historical_candle_data
        print("\n2. get_historical_candle_data")
        try:
            r = await asyncio.to_thread(
                client.samco.get_historical_candle_data,
                symbol_name="NIFTY 50",
                exchange="NSE",
                from_date=f"{d} 09:15:00",
                to_date=f"{d} 15:30:00",
                time_interval="1minute",
            )
            resp = SamcoClient._parse_response(r)
            status = resp.get("status")
            msg    = resp.get("statusMessage") or resp.get("message") or ""
            # Check all possible data keys
            for key in ["historicalCandleData","data","candles","intradayCandleData"]:
                val = resp.get(key)
                if val:
                    print(f"   Status: {status} | {msg} | key='{key}' candles: {len(val)}")
                    if val: print(f"   First: {val[0]}")
                    break
            else:
                print(f"   Status: {status} | {msg} | Keys: {list(resp.keys())}")
        except Exception as e:
            print(f"   Error: {e}")

        # Test 3: get_historical_candle_data different params
        print("\n3. get_historical_candle_data (alternate params)")
        try:
            r = await asyncio.to_thread(
                client.samco.get_historical_candle_data,
                symbol_name="NIFTY 50",
                exchange="NSE",
                from_date=d,
                to_date=d,
                time_interval="1minute",
            )
            resp = SamcoClient._parse_response(r)
            status = resp.get("status")
            msg    = resp.get("statusMessage") or resp.get("message") or ""
            for key in ["historicalCandleData","data","candles","intradayCandleData"]:
                val = resp.get(key)
                if val:
                    print(f"   Status: {status} | {msg} | key='{key}' candles: {len(val)}")
                    break
            else:
                print(f"   Status: {status} | {msg} | Keys: {list(resp.keys())}")
        except Exception as e:
            print(f"   Error: {e}")

        # Test 4: check method signature
        print("\n4. get_historical_candle_data_with_http_info")
        try:
            import inspect
            sig = inspect.signature(client.samco.get_historical_candle_data)
            print(f"   Params: {list(sig.parameters.keys())}")
        except Exception as e:
            print(f"   Error: {e}")

asyncio.run(main())
