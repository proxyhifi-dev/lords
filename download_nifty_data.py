"""
Lords Bot — Nifty 1-Min Historical Data Downloader
====================================================
Uses your existing Samco credentials to download
1-minute OHLC data for Nifty 50 from March 1 to today.

USAGE:
------
  cd C:\\Users\\bollu\\github\\lords
  python download_nifty_data.py

OUTPUT:
-------
  data/nifty_1min_YYYYMMDD.csv   — use this with backtest.py
"""

import asyncio
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, date
from pathlib import Path

# ── Make sure project root is in path ──────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from backend.app.broker.samco_client import SamcoClient

# ── Config ──────────────────────────────────────────────
START_DATE  = date(2025, 11, 12)
END_DATE    = date.today()
OUTPUT_DIR  = ROOT / "data"
# Samco intraday endpoint allows 1 day per call only
RATE_LIMIT  = 0.5   # seconds between API calls


def trading_days(start: date, end: date):
    """Yield each weekday between start and end inclusive."""
    current = start
    while current <= end:
        if current.weekday() < 5:   # Mon–Fri only
            yield current
        current += timedelta(days=1)


def parse_candles(response: dict, trade_date: date) -> list[dict]:
    """Parse Samco intraday candle response into list of OHLC rows."""
    rows = []

    data = (
        response.get("data")
        or response.get("intradayCandleData")
        or response.get("indexCandleData")
        or []
    )

    if not isinstance(data, list):
        return rows

    for item in data:
        try:
            if isinstance(item, list) and len(item) >= 5:
                # [datetime_str, open, high, low, close, volume?]
                rows.append({
                    "datetime": str(item[0]),
                    "open":     float(item[1]),
                    "high":     float(item[2]),
                    "low":      float(item[3]),
                    "close":    float(item[4]),
                    "volume":   float(item[5]) if len(item) > 5 else 0,
                })
            elif isinstance(item, dict):
                dt = (
                    item.get("dateTime") or item.get("time") or
                    item.get("date")     or item.get("timestamp")
                )
                rows.append({
                    "datetime": str(dt),
                    "open":     float(item.get("open", 0)),
                    "high":     float(item.get("high", 0)),
                    "low":      float(item.get("low", 0)),
                    "close":    float(item.get("close", 0)),
                    "volume":   float(item.get("volume", 0)),
                })
        except (ValueError, TypeError, IndexError):
            continue

    return rows


async def download_day(client: SamcoClient, trade_date: date) -> list[dict]:
    """Download 1-min candles for a single trading day."""

    date_str = trade_date.strftime("%Y-%m-%d")

    try:
        # PRIMARY: get_index_intraday_candle_data — 1-min index data
        result = await asyncio.to_thread(
            client.samco.get_index_intraday_candle_data,
            index_name="NIFTY 50",
            from_date=f"{date_str} 09:15:00",
            to_date=f"{date_str} 15:30:00",
        )
        response = SamcoClient._parse_response(result)

        if response.get("status") == "Success":
            candles = parse_candles(response, trade_date)
            if candles:
                return candles

        # FALLBACK: get_index_candle_data with time interval
        result2 = await asyncio.to_thread(
            client.samco.get_index_candle_data,
            index_name="NIFTY 50",
            from_date=f"{date_str} 09:15:00",
            to_date=f"{date_str} 15:30:00",
            time_interval_type="minutes",
            time_interval=1,
        )
        response2 = SamcoClient._parse_response(result2)

        if response2.get("status") == "Success":
            candles2 = parse_candles(response2, trade_date)
            if candles2:
                return candles2

        # Log failure
        err = response.get("message") or response.get("validationErrors") or response
        print(f"    ⚠️  No data for {date_str}: {err}")
        return []

    except Exception as exc:
        print(f"    ❌ Error for {date_str}: {exc}")
        return []


def save_csv(rows: list[dict], filepath: Path):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["datetime", "open", "high", "low", "close", "volume"]
        )
        writer.writeheader()
        writer.writerows(rows)


async def main():
    days = list(trading_days(START_DATE, END_DATE))

    print("\n" + "═" * 55)
    print("  LORDS BOT — Nifty 1-Min Data Downloader")
    print(f"  Range  : {START_DATE} → {END_DATE}")
    print(f"  Days   : {len(days)} trading days")
    print(f"  Output : {OUTPUT_DIR}/")
    print("═" * 55 + "\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Login ───────────────────────────────────────────
    client = SamcoClient()
    print("Logging into Samco...")
    await client.login()
    print("Login successful ✅\n")

    # ── Check for existing partial download ─────────────
    today_str    = END_DATE.strftime("%Y%m%d")
    csv_path     = OUTPUT_DIR / f"nifty_1min_{today_str}.csv"
    already_done = set()

    if csv_path.exists():
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    d = datetime.fromisoformat(row["datetime"]).date()
                    already_done.add(d)
                except Exception:
                    pass
        print(f"Resuming — {len(already_done)} days already downloaded\n")

    # ── Download day by day ─────────────────────────────
    all_rows = []
    ok_days  = 0
    fail_days = 0

    for i, day in enumerate(days, 1):
        if day in already_done:
            print(f"  [{i:>2}/{len(days)}] {day} — already have ✅")
            continue

        print(f"  [{i:>2}/{len(days)}] {day} ... ", end="", flush=True)
        candles = await download_day(client, day)

        if candles:
            all_rows.extend(candles)
            ok_days += 1
            print(f"{len(candles)} candles ✅")
        else:
            fail_days += 1
            print("no data ⚠️")

        time.sleep(RATE_LIMIT)

    if not all_rows and not already_done:
        print("\n❌ No data downloaded at all.")
        print("\nThe Samco intraday index API may require a different parameter format.")
        print("Please paste the error messages above and I'll fix the script.\n")
        return

    # ── Merge with existing if resuming ─────────────────
    existing_rows = []
    if csv_path.exists() and already_done:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            existing_rows = list(reader)

    combined = existing_rows + all_rows

    # Deduplicate and sort
    seen   = set()
    unique = []
    for row in combined:
        key = row["datetime"]
        if key not in seen:
            seen.add(key)
            unique.append(row)

    unique.sort(key=lambda r: r["datetime"])

    # ── Save ────────────────────────────────────────────
    save_csv(unique, csv_path)

    # Also save JSON
    json_path = OUTPUT_DIR / f"nifty_1min_{today_str}.json"
    with open(json_path, "w") as f:
        json.dump(unique, f, indent=2)

    # ── Summary ─────────────────────────────────────────
    print(f"\n{'═'*55}")
    print(f"  ✅ Done!")
    print(f"  Total candles : {len(unique):,}")
    print(f"  Days OK       : {ok_days + len(already_done)}")
    print(f"  Days failed   : {fail_days}")
    print(f"  CSV           : {csv_path}")
    print(f"{'═'*55}")
    print(f"\nRun backtest with real data:")
    print(f"  python backtest.py --file {csv_path} --start 2026-03-01 --end 2026-04-10\n")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        # Already ran probe — this shouldn't be called again
        print("Probe already complete. Run without --probe to download data.")
    else:
        asyncio.run(main())
