"""
Lords Bot — Nifty Data Downloader
====================================================
Downloads Nifty 50 data using Samco API.

API REALITY (discovered via debug):
  get_index_candle_data(index_name, from_date)
    → Returns DAILY OHLC from from_date to today
    → Works for ANY historical date
    → One row per trading day

  get_index_intraday_candle_data(index_name, from_date, to_date)
    → Returns 1-MIN candles
    → Works for TODAY only (recent session)

USAGE:
------
  # Interactive menu
  python download_nifty_data.py

  # Daily OHLC — any date range
  python download_nifty_data.py --mode daily --start 2020-01-01
  python download_nifty_data.py --mode daily --start 2020-01-01 --end 2024-12-31

  # Today's 1-min (append to growing dataset)
  python download_nifty_data.py --mode intraday
"""

import asyncio
import argparse
import csv
import sys
import time as time_module
from datetime import datetime, timedelta, date
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from backend.app.broker.samco_client import SamcoClient

OUTPUT_DIR = ROOT / "data"


def parse_date(s: str) -> date:
    for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"]:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse '{s}' — use YYYY-MM-DD")


def save_csv(rows: list[dict], filepath: Path, fieldnames: list[str]):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ── MODE 1: DAILY OHLC ──────────────────────────────────
async def download_daily(start_date: date, end_date: date):
    """
    Download daily OHLC for any date range.
    Uses get_index_candle_data — proven to work for all historical dates.
    """
    client = SamcoClient()
    print("\n  Logging into Samco...")
    await client.login()
    print("  ✅ Login successful\n")

    # API takes only from_date — returns from that date to today
    # We filter to our requested range afterward
    start_str = start_date.strftime("%Y-%m-%d")

    print(f"  Fetching Nifty daily OHLC from {start_str} → {end_date} ...")

    result   = await asyncio.to_thread(
        client.samco.get_index_candle_data,
        index_name="NIFTY 50",
        from_date=start_str,
    )
    response = SamcoClient._parse_response(result)

    if response.get("status") != "Success":
        msg = response.get("statusMessage") or response.get("message") or str(response)
        print(f"  ❌ API error: {msg}")
        return

    raw_rows = response.get("indexCandleData") or []

    # Parse + filter to requested range
    rows = []
    for item in raw_rows:
        try:
            d = date.fromisoformat(str(item.get("date", "")))
            if start_date <= d <= end_date:
                rows.append({
                    "date":   str(d),
                    "open":   float(item.get("open",   0)),
                    "high":   float(item.get("high",   0)),
                    "low":    float(item.get("low",    0)),
                    "close":  float(item.get("close",  0)),
                    "volume": float(item.get("volume", 0)),
                })
        except (ValueError, TypeError):
            continue

    rows.sort(key=lambda r: r["date"])

    if not rows:
        print("  ❌ No data in requested range")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fname    = f"nifty_daily_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.csv"
    csv_path = OUTPUT_DIR / fname
    save_csv(rows, csv_path, ["date","open","high","low","close","volume"])

    print(f"\n{'═'*58}")
    print(f"  ✅ Daily OHLC downloaded!")
    print(f"  Trading days : {len(rows):,}")
    print(f"  Range        : {rows[0]['date']} → {rows[-1]['date']}")
    print(f"  File         : {fname}")
    print(f"{'═'*58}")
    print(f"\n  ⚠️  NOTE: Daily data = 1 row per day (open/high/low/close).")
    print(f"  ORB backtesting needs 1-min candles — use --mode intraday")
    print(f"  to capture today's 1-min data and build up over time.\n")


