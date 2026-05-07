"""
Lords Bot — Real NIFTY Spot + Optional Iron Condor Option Data Downloader
=======================================================================

Downloads:
1. Real NIFTY 50 1-min index candles
2. Optional NIFTY option historical candles for Iron Condor legs

Important:
- Spot candles usually work through Samco SDK.
- Option historical candles may return no data depending on Samco endpoint/symbol support.
- Default is spot-only to avoid wasting time on unavailable option candles.

Usage:
python download_nifty_data.py
python download_nifty_data.py --start 2025-11-12 --end 2026-05-04
python download_nifty_data.py --download-options
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except Exception:
    pass


ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from backend.app.broker.samco_client import SamcoClient, get_expiry_api
from backend.app.core.config_loader import get_settings


_SETTINGS = get_settings()


INDEX_NAME = "NIFTY 50"
UNDERLYING = "NIFTY"
EXCHANGE_NFO = "NFO"

DAY_START = "09:15:00"
DAY_END = "15:30:00"


def _entry_time_default() -> str:
    raw = str(getattr(_SETTINGS, "ic_entry_window_start", "09:30")).strip()
    return f"{raw}:00" if len(raw) == 5 else raw


ENTRY_TIME = _entry_time_default()

ROUNDING = int(getattr(_SETTINGS, "ic_strike_rounding", 50))
SHORT_DISTANCE = int(getattr(_SETTINGS, "ic_short_distance", 250))
WING_WIDTH = int(getattr(_SETTINGS, "ic_wing_width", 100))

RATE_LIMIT_SECONDS = 0.5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download NIFTY spot/options data from Samco")

    parser.add_argument("--start", default="2025-11-12")
    parser.add_argument("--end", default=str(date.today()))

    parser.add_argument("--download-options", action="store_true")
    parser.add_argument("--output-dir", default="data")

    parser.add_argument("--entry-time", default=ENTRY_TIME)
    parser.add_argument("--short-distance", type=int, default=SHORT_DISTANCE)
    parser.add_argument("--wing-width", type=int, default=WING_WIDTH)
    parser.add_argument("--rounding", type=int, default=ROUNDING)

    return parser.parse_args()


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def trading_days(start: date, end: date) -> Iterable[date]:
    current = start

    while current <= end:
        if current.weekday() < 5:
            yield current

        current += timedelta(days=1)


def _first_non_empty(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default

        return float(str(value).replace(",", "").strip())

    except Exception:
        return default


def pd_to_datetime(value: str) -> datetime:
    value = str(value).strip().replace(".0", "")

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except Exception:
            continue

    return datetime.fromisoformat(value)


def normalize_datetime(value: Any, fallback_day: date | None = None) -> str:
    if value is None:
        return ""

    text = str(value).strip()

    if not text:
        return ""

    try:
        dt = pd_to_datetime(text)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass

    if fallback_day:
        return f"{fallback_day} {text}"

    return text


def parse_candles(
    response: dict[str, Any] | list[Any],
    fallback_day: date | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    if isinstance(response, list):
        data = response
    elif isinstance(response, dict):
        data = _first_non_empty(
            response.get("data"),
            response.get("intradayCandleData"),
            response.get("indexCandleData"),
            response.get("historicalCandleData"),
            response.get("candles"),
            response.get("result"),
        )
    else:
        data = []

    if not isinstance(data, list):
        return rows

    for item in data:
        try:
            if isinstance(item, (list, tuple)):
                dt = str(item[0]) if len(item) > 0 else ""
                op = _to_float(item[1]) if len(item) > 1 else 0.0
                hi = _to_float(item[2]) if len(item) > 2 else 0.0
                lo = _to_float(item[3]) if len(item) > 3 else 0.0
                cl = _to_float(item[4]) if len(item) > 4 else 0.0
                vol = _to_float(item[5]) if len(item) > 5 else 0.0

                if cl > 0:
                    rows.append(
                        {
                            "datetime": normalize_datetime(dt, fallback_day),
                            "open": op,
                            "high": hi,
                            "low": lo,
                            "close": cl,
                            "volume": vol,
                        }
                    )

                continue

            if isinstance(item, dict):
                dt = _first_non_empty(
                    item.get("dateTime"),
                    item.get("datetime"),
                    item.get("timestamp"),
                    item.get("time"),
                    item.get("date"),
                )

                op = _to_float(item.get("open"))
                hi = _to_float(item.get("high"))
                lo = _to_float(item.get("low"))
                cl = _to_float(
                    _first_non_empty(
                        item.get("close"),
                        item.get("ltp"),
                        item.get("lastTradedPrice"),
                    )
                )
                vol = _to_float(item.get("volume"))

                if cl > 0:
                    rows.append(
                        {
                            "datetime": normalize_datetime(dt, fallback_day),
                            "open": op,
                            "high": hi,
                            "low": lo,
                            "close": cl,
                            "volume": vol,
                        }
                    )

        except Exception:
            continue

    rows = [r for r in rows if r.get("datetime") and r.get("close", 0) > 0]
    rows.sort(key=lambda r: r["datetime"])

    return rows


def save_csv(rows: list[dict[str, Any]], filepath: Path) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        return

    with filepath.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def save_json(rows: list[dict[str, Any]], filepath: Path) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)

    with filepath.open("w", encoding="utf-8") as file:
        json.dump(rows, file, indent=2)


def load_existing_rows(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.exists():
        return []

    rows: list[dict[str, Any]] = []

    try:
        with csv_path.open("r", encoding="utf-8") as file:
            reader = csv.DictReader(file)

            for row in reader:
                rows.append(
                    {
                        "datetime": row.get("datetime", ""),
                        "open": _to_float(row.get("open")),
                        "high": _to_float(row.get("high")),
                        "low": _to_float(row.get("low")),
                        "close": _to_float(row.get("close")),
                        "volume": _to_float(row.get("volume")),
                    }
                )

    except Exception:
        return []

    return rows


def ensure_real_samco_sdk_available() -> None:
    try:
        from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "\nReal data download requires Samco official Python SDK: snapi_py_client.\n\n"
            "Fix:\n"
            "1. Install SDK package:\n"
            "   pip install stocknotebridge\n"
            "2. Verify:\n"
            "   python -c \"from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge; print('SDK OK')\"\n"
            "3. Put real Samco credentials and today's access token in .env.\n"
        ) from exc


def get_entry_spot(
    candles: list[dict[str, Any]],
    entry_time: str = ENTRY_TIME,
) -> float | None:
    target = entry_time[:5]

    for row in candles:
        dt_text = str(row["datetime"])
        hhmm = dt_text[11:16] if len(dt_text) >= 16 else ""

        if hhmm >= target:
            close = _to_float(row.get("close"))
            return close if close > 0 else None

    return None


def ceil_to_step(value: float, step: int = ROUNDING) -> int:
    return int(math.ceil(value / step) * step)


def floor_to_step(value: float, step: int = ROUNDING) -> int:
    return int(math.floor(value / step) * step)


def calculate_ic_strikes(
    spot: float,
    short_distance: int,
    wing_width: int,
    rounding: int,
) -> dict[str, int]:
    atm_up = ceil_to_step(spot, rounding)
    atm_down = floor_to_step(spot, rounding)

    short_call = atm_up + short_distance
    short_put = atm_down - short_distance
    long_call = short_call + wing_width
    long_put = short_put - wing_width

    return {
        "short_call": short_call,
        "long_call": long_call,
        "short_put": short_put,
        "long_put": long_put,
    }


def expiry_for_date(trade_date: date) -> date:
    try:
        expiry_str = get_expiry_api(trade_date)
        return datetime.strptime(expiry_str, "%Y-%m-%d").date()
    except Exception:
        target_weekday = 1 if trade_date >= date(2025, 9, 2) else 3
        days = (target_weekday - trade_date.weekday()) % 7
        return trade_date + timedelta(days=days)


def samco_option_symbol(expiry: date, strike: int, opt_type: str) -> str:
    yy = expiry.strftime("%y")
    mon = expiry.strftime("%b").upper()
    return f"{UNDERLYING}{yy}{mon}{strike}{opt_type}"


def build_ic_symbols(
    trade_date: date,
    strikes: dict[str, int],
) -> dict[str, dict[str, Any]]:
    expiry = expiry_for_date(trade_date)

    return {
        "short_call": {
            "symbol": samco_option_symbol(expiry, strikes["short_call"], "CE"),
            "strike": strikes["short_call"],
            "option_type": "CE",
            "side": "SELL",
            "expiry": str(expiry),
        },
        "long_call": {
            "symbol": samco_option_symbol(expiry, strikes["long_call"], "CE"),
            "strike": strikes["long_call"],
            "option_type": "CE",
            "side": "BUY",
            "expiry": str(expiry),
        },
        "short_put": {
            "symbol": samco_option_symbol(expiry, strikes["short_put"], "PE"),
            "strike": strikes["short_put"],
            "option_type": "PE",
            "side": "SELL",
            "expiry": str(expiry),
        },
        "long_put": {
            "symbol": samco_option_symbol(expiry, strikes["long_put"], "PE"),
            "strike": strikes["long_put"],
            "option_type": "PE",
            "side": "BUY",
            "expiry": str(expiry),
        },
    }


async def download_spot_day(
    client: SamcoClient,
    trade_date: date,
) -> tuple[list[dict[str, Any]], str]:
    from_dt = f"{trade_date} {DAY_START}"
    to_dt = f"{trade_date} {DAY_END}"

    try:
        if hasattr(client, "get_index_intraday_candles"):
            response = await client.get_index_intraday_candles(
                index_name=INDEX_NAME,
                from_date=from_dt,
                to_date=to_dt,
            )
        else:
            bridge = client._get_bridge()
            raw = await asyncio.to_thread(
                bridge.get_index_intraday_candle_data,
                index_name=INDEX_NAME,
                from_date=from_dt,
                to_date=to_dt,
            )
            response = SamcoClient._parse_response(raw)

        if isinstance(response, dict) and response.get("status") not in (None, "Success"):
            err = _first_non_empty(
                response.get("statusMessage"),
                response.get("message"),
                response.get("validationErrors"),
                response,
            )
            return [], str(err)

        candles = parse_candles(response, fallback_day=trade_date)
        return candles, "" if candles else "No spot candles parsed"

    except Exception as exc:
        return [], str(exc)


async def download_option_day(
    client: SamcoClient,
    trade_date: date,
    leg_name: str,
    symbol_meta: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    from_dt = f"{trade_date} {DAY_START}"
    to_dt = f"{trade_date} {DAY_END}"
    symbol = symbol_meta["symbol"]

    try:
        response = {}

        if hasattr(client, "get_intraday_candles"):
            response = await client.get_intraday_candles(
                symbol_name=symbol,
                exchange=EXCHANGE_NFO,
                from_date=from_dt,
                to_date=to_dt,
            )
        else:
            bridge = client._get_bridge()

            if hasattr(bridge, "get_intraday_candle_data"):
                raw = await asyncio.to_thread(
                    bridge.get_intraday_candle_data,
                    symbol_name=symbol,
                    exchange=EXCHANGE_NFO,
                    from_date=from_dt,
                    to_date=to_dt,
                )
                response = SamcoClient._parse_response(raw)

        candles = parse_candles(response, fallback_day=trade_date)

        if not candles:
            bridge = client._get_bridge()

            if hasattr(bridge, "get_historical_candle_data"):
                raw = await asyncio.to_thread(
                    bridge.get_historical_candle_data,
                    symbol_name=symbol,
                    exchange=EXCHANGE_NFO,
                    from_date=str(trade_date),
                    to_date=str(trade_date),
                )
                response = SamcoClient._parse_response(raw)
                candles = parse_candles(response, fallback_day=trade_date)

        if not candles:
            return [], f"No option candles parsed for {symbol}"

        enriched = []

        for row in candles:
            enriched.append(
                {
                    **row,
                    "symbol": symbol,
                    "leg_name": leg_name,
                    "strike": symbol_meta["strike"],
                    "option_type": symbol_meta["option_type"],
                    "side": symbol_meta["side"],
                    "expiry": symbol_meta["expiry"],
                }
            )

        return enriched, ""

    except Exception as exc:
        return [], str(exc)


async def main() -> None:
    args = parse_args()

    start_date = parse_date(args.start)
    end_date = parse_date(args.end)

    output_dir = Path(args.output_dir)
    options_dir = output_dir / "options"

    try:
        ensure_real_samco_sdk_available()
    except RuntimeError as exc:
        print(str(exc))
        return

    days = list(trading_days(start_date, end_date))

    print("\n" + "=" * 72)
    print("LORDS BOT — Real NIFTY Spot Downloader")
    print(f"Range          : {start_date} -> {end_date}")
    print(f"Trading days   : {len(days)}")
    print(f"Index          : {INDEX_NAME}")
    print(f"Options        : {'ON' if args.download_options else 'OFF'}")
    print("=" * 72 + "\n")

    output_dir.mkdir(parents=True, exist_ok=True)
    options_dir.mkdir(parents=True, exist_ok=True)

    client = SamcoClient()

    print("Logging into Samco...")

    try:
        await client.login()
    except Exception as exc:
        print(f"LOGIN FAILED: {exc}")
        return

    print("Login successful\n")

    spot_csv_path = output_dir / f"nifty_1min_{end_date.strftime('%Y%m%d')}.csv"
    spot_json_path = output_dir / f"nifty_1min_{end_date.strftime('%Y%m%d')}.json"

    existing_spot_rows = load_existing_rows(spot_csv_path)
    existing_keys = {
        row["datetime"]
        for row in existing_spot_rows
        if row.get("datetime")
    }

    all_spot_rows = list(existing_spot_rows)

    ok_spot_days = 0
    fail_spot_days = 0
    ok_option_days = 0
    fail_option_days = 0

    for idx, trade_date in enumerate(days, 1):
        print(f"[{idx}/{len(days)}] {trade_date}")

        already_present = any(
            str(row.get("datetime", "")).startswith(str(trade_date))
            for row in all_spot_rows
        )

        if already_present:
            day_spot_rows = [
                row
                for row in all_spot_rows
                if str(row.get("datetime", "")).startswith(str(trade_date))
            ]
            print(f"  Spot: already present ({len(day_spot_rows)} candles)")
        else:
            day_spot_rows, spot_err = await download_spot_day(client, trade_date)

            if day_spot_rows:
                new_count = 0

                for row in day_spot_rows:
                    key = row["datetime"]

                    if key not in existing_keys:
                        existing_keys.add(key)
                        all_spot_rows.append(row)
                        new_count += 1

                ok_spot_days += 1
                print(f"  Spot: {new_count} candles OK")
            else:
                fail_spot_days += 1
                print("  Spot: no data")

                if spot_err:
                    print(f"    Reason: {spot_err}")

                await asyncio.sleep(RATE_LIMIT_SECONDS)
                continue

        if not args.download_options:
            print("  Options: skipped. Use --download-options to try option candles.\n")
            await asyncio.sleep(RATE_LIMIT_SECONDS)
            continue

        entry_spot = get_entry_spot(day_spot_rows, args.entry_time)

        if not entry_spot:
            print("  Options: skipped, no entry spot\n")
            await asyncio.sleep(RATE_LIMIT_SECONDS)
            continue

        print(f"  Entry spot {args.entry_time}: {entry_spot}")

        strikes = calculate_ic_strikes(
            spot=entry_spot,
            short_distance=args.short_distance,
            wing_width=args.wing_width,
            rounding=args.rounding,
        )

        symbols = build_ic_symbols(trade_date, strikes)

        day_option_dir = options_dir / str(trade_date)
        day_option_dir.mkdir(parents=True, exist_ok=True)

        all_legs_ok = True

        for leg_name, meta in symbols.items():
            out_path = day_option_dir / f"{leg_name}.csv"

            if out_path.exists() and out_path.stat().st_size > 0:
                print(f"  {leg_name}: already present -> {meta['symbol']}")
                continue

            print(f"  {leg_name}: downloading {meta['symbol']}")

            rows, err = await download_option_day(client, trade_date, leg_name, meta)

            if rows:
                save_csv(rows, out_path)
                save_json(rows, day_option_dir / f"{leg_name}.json")
                print(f"    OK: {len(rows)} candles")
            else:
                all_legs_ok = False
                print(f"    NO DATA: {err}")

            await asyncio.sleep(RATE_LIMIT_SECONDS)

        if all_legs_ok:
            ok_option_days += 1
        else:
            fail_option_days += 1

        print("")

    if all_spot_rows:
        all_spot_rows.sort(key=lambda row: row["datetime"])
        save_csv(all_spot_rows, spot_csv_path)
        save_json(all_spot_rows, spot_json_path)

    print("\n" + "=" * 72)
    print("DONE")
    print(f"Spot candles     : {len(all_spot_rows):,}")
    print(f"Spot OK days     : {ok_spot_days}")
    print(f"Spot failed days : {fail_spot_days}")
    print(f"Option OK days   : {ok_option_days}")
    print(f"Option failed    : {fail_option_days}")
    print(f"Spot CSV         : {spot_csv_path}")
    print(f"Options folder   : {options_dir}")
    print("=" * 72)

    print("\nNext:")
    print(f"python backtest_runner.py --file {spot_csv_path}")
    print("(strategy params come from .env automatically — override with CLI flags if needed)")


if __name__ == "__main__":
    asyncio.run(main())
