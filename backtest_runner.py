"""
Lords Bot — Real Spot + Realistic Synthetic Iron Condor Backtest

Uses:
- Real NIFTY spot 1-min CSV
- Realistic synthetic option pricing engine
- Trend/gamma/IV/spread/slippage risk

Expected healthy result:
- NOT 100% win
- NOT 100% stop-loss
- Mix of EOD, TARGET, STOP_LOSS, CALL_BREACH, PUT_BREACH
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from option_pricing_engine import estimate_dynamic_entry_credit, estimate_ic_debit


@dataclass
class Trade:
    date: str
    entry_time: str
    exit_time: str
    entry_spot: float
    exit_spot: float
    short_call: int
    short_put: int
    long_call: int
    long_put: int
    entry_credit: float
    exit_debit: float
    gross_pnl: float
    charges: float
    net_pnl: float
    exit_reason: str
    day_high_after_entry: float
    day_low_after_entry: float
    max_move_pct: float


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Realistic Iron Condor backtest")

    p.add_argument("--file", default="data/nifty_1min_20260504.csv")
    p.add_argument("--capital", type=float, default=50000)
    p.add_argument("--lot-size", type=int, default=50)

    # Compatibility args. These are accepted so old commands do not fail.
    p.add_argument("--strategy", default="iron_condor")
    p.add_argument("--frequency", choices=["daily", "monthly"], default="daily")

    p.add_argument(
        "--mode",
        choices=["conservative", "balanced", "realistic", "aggressive"],
        default="realistic",
    )

    p.add_argument("--entry-time", default="09:20")
    p.add_argument("--exit-time", default="15:20")

    # Calibrated safe defaults.
    p.add_argument("--short-distance", type=int, default=350)
    p.add_argument("--wing-width", type=int, default=300)
    p.add_argument("--rounding", type=int, default=50)
    p.add_argument("--target-pct", type=float, default=0.30)
    p.add_argument("--stop-loss-mult", type=float, default=1.80)

    # Filters.
    p.add_argument("--skip-gap-pct", type=float, default=0.012)
    p.add_argument("--skip-open-range-pct", type=float, default=0.012)

    p.add_argument("--output", default="data/backtest_results.csv")
    p.add_argument("--summary-output", default="data/backtest_summary.json")

    return p.parse_args()


def normalize_data(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]

    if "datetime" not in df.columns:
        if "date" in df.columns and "time" in df.columns:
            df["datetime"] = df["date"].astype(str) + " " + df["time"].astype(str)
        else:
            raise ValueError("CSV must contain datetime or date+time columns")

    price_col = None
    for c in ["close", "ltp", "price", "last"]:
        if c in df.columns:
            price_col = c
            break

    if price_col is None:
        raise ValueError("CSV must contain close/ltp/price column")

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df["close"] = pd.to_numeric(df[price_col], errors="coerce")

    for c in ["open", "high", "low"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(df["close"])
        else:
            df[c] = df["close"]

    df = df.dropna(subset=["datetime", "close"])
    df = df[df["close"] > 0]
    df = df.sort_values("datetime")

    df["date"] = df["datetime"].dt.date
    df["hhmm"] = df["datetime"].dt.strftime("%H:%M")

    return df[["datetime", "date", "hhmm", "open", "high", "low", "close"]]


def ceil_to_step(x: float, step: int) -> int:
    return int(math.ceil(x / step) * step)


def floor_to_step(x: float, step: int) -> int:
    return int(math.floor(x / step) * step)


def calculate_strikes(
    spot: float,
    short_distance: int,
    wing_width: int,
    rounding: int,
) -> tuple[int, int, int, int]:
    atm_up = ceil_to_step(spot, rounding)
    atm_down = floor_to_step(spot, rounding)

    short_call = atm_up + short_distance
    short_put = atm_down - short_distance
    long_call = short_call + wing_width
    long_put = short_put - wing_width

    return short_call, short_put, long_call, long_put


def calculate_charges(entry_credit: float, exit_debit: float, qty: int) -> float:
    """
    Approx Indian options charges.
    Adjust these if your broker/charges differ.
    """

    turnover = (entry_credit + exit_debit) * qty

    brokerage = 100.0
    stt = exit_debit * qty * 0.00125
    exchange_txn = turnover * 0.00053
    sebi = turnover * 0.000001
    stamp = entry_credit * qty * 0.00003
    gst = (brokerage + exchange_txn) * 0.18

    return round(brokerage + stt + exchange_txn + sebi + stamp + gst, 2)


def should_skip_day(
    day_df: pd.DataFrame,
    prev_close: float | None,
    args: argparse.Namespace,
) -> tuple[bool, str]:
    if len(day_df) < 200:
        return True, "LOW_CANDLES"

    first_price = float(day_df.iloc[0]["close"])

    if prev_close and prev_close > 0:
        gap_pct = abs(first_price - prev_close) / prev_close
        if gap_pct > args.skip_gap_pct:
            return True, f"HIGH_GAP_{gap_pct:.2%}"

    first_30 = day_df[day_df["hhmm"] <= "09:45"]
    if not first_30.empty:
        open_range_pct = (first_30["high"].max() - first_30["low"].min()) / first_price
        if open_range_pct > args.skip_open_range_pct:
            return True, f"HIGH_OPEN_RANGE_{open_range_pct:.2%}"

    return False, ""


def get_entry_row(day_df: pd.DataFrame, entry_time: str):
    rows = day_df[day_df["hhmm"] >= entry_time]
    if rows.empty:
        return None
    return rows.iloc[0]


def run_day(
    day_df: pd.DataFrame,
    prev_close: float | None,
    args: argparse.Namespace,
) -> Trade | None:
    skip, _ = should_skip_day(day_df, prev_close, args)
    if skip:
        return None

    entry_row = get_entry_row(day_df, args.entry_time)
    if entry_row is None:
        return None

    entry_dt = entry_row["datetime"]
    entry_spot = float(entry_row["close"])

    short_call, short_put, long_call, long_put = calculate_strikes(
        entry_spot,
        short_distance=args.short_distance,
        wing_width=args.wing_width,
        rounding=args.rounding,
    )

    entry_credit = estimate_dynamic_entry_credit(
        entry_spot=entry_spot,
        short_call=short_call,
        short_put=short_put,
        wing_width=args.wing_width,
        mode=args.mode,
    )

    if entry_credit <= 0:
        return None

    after_entry = day_df[day_df["datetime"] > entry_dt].copy()
    if after_entry.empty:
        return None

    exit_row = after_entry.iloc[-1]
    exit_reason = "EOD"
    exit_debit = entry_credit

    high_so_far = entry_spot
    low_so_far = entry_spot
    max_move_pct = 0.0

    for _, row in after_entry.iterrows():
        now_dt = row["datetime"]
        now_hhmm = row["hhmm"]
        spot = float(row["close"])

        high_so_far = max(high_so_far, float(row["high"]), spot)
        low_so_far = min(low_so_far, float(row["low"]), spot)

        minutes = (now_dt - entry_dt).total_seconds() / 60.0
        max_move_pct = max(max_move_pct, abs(spot - entry_spot) / entry_spot)

        current_debit = estimate_ic_debit(
            entry_credit=entry_credit,
            entry_spot=entry_spot,
            spot=spot,
            sc=short_call,
            sp=short_put,
            minutes=minutes,
            mode=args.mode,
            day_high=high_so_far,
            day_low=low_so_far,
        )

        profit_per_unit = entry_credit - current_debit

        if spot >= short_call:
            exit_row = row
            exit_debit = max(current_debit, entry_credit * args.stop_loss_mult)
            exit_reason = "CALL_BREACH"
            break

        if spot <= short_put:
            exit_row = row
            exit_debit = max(current_debit, entry_credit * args.stop_loss_mult)
            exit_reason = "PUT_BREACH"
            break

        if current_debit >= entry_credit * args.stop_loss_mult:
            exit_row = row
            exit_debit = current_debit
            exit_reason = "STOP_LOSS"
            break

        if profit_per_unit >= entry_credit * args.target_pct:
            exit_row = row
            exit_debit = current_debit
            exit_reason = "TARGET"
            break

        if now_hhmm >= args.exit_time:
            exit_row = row
            exit_debit = current_debit
            exit_reason = "EOD"
            break

    exit_spot = float(exit_row["close"])
    gross = (entry_credit - exit_debit) * args.lot_size
    charges = calculate_charges(entry_credit, exit_debit, args.lot_size)
    net = gross - charges

    return Trade(
        date=str(entry_dt.date()),
        entry_time=str(entry_dt),
        exit_time=str(exit_row["datetime"]),
        entry_spot=round(entry_spot, 2),
        exit_spot=round(exit_spot, 2),
        short_call=short_call,
        short_put=short_put,
        long_call=long_call,
        long_put=long_put,
        entry_credit=round(entry_credit, 2),
        exit_debit=round(exit_debit, 2),
        gross_pnl=round(gross, 2),
        charges=round(charges, 2),
        net_pnl=round(net, 2),
        exit_reason=exit_reason,
        day_high_after_entry=round(high_so_far, 2),
        day_low_after_entry=round(low_so_far, 2),
        max_move_pct=round(max_move_pct * 100, 3),
    )


def max_drawdown(equity_values: list[float]) -> float:
    peak = -10**18
    max_dd = 0.0

    for value in equity_values:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)

    return round(max_dd, 2)


def summarize(trades: list[Trade], args: argparse.Namespace) -> dict:
    if not trades:
        return {"total_trades": 0}

    pnls = [t.net_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    equity = []
    current_equity = args.capital

    for pnl in pnls:
        current_equity += pnl
        equity.append(current_equity)

    start = pd.to_datetime(trades[0].date)
    end = pd.to_datetime(trades[-1].date)
    months = max((end - start).days / 30.0, 1 / 30)

    exit_reasons = {}
    for trade in trades:
        exit_reasons[trade.exit_reason] = exit_reasons.get(trade.exit_reason, 0) + 1

    gross_total = sum(t.gross_pnl for t in trades)
    charge_total = sum(t.charges for t in trades)
    net_total = sum(t.net_pnl for t in trades)

    if losses and abs(sum(losses)) > 0:
        profit_factor = round(sum(wins) / abs(sum(losses)), 3)
    else:
        profit_factor = "INF"

    return {
        "capital": args.capital,
        "mode": args.mode,
        "frequency": args.frequency,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2),
        "gross_pnl": round(gross_total, 2),
        "charges": round(charge_total, 2),
        "net_pnl": round(net_total, 2),
        "return_pct": round(net_total / args.capital * 100, 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "max_win": round(max(pnls), 2),
        "max_loss": round(min(pnls), 2),
        "profit_factor": profit_factor,
        "expectancy": round(net_total / len(trades), 2),
        "max_drawdown": max_drawdown(equity),
        "monthly_expected": round(net_total / months, 2),
        "monthly_return_pct": round((net_total / months) / args.capital * 100, 2),
        "exit_reasons": exit_reasons,
    }


def print_results(trades: list[Trade], summary: dict) -> None:
    print("\n" + "=" * 132)
    print("REAL SPOT + REALISTIC SYNTHETIC OPTION IC BACKTEST")
    print("=" * 132)

    if not trades:
        print("No trades generated.")
        return

    print(
        f"{'Date':<12} {'Entry':<6} {'Exit':<6} {'Spot':>9} {'SC':>7} {'SP':>7} "
        f"{'Credit':>8} {'Debit':>8} {'NetPnL':>10} {'Reason':<12}"
    )
    print("-" * 132)

    for trade in trades:
        print(
            f"{trade.date:<12} {trade.entry_time[11:16]:<6} {trade.exit_time[11:16]:<6} "
            f"{trade.entry_spot:>9.2f} {trade.short_call:>7} {trade.short_put:>7} "
            f"{trade.entry_credit:>8.2f} {trade.exit_debit:>8.2f} "
            f"{trade.net_pnl:>10.2f} {trade.exit_reason:<12}"
        )

    print("\nSUMMARY")
    print("-" * 80)

    for key, value in summary.items():
        if key != "exit_reasons":
            print(f"{key:<22}: {value}")

    print("\nEXIT REASONS")
    print("-" * 80)

    for reason, count in summary["exit_reasons"].items():
        print(f"{reason:<16}: {count}")

    print("\nREALISM CHECK")
    print("-" * 80)

    if summary["win_rate_pct"] >= 90:
        print("WARNING: Win rate still too high. Increase risk or use real option data.")

    if summary["losses"] == 0:
        print("WARNING: No losses. Model is still too smooth.")

    if summary["win_rate_pct"] <= 10:
        print("WARNING: Win rate too low. Model or stop loss is too harsh.")

    if summary["monthly_return_pct"] > 12:
        print("WARNING: Monthly return too high. Treat as optimistic.")

    if summary["monthly_return_pct"] < -15:
        print("WARNING: Monthly loss too high. Parameters are too risky/harsh.")

    print("NOTE: Uses real NIFTY spot candles, synthetic option premium model.")


def save_outputs(trades: list[Trade], summary: dict, args: argparse.Namespace) -> None:
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    pd.DataFrame([asdict(t) for t in trades]).to_csv(output_path, index=False)

    summary_path = Path(args.summary_output)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    with summary_path.open("w", encoding="utf-8") as file:
        json.dump(summary, file, indent=2, default=str)

    print(f"\nSaved trades : {output_path}")
    print(f"Saved summary: {summary_path}")


def main() -> None:
    args = parse_args()

    file_path = Path(args.file)

    if not file_path.exists():
        fallback = Path("data/nifty_1min_20260501.csv")
        if fallback.exists():
            print(f"WARNING: {file_path} not found. Using fallback: {fallback}")
            file_path = fallback
        else:
            raise FileNotFoundError(f"Missing file: {file_path}")

    raw = pd.read_csv(file_path)
    df = normalize_data(raw)

    trades: list[Trade] = []
    prev_close: float | None = None
    traded_months: set[str] = set()

    for trade_date, day_df in df.groupby("date"):
        day_df = day_df.sort_values("datetime").reset_index(drop=True)

        month_key = str(trade_date)[:7]

        if args.frequency == "monthly" and month_key in traded_months:
            prev_close = float(day_df.iloc[-1]["close"])
            continue

        trade = run_day(day_df, prev_close, args)

        if trade:
            trades.append(trade)
            traded_months.add(month_key)

        prev_close = float(day_df.iloc[-1]["close"])

    summary = summarize(trades, args)
    print_results(trades, summary)
    save_outputs(trades, summary, args)


if __name__ == "__main__":
    main()