# ── MODE 2: TODAY'S INTRADAY 1-MIN ──────────────────────
async def download_intraday(append: bool = True):
    """
    Download today's 1-min candles and append to rolling dataset.
    Run this every trading day after 3:30 PM.
    """
    today    = date.today()
    date_str = today.strftime("%Y-%m-%d")

    client = SamcoClient()
    print("\n  Logging into Samco...")
    await client.login()
    print("  ✅ Login successful\n")

    print(f"  Fetching today's 1-min candles ({date_str}) ...")

    result   = await asyncio.to_thread(
        client.samco.get_index_intraday_candle_data,
        index_name="NIFTY 50",
        from_date=f"{date_str} 09:15:00",
        to_date=f"{date_str} 15:30:00",
    )
    response = SamcoClient._parse_response(result)

    if response.get("status") != "Success":
        msg = response.get("statusMessage") or response.get("message") or str(response)
        print(f"  ❌ API error: {msg}")
        print(f"  Note: Intraday 1-min only works during/after market hours (9:15–15:30)")
        return

    raw  = response.get("intradayCandleData") or []
    rows = []
    for item in raw:
        try:
            rows.append({
                "datetime": str(item.get("dateTime", "")),
                "open":     float(item.get("open",   0)),
                "high":     float(item.get("high",   0)),
                "low":      float(item.get("low",    0)),
                "close":    float(item.get("close",  0)),
                "volume":   float(item.get("volume", 0)),
            })
        except (ValueError, TypeError):
            continue

    if not rows:
        print("  ❌ No candles — market may not have started yet")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Save today's file
    today_path = OUTPUT_DIR / f"nifty_1min_{today.strftime('%Y%m%d')}.csv"
    save_csv(rows, today_path, ["datetime","open","high","low","close","volume"])

    new_added = len(rows)
    total_days = 1

    if append:
        # Merge into rolling dataset
        dataset_path = OUTPUT_DIR / "nifty_1min_dataset.csv"
        existing = []
        seen     = set()

        if dataset_path.exists():
            with open(dataset_path) as f:
                for row in csv.DictReader(f):
                    if row["datetime"] not in seen:
                        seen.add(row["datetime"])
                        existing.append(row)

        new_rows = [r for r in rows if r["datetime"] not in seen]
        new_added = len(new_rows)
        combined  = existing + new_rows
        combined.sort(key=lambda r: r["datetime"])

        save_csv(combined, dataset_path, ["datetime","open","high","low","close","volume"])

        all_dates  = sorted(set(
            datetime.fromisoformat(r["datetime"]).date() for r in combined
        ))
        total_days = len(all_dates)
        d_start    = all_dates[0] if all_dates else today
        d_end      = all_dates[-1] if all_dates else today

        print(f"\n{'═'*58}")
        print(f"  ✅ Today's 1-min data saved!")
        print(f"  Today's candles   : {len(rows)}")
        print(f"  New candles added : {new_added}")
        print(f"  Dataset total     : {len(combined):,} candles")
        print(f"  Dataset days      : {total_days} trading days")
        print(f"  Dataset range     : {d_start} → {d_end}")
        print(f"  Files saved       :")
        print(f"    data/nifty_1min_{today.strftime('%Y%m%d')}.csv  (today only)")
        print(f"    data/nifty_1min_dataset.csv  (full rolling dataset)")
        print(f"{'═'*58}")
        print(f"\n  Run backtest:")
        print(f"  python backtest_runner.py --file data\\nifty_1min_dataset.csv\n")
        print(f"  💡 Tip: Run this every day after 3:30 PM to grow your dataset.")
        print(f"         After 1 month = 22 days of 1-min data for backtesting.\n")
    else:
        print(f"\n{'═'*58}")
        print(f"  ✅ Today's 1-min data saved!")
        print(f"  Candles : {len(rows)}")
        print(f"  File    : nifty_1min_{today.strftime('%Y%m%d')}.csv")
        print(f"{'═'*58}\n")


def _dataset_info() -> str:
    p = OUTPUT_DIR / "nifty_1min_dataset.csv"
    if not p.exists():
        return "0 days (not started yet)"
    try:
        days = set()
        with open(p) as f:
            for row in csv.DictReader(f):
                try:
                    days.add(datetime.fromisoformat(row["datetime"]).date())
                except Exception:
                    pass
        if not days:
            return "0 days"
        return f"{len(days)} days ({min(days)} → {max(days)})"
    except Exception:
        return "unknown"


def interactive():
    print(f"\n{'═'*58}")
    print(f"  LORDS BOT — Nifty Data Downloader")
    print(f"{'═'*58}\n")
    print(f"  SAMCO API CAPABILITIES:")
    print(f"  • Daily OHLC   → any date from 2006 onwards ✅")
    print(f"  • 1-min data   → TODAY only (build up day by day)")
    print(f"\n  OPTIONS:\n")
    print(f"  1 → Download Daily OHLC (any date range)")
    print(f"      One row per trading day — use for long-term backtest")
    print(f"\n  2 → Download Today's 1-min candles + append to dataset")
    print(f"      Current dataset: {_dataset_info()}")
    print(f"      Run every day after 3:30 PM to build 1-min history")
    print(f"\n  3 → Download Today's 1-min candles only (no append)")

    choice = input("\n  Choice (1/2/3): ").strip()

    if choice == "1":
        print(f"\n  Enter date range (YYYY-MM-DD)")
        while True:
            try:
                start = parse_date(input("  FROM: ").strip())
                break
            except ValueError as e:
                print(f"  ❌ {e}")
        while True:
            try:
                raw = input("  TO (Enter = today): ").strip()
                end = date.today() if not raw else parse_date(raw)
                if end < start:
                    print("  ❌ End must be after start")
                    continue
                end = min(end, date.today())
                break
            except ValueError as e:
                print(f"  ❌ {e}")
        asyncio.run(download_daily(start, end))

    elif choice == "2":
        asyncio.run(download_intraday(append=True))

    elif choice == "3":
        asyncio.run(download_intraday(append=False))

    else:
        print("  ❌ Invalid — enter 1, 2, or 3")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Lords Bot — Nifty Data Downloader")
    parser.add_argument("--mode",   choices=["daily","intraday"], default=None)
    parser.add_argument("--start",  type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--end",    type=str, default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--no-append", action="store_true",
                        help="Don't append to dataset (intraday mode)")
    args = parser.parse_args()

    if args.mode == "daily":
        if not args.start:
            print("❌ --start required  e.g. --start 2023-01-01"); sys.exit(1)
        try:
            start = parse_date(args.start)
            end   = parse_date(args.end) if args.end else date.today()
        except ValueError as e:
            print(f"❌ {e}"); sys.exit(1)
        asyncio.run(download_daily(start, end))

    elif args.mode == "intraday":
        asyncio.run(download_intraday(append=not args.no_append))

    else:
        interactive()


if __name__ == "__main__":
    main()