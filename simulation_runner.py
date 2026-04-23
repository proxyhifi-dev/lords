import pandas as pd
from datetime import time
from math import log, sqrt, exp
from scipy.stats import norm
from pathlib import Path

# ===== CONFIG (from your file) =====
SL_PCT = 0.30
T2_PCT = 1.00
TRAIL_PCT = 0.20
MIN_PREM = 30
STEP = 50
SPREAD = 2
QTY = 65

ORB_MIN = 50
ORB_MAX = 150
BUFFER = 5

NO_ENTRY = time(13, 30)
SQ_OFF = time(15, 10)

MAX_TRADES = 3
SKIP_FIRST = True

DATA_FILE = Path("data/nifty_1min_20260423.csv")
OUTPUT_FILE = Path("simulation_results.csv")


# ===== PRICING =====
def get_iv(dte):
    if dte <= 1: return 0.07
    if dte <= 2: return 0.09
    if dte <= 3: return 0.11
    if dte <= 5: return 0.13
    return 0.15


def bsm(S, K, T, opt, iv):
    if T <= 0:
        return max(S - K, 0.05) if opt == "CE" else max(K - S, 0.05)

    d1 = (log(S / K) + (0.065 + 0.5 * iv ** 2) * T) / (iv * sqrt(T))
    d2 = d1 - iv * sqrt(T)

    if opt == "CE":
        return S * norm.cdf(d1) - K * exp(-0.065 * T) * norm.cdf(d2)
    return K * exp(-0.065 * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bsm_ask(S, K, T, opt, dte):
    return round(bsm(S, K, T, opt, get_iv(dte)) + SPREAD, 2)


def bsm_bid(S, K, T, opt, dte):
    return round(max(bsm(S, K, T, opt, get_iv(dte)) - SPREAD, 0.05), 2)


# ===== HELPERS =====
def atm(spot):
    return int(round(spot / STEP) * STEP)


def strike(spot, sig):
    a = atm(spot)
    return a + STEP if sig == "CALL" else a - STEP


# ===== CORE =====
def run(df: pd.DataFrame) -> pd.DataFrame:
    df["datetime"] = pd.to_datetime(df["datetime"])

    # EMA for trend
    df["ema"] = df["close"].ewm(span=20).mean()

    results = []

    for day in sorted(df["datetime"].dt.date.unique()):
        ddf = df[df["datetime"].dt.date == day]
        trades_taken = 0

        # ORB
        orb = ddf[(ddf["datetime"].dt.time >= time(9, 15)) &
                  (ddf["datetime"].dt.time < time(9, 30))]
        if orb.empty:
            continue

        high = orb["high"].max()
        low = orb["low"].min()
        orb_range = high - low

        if orb_range < ORB_MIN or orb_range > ORB_MAX:
            continue

        post = ddf[(ddf["datetime"].dt.time >= time(9, 30)) &
                   (ddf["datetime"].dt.time < NO_ENTRY)]

        for _, r in post.iterrows():

            if trades_taken >= MAX_TRADES:
                break

            # Skip first candle (09:30)
            if SKIP_FIRST and r["datetime"].time() == time(9, 30):
                continue

            signal = None

            if r["close"] > high + BUFFER:
                signal = "CALL"
            elif r["close"] < low - BUFFER:
                signal = "PUT"

            if not signal:
                continue

            entry_time = r["datetime"]
            entry_spot = r["close"]

            # Trend filter
            ema = r["ema"]
            if signal == "CALL" and entry_spot < ema:
                continue
            if signal == "PUT" and entry_spot > ema:
                continue

            k = strike(entry_spot, signal)
            opt = "CE" if signal == "CALL" else "PE"

            ep = bsm_ask(entry_spot, k, 3 / 365, opt, 3)

            if ep < MIN_PREM:
                continue

            sl = ep * (1 - SL_PCT)
            t2 = ep * (1 + T2_PCT)

            maxp = ep
            exit_price = ep
            reason = "EOD"

            for _, r2 in ddf[ddf["datetime"] > entry_time].iterrows():
                curr = bsm_bid(r2["close"], k, 3 / 365, opt, 3)

                maxp = max(maxp, curr)
                trail = maxp * (1 - TRAIL_PCT)

                if curr <= sl:
                    exit_price = sl
                    reason = "SL"
                    break

                if curr >= t2:
                    exit_price = t2
                    reason = "T2"
                    break

                if curr < trail:
                    exit_price = trail
                    reason = "TRAIL"
                    break

                if r2["datetime"].time() >= SQ_OFF:
                    exit_price = curr
                    reason = "EOD"
                    break

            pnl = round((exit_price - ep) * QTY, 2)

            results.append({
                "date": str(day),
                "signal": signal,
                "entry": ep,
                "exit": exit_price,
                "pnl": pnl,
                "reason": reason
            })

            trades_taken += 1
            break  # only 1 trade per signal cycle

    return pd.DataFrame(results)


# ===== RUN =====
if __name__ == "__main__":
    df = pd.read_csv(DATA_FILE)
    res = run(df)

    pd.set_option("display.max_rows", None)
    print(res.to_string(index=False))

    res.to_csv(OUTPUT_FILE, index=False)
    print(f"\nSaved → {OUTPUT_FILE}")

    total = res["pnl"].sum()
    print(f"\nTOTAL PNL: ₹{total:,.2f}")