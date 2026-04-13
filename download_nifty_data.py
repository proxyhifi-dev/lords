"""
Lords Bot — Nifty 1-Min Historical Data Downloader
====================================================
Downloads 1-minute OHLC candles day-by-day using your existing SamcoClient bridge.

What this script does:
- Logs into Samco once
- Uses the live SDK bridge from SamcoClient
- Tries intraday 1-min endpoint for each trading day
- Parses multiple possible response shapes
- Saves combined CSV + JSON into /data

Notes:
- If Samco returns "No Records Found" for a day, that day is skipped.
- This script is written to be tolerant of different SDK response shapes.
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

# project root
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from backend.app.broker.samco_client import SamcoClient

# --------------------------------------------------
# CONFIG
# --------------------------------------------------
START_DATE = date(2025, 11, 12)
END_DATE = date.today()
OUTPUT_DIR = ROOT / "data"
RATE_LIMIT_SECONDS = 0.5

INDEX_NAME = "NIFTY 50"   # keep this as NIFTY 50 unless your SDK requires NIFTY


# --------------------------------------------------
# HELPERS
# --------------------------------------------------
def trading_days(start: date, end: date) -> Iterable[date]:
    """Yield weekdays only."""
    current = start
    while current <= end:
        if current.weekday() < 5:  # Mon-Fri
            yield current
        current += timedelta(days=1)


def _first_non_empty(*values):
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


def parse_candles(response: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Parse candle responses from different possible SAMCO shapes.
    Expected output:
      datetime, open, high, low, close, volume
    """
    rows: list[dict[str, Any]] = []

    data = _first_non_empty(
        response.get("data"),
        response.get("intradayCandleData"),
        response.get("indexCandleData"),
        response.get("candles"),
        response.get("result"),
    )

    if not isinstance(data, list):
        return rows

    for item in data:
        try:
            # Shape 1: list/tuple
            if isinstance(item, (list, tuple)):
                # Common formats:
                # [datetime, open, high, low, close, volume]
                # [dateTime, open, high, low, close]
                dt = str(item[0]) if len(item) > 0 else ""
                op = float(item[1]) if len(item) > 1 else 0.0
                hi = float(item[2]) if len(item) > 2 else 0.0
                lo = float(item[3]) if len(item) > 3 else 0.0
                cl = float(item[4]) if len(item) > 4 else 0.0
                vol = float(item[5]) if len(item) > 5 else 0.0

                rows.append(
                    {
                        "datetime": dt,
                        "open": op,
                        "high": hi,
                        "low": lo,
                        "close": cl,
                        "volume": vol,
                    }
                )
                continue

            # Shape 2: dict
            if isinstance(item, dict):
                dt = _first_non_empty(
                    item.get("dateTime"),
                    item.get("datetime"),
                    item.get("timestamp"),
                    item.get("time"),
                    item.get("date"),
                )

                rows.append(
                    {
                        "datetime": str(dt) if dt is not None else "",
                        "open": float(item.get("open", 0) or 0),
                        "high": float(item.get("high", 0) or 0),
                        "low": float(item.get("low", 0) or 0),
                        "close": float(item.get("close", 0) or 0),
                        "volume": float(item.get("volume", 0) or 0),
                    }
                )

        except (ValueError, TypeError, IndexError):
            continue

    return rows


def _normalize_sort_key(row: dict[str, Any]) -> str:
    return str(row.get("datetime", ""))


def save_csv(rows: list[dict[str, Any]], filepath: Path) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["datetime", "open", "high", "low", "close", "volume"],
        )
        writer.writeheader()
        writer.writerows(rows)


def load_existing_rows(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.exists():
        return []

    rows: list[dict[str, Any]] = []
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(
                    {
                        "datetime": row.get("datetime", ""),
                        "open": float(row.get("open", 0) or 0),
                        "high": float(row.get("high", 0) or 0),
                        "low": float(row.get("low", 0) or 0),
                        "close": float(row.get("close", 0) or 0),
                        "volume": float(row.get("volume", 0) or 0),
                    }
                )
    except Exception:
        return []

    return rows


# --------------------------------------------------
# DOWNLOAD ONE DAY
# --------------------------------------------------
async def download_day(bridge, trade_date: date) -> tuple[list[dict[str, Any]], str]:
    """
    Returns:
      (candles, error_message)

    If candles are available, error_message will be empty.
    """
    date_str = trade_date.strftime("%Y-%m-%d")

    try:
        # Primary call: intraday 1-min candles
        result = await asyncio.to_thread(
            bridge.get_index_intraday_candle_data,
            index_name=INDEX_NAME,
            from_date=f"{date_str} 09:15:00",
            to_date=f"{date_str} 15:30:00",
        )
        response = SamcoClient._parse_response(result)

        if response.get("status") == "Success":
            candles = parse_candles(response)
            if candles:
                return candles, ""

        # If API returns a structured error, preserve it
        err = _first_non_empty(
            response.get("message"),
            response.get("statusMessage"),
            response.get("validationErrors"),
            response,
        )
        return [], str(err)

    except Exception as exc:
        return [], str(exc)


# --------------------------------------------------
# MAIN
# --------------------------------------------------
async def main() -> None:
    days = list(trading_days(START_DATE, END_DATE))

    print("\n═══════════════════════════════════════════════════════")
    print("LORDS BOT — Nifty 1-Min Data Downloader")
    print(f"Range : {START_DATE} → {END_DATE}")
    print(f"Days  : {len(days)}")
    print("═══════════════════════════════════════════════════════\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    client = SamcoClient()

    print("Logging into Samco...")
    await client.login()
    print("Login successful\n")

    # Use the actual SDK bridge from SamcoClient
    bridge = client._get_bridge()

    # Resumable output file
    out_name = f"nifty_1min_{END_DATE.strftime('%Y%m%d')}.csv"
    csv_path = OUTPUT_DIR / out_name
    json_path = OUTPUT_DIR / out_name.replace(".csv", ".json")

    existing_rows = load_existing_rows(csv_path)
    existing_keys = {row["datetime"] for row in existing_rows}

    all_rows = list(existing_rows)
    ok_days = 0
    fail_days = 0

    for i, day in enumerate(days, 1):
        if day in {datetime.fromisoformat(k).date() for k in existing_keys if k}:
            print(f"[{i}/{len(days)}] {day} ... already present ✅")
            continue

        print(f"[{i}/{len(days)}] {day} ... ", end="", flush=True)

        candles, err = await download_day(bridge, day)

        if candles:
            new_count = 0
            for row in candles:
                key = row["datetime"]
                if key not in existing_keys:
                    existing_keys.add(key)
                    all_rows.append(row)
                    new_count += 1

            ok_days += 1
            print(f"{new_count} candles ✅")
        else:
            fail_days += 1
            print(f"no data ⚠️")
            if err:
                print(f"    ↳ {err}")

        await asyncio.sleep(RATE_LIMIT_SECONDS)

    if not all_rows:
        print("\n❌ No candles downloaded.")
        return

    all_rows.sort(key=_normalize_sort_key)

    save_csv(all_rows, csv_path)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, indent=2)

    print(f"\n{'═'*55}")
    print("✅ Done!")
    print(f"Total candles : {len(all_rows):,}")
    print(f"Days OK       : {ok_days}")
    print(f"Days failed   : {fail_days}")
    print(f"CSV           : {csv_path}")
    print(f"JSON          : {json_path}")
    print(f"{'═'*55}")

    print("\nRun backtest with real data:")
    print(f"  python backtest_runner.py --file {csv_path}")


if __name__ == "__main__":
    asyncio.run(main())