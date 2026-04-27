"""
Lords Bot — Backtest Runner  (Production Grade)
================================================
TWO MODES:

  MODE 1 — OFFLINE (default, any time, no login):
      1-min NIFTY spot CSV + DTE-calibrated IV + ₹2 spread.
      python backtest_runner.py

  MODE 2 — LIVE (today only, market hours):
      SAMCO option chain → real CE/PE LTP for TODAY's trades.
      Historical dates use DTE-calibrated BSM automatically.
      python backtest_runner.py --live

USAGE:
  python backtest_runner.py
  python backtest_runner.py --file data/nifty_1min_20260413.csv
  python backtest_runner.py --start 2026-03-01 --end 2026-04-13
  python backtest_runner.py --live

TRADE LOG shows:
  - Exact contract name  (NIFTY26APR23800CE)
  - NIFTY spot at entry  (23709)
  - BUY price per lot    (₹62.45)
  - SELL price per lot   (₹89.20)
  - Total BUY value      (₹4,059)
  - Total SELL value     (₹5,798)
  - Net P&L              (+₹1,739)

PRICING FIX (DTE-calibrated IV):
  OLD bug : BSM fixed IV=16% → DTE=3, ₹103  (real market = ₹25-40, 3x wrong!)
  NEW fix : DTE-based IV matched to real NSE option chain:
            DTE=1 → IV=7%    DTE=2 → IV=9%    DTE=3 → IV=11%
            DTE=4-5 → IV=13%  DTE≥7 → IV=15%
  + ₹2 spread on entry (ask), ₹2 on exit (bid)
"""

import sys
import asyncio
import argparse
from pathlib import Path
from datetime import datetime, date, time, timedelta
from math import log, sqrt, exp

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

try:
    import pandas as pd
    from scipy.stats import norm
except ImportError:
    print("\n❌  Run: pip install pandas scipy\n"); sys.exit(1)

# ── Load bot config ──────────────────────────────────────
try:
    from backend.app.core.config_loader import get_settings
    s = get_settings()
    SL_PCT    = getattr(s, "stop_loss_pct",        0.25)
    T1_PCT    = getattr(s, "t1_pct",               0.30)
    T2_PCT    = getattr(s, "t2_pct",               1.00)
    TRAIL_PCT = getattr(s, "trailing_pct",          0.15)
    MIN_PREM  = getattr(s, "min_entry_premium",     30.0)
    OTM_DIST  = getattr(s, "otm_distance",          1)
    ATR_MULT  = getattr(s, "orb_atr_multiplier",    1.0)
    BUF       = getattr(s, "breakout_buffer",       5.0)
    MIN_ORB   = getattr(s, "min_orb_range",         5.0)
    TREND_ON  = getattr(s, "trend_filter_enabled",  False)
    ORDER_QTY = getattr(s, "order_qty",             65)
    MAX_LOSS  = getattr(s, "max_daily_loss",        3000.0)
    NO_ENTRY  = time(*map(int, getattr(s, "no_entry_after", "13:30").split(":")))
    SQ_OFF    = time(*map(int, getattr(s, "square_off",     "15:10").split(":")))
    CFG_SOURCE = "✅ Loaded from config_loader.py"
except Exception as e:
    SL_PCT=0.25; T1_PCT=0.30; T2_PCT=1.00; TRAIL_PCT=0.15
    MIN_PREM=30.0; OTM_DIST=1; ATR_MULT=1.0; BUF=5.0
    MIN_ORB=5.0; TREND_ON=False; ORDER_QTY=65; MAX_LOSS=3000.0
    NO_ENTRY=time(13,30); SQ_OFF=time(15,10)
    CFG_SOURCE = f"⚠️  Using defaults ({e})"

STEP           = 50
SPREAD         = 2.0
API_CALL_DELAY = 0.3


# ═══════════════════════════════════════════════════════════
#  SECTION 1 — PRICING HELPERS
# ═══════════════════════════════════════════════════════════

def get_iv(dte: int) -> float:
    """DTE-calibrated IV matched to real NSE option chain prices."""
    if dte <= 1: return 0.07
    if dte <= 2: return 0.09
    if dte <= 3: return 0.11
    if dte <= 5: return 0.13
    if dte <= 7: return 0.15
    return 0.16


def bsm(S, K, T, opt="CE", r=0.065, iv=0.14) -> float:
    if T <= 0:
        return max(S - K, 0.05) if opt == "CE" else max(K - S, 0.05)
    d1 = (log(S / K) + (r + 0.5 * iv ** 2) * T) / (iv * sqrt(T))
    d2 = d1 - iv * sqrt(T)
    if opt == "CE":
        return max(S * norm.cdf(d1) - K * exp(-r * T) * norm.cdf(d2), 0.05)
    return max(K * exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1), 0.05)


def bsm_ask(S, K, T, opt, dte: int) -> float:
    """Entry: buy at ask = BSM + ₹2 spread."""
    return round(bsm(S, K, T, opt, iv=get_iv(dte)) + SPREAD, 2)


def bsm_bid(S, K, T, opt, dte: int) -> float:
    """Exit: sell at bid = BSM − ₹2 spread."""
    return round(max(bsm(S, K, T, opt, iv=get_iv(dte)) - SPREAD, 0.05), 2)


# ── NSE Expiry Day Change ────────────────────────────────
# Before Sep 2 2025  : NIFTY weekly expiry = THURSDAY
# From Sep 2 2025    : NIFTY weekly expiry = TUESDAY
#   (SEBI circular — standardised NSE to Tuesday, BSE to Thursday)
# Reference: NSE Circular 111/2025, effective Sep 2 2025
_EXPIRY_CHANGE_DATE = date(2025, 9, 2)


def next_expiry(dt: datetime) -> datetime:
    """
    Returns next NIFTY weekly expiry date.
      Before Sep 2 2025 : Thursday
      From Sep 2 2025   : Tuesday  ← NSE changed!

    Also skips current-week expiry if DTE would be < 2,
    because 1-DTE options have almost no premium for ORB strategy.
    (e.g. Monday entry → Tuesday DTE=1 → use next Tuesday DTE=8 instead)
    """
    if dt.date() >= _EXPIRY_CHANGE_DATE:
        target_weekday = 1   # Tuesday
    else:
        target_weekday = 3   # Thursday

    days_ahead = (target_weekday - dt.weekday()) % 7

    # If today IS the expiry day and market already closed → next week
    if days_ahead == 0 and dt.hour >= 15 and dt.minute >= 30:
        days_ahead = 7

    expiry = dt + timedelta(days=days_ahead)

    # If DTE < 2 (e.g. Monday before Tuesday expiry → only 1 day left),
    # skip to NEXT week. 1-DTE premiums are too small for ORB.
    if (expiry.date() - dt.date()).days < 2:
        expiry += timedelta(days=7)

    return expiry


def calc_atm(spot: float) -> int:
    return int(round(spot / STEP) * STEP)


def calc_strike(spot: float, sig: str) -> int:
    a = calc_atm(spot)
    return a + OTM_DIST * STEP if sig == "CALL" else a - OTM_DIST * STEP


def make_symbol(strike: int, ot: str, expiry: datetime) -> str:
    """NSE format: NIFTY26APR23800CE"""
    return f"NIFTY{expiry.strftime('%y')}{expiry.strftime('%b').upper()}{strike}{ot}"


def get_atr(day_df) -> float:
    orb = day_df[(day_df["datetime"].dt.time >= time(9, 15)) &
                 (day_df["datetime"].dt.time < time(9, 30))]
    if len(orb) < 2: return 15.0
    return max((orb["high"] - orb["low"]).mean(), 1.0)


# ═══════════════════════════════════════════════════════════
#  SECTION 2 — LIVE SAMCO OPTION PRICE FETCHER
# ═══════════════════════════════════════════════════════════

_samco_client = None
_chain_cache  = {}
_CHAIN_TTL    = 60


def _get_samco():
    global _samco_client
    if _samco_client is None:
        try:
            from backend.app.broker.samco_client import SamcoClient
            _samco_client = SamcoClient()
        except Exception as e:
            print(f"\n  ❌ Cannot import SamcoClient: {e}\n"); sys.exit(1)
    return _samco_client


async def _login_samco():
    client = _get_samco()
    print("  🔐 Logging in to SAMCO ...")
    await client.login()
    print("  ✅ SAMCO login successful")
    print("  ℹ️  SAMCO API: live prices for TODAY only.")
    print("       Historical dates → DTE-calibrated BSM automatically.\n")


async def _fetch_chain_safe(spot: float, expiry: datetime, ot: str) -> dict:
    import time as _time
    exp_str = expiry.strftime("%Y-%m-%d")
    key     = (exp_str, ot)
    cached  = _chain_cache.get(key)
    if cached and (_time.time() - cached["ts"]) < _CHAIN_TTL:
        return cached["data"]
    client = _get_samco()
    await asyncio.sleep(API_CALL_DELAY)
    try:
        resp = await asyncio.wait_for(
            client.get_option_chain(
                search_symbol_name="NIFTY", exchange="NFO",
                expiry_date=exp_str, strike_price=str(calc_atm(spot)),
                option_type=ot,
            ), timeout=5.0,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        return {}
    rows = resp.get("optionChainDetails") or resp.get("data") or []
    result = {}
    for row in rows:
        try:
            k   = int(float(row.get("strikePrice", 0)))
            ltp = float(str(row.get("lastTradedPrice", 0) or
                            row.get("ltp", 0) or 0).replace(",", ""))
            if ltp > 0: result[k] = ltp
        except (ValueError, TypeError): continue
    if result:
        _chain_cache[key] = {"ts": _time.time(), "data": result}
    return result


async def get_live_price(spot: float, sig: str, expiry: datetime) -> tuple:
    ot     = "CE" if sig == "CALL" else "PE"
    strike = calc_strike(spot, sig)
    symbol = make_symbol(strike, ot, expiry)
    chain  = await _fetch_chain_safe(spot, expiry, ot)
    if not chain:
        return None, strike, symbol
    for try_k in [strike, calc_atm(spot),
                  *sorted(chain.keys(), key=lambda k: abs(k - strike))]:
        if try_k in chain and chain[try_k] > 0:
            return round(chain[try_k], 2), try_k, make_symbol(try_k, ot, expiry)
    return None, strike, symbol


# ═══════════════════════════════════════════════════════════
#  SECTION 3 — CORE TRADE ENGINE (shared by both modes)
# ═══════════════════════════════════════════════════════════

def _simulate_trade(ddf, sig, ets, esp, strike, ot, expiry, dte, ep, day):
    """Run exit logic for one trade. Returns trade dict."""
    sl = round(ep * (1 - SL_PCT), 2)
    t1 = round(ep * (1 + T1_PCT), 2)
    t2 = round(ep * (1 + T2_PCT), 2)
    t1q = ORDER_QTY // 2; t2q = ORDER_QTY - t1q

    maxp = ep; t1b = False; t1e = 0.0
    t1_spot = None  # NIFTY spot when T1 booked
    xp = xr = xts = None
    exit_spot = None

    for _, row in ddf[ddf["datetime"] > ets].iterrows():
        t  = row["datetime"].time()
        dn = max((expiry.date() - row["datetime"].date()).days, 0)
        curr  = bsm_bid(row["close"], strike, max(dn / 365, 1 / 365), ot, dn)
        maxp  = max(maxp, curr)
        trail = round(maxp * (1 - TRAIL_PCT), 2)

        if t >= SQ_OFF:
            xp, xr, xts, exit_spot = curr, "EOD", row["datetime"], row["close"]; break
        if curr <= sl:
            xp, xr, xts, exit_spot = sl, "STOPLOSS", row["datetime"], row["close"]; break
        if not t1b and curr >= t1:
            t1b = True; t1e = t1; t1_spot = row["close"]
        if t1b:
            if curr >= t2:
                xp, xr, xts, exit_spot = t2, "TARGET_2", row["datetime"], row["close"]; break
            if curr < trail:
                xp, xr, xts, exit_spot = trail, "TRAIL", row["datetime"], row["close"]; break

    if xp is None:
        last = ddf[ddf["datetime"] > ets]
        if not last.empty:
            r  = last.iloc[-1]
            dn = max((expiry.date() - r["datetime"].date()).days, 0)
            xp = bsm_bid(r["close"], strike, max(dn / 365, 1 / 365), ot, dn)
            xts = r["datetime"]; exit_spot = r["close"]
        else:
            xp = ep; xts = ets; exit_spot = esp
        xr = "—"

    # Full PnL calculation
    if t1b:
        t1_pnl  = round((t1e - ep) * t1q, 2)
        t2_pnl  = round((xp  - ep) * t2q, 2)
        pnl     = round(t1_pnl + t2_pnl, 2)
    else:
        t1_pnl = 0.0; t2_pnl = 0.0
        pnl    = round((xp - ep) * ORDER_QTY, 2)

    # Money values
    buy_val   = round(ep * ORDER_QTY, 2)
    sell_val  = round(xp * ORDER_QTY, 2)  # simplified (full qty at exit price)
    if t1b:
        # T1: sold half at t1, rest at xp
        sell_val = round(t1e * t1q + xp * t2q, 2)

    m  = ets.hour * 60 + ets.minute
    sz = "FULL" if m <= 630 else ("MED" if m <= 720 else "HALF")

    return {
        # Identity
        "date":        str(day),
        "signal":      sig,
        "symbol":      make_symbol(strike, ot, expiry),
        "strike":      strike,
        "expiry":      expiry.strftime("%d-%b-%Y"),
        "dte":         dte,
        "iv_pct":      round(get_iv(dte) * 100, 0),
        # Market context
        "orb_range":   0.0,   # filled by caller
        "atr":         0.0,   # filled by caller
        "size":        sz,
        # Entry
        "entry_time":  ets.strftime("%H:%M"),
        "entry_spot":  round(esp, 2),
        "buy_price":   ep,                         # ← option BUY price per lot
        "buy_value":   buy_val,                    # ← total ₹ paid (qty × price)
        "sl":          sl,
        "t1":          t1,
        "t2":          t2,
        # T1 partial
        "t1_hit":      t1b,
        "t1_price":    round(t1e, 2) if t1b else 0.0,
        "t1_spot":     round(t1_spot, 2) if t1_spot else 0.0,
        # Exit
        "exit_time":   xts.strftime("%H:%M") if xts else "—",
        "exit_spot":   round(exit_spot, 2) if exit_spot else 0.0,
        "sell_price":  xp,                         # ← option SELL price per lot
        "sell_value":  sell_val,                   # ← total ₹ received
        "exit_reason": xr,
        # PnL
        "pnl":         pnl,
        "t1_pnl":      t1_pnl,
        "t2_pnl":      t2_pnl,
        "mode":        "BSM",
        # Legacy columns (for CSV compatibility)
        "entry_prem":  ep,
        "exit_price":  xp,
        "t1_exit":     round(t1e, 2) if t1b else 0,
    }


# ═══════════════════════════════════════════════════════════
#  SECTION 4 — OFFLINE BACKTEST
# ═══════════════════════════════════════════════════════════

def run_backtest_offline(df, start_date=None, end_date=None):
    if start_date:
        df = df[df["datetime"].dt.date >= pd.to_datetime(start_date).date()]
    if end_date:
        df = df[df["datetime"].dt.date <= pd.to_datetime(end_date).date()]
    if df.empty:
        print("❌ No data in range."); return [], []

    trades = []; skipped = []

    for day in sorted(df["datetime"].dt.date.unique()):
        ddf = df[df["datetime"].dt.date == day].copy()
        orb = ddf[(ddf["datetime"].dt.time >= time(9, 15)) &
                  (ddf["datetime"].dt.time < time(9, 30))]
        if orb.empty:
            skipped.append((day, "No ORB data")); continue
        oh = orb["high"].max(); ol = orb["low"].min()
        orb_range = oh - ol;    atr = get_atr(ddf)
        if orb_range < MIN_ORB:
            skipped.append((day, f"ORB {orb_range:.0f}pts too small")); continue
        if orb_range < atr * ATR_MULT:
            skipped.append((day, f"ORB {orb_range:.0f}<{ATR_MULT}×ATR {atr:.0f} choppy")); continue

        bu = oh + BUF; bd = ol - BUF
        post = ddf[(ddf["datetime"].dt.time >= time(9, 30)) &
                   (ddf["datetime"].dt.time < NO_ENTRY)]
        sig = ets = esp = None
        for _, row in post.iterrows():
            if row["close"] > bu:   sig, ets, esp = "CALL", row["datetime"], row["close"]; break
            elif row["close"] < bd: sig, ets, esp = "PUT",  row["datetime"], row["close"]; break
        if sig is None:
            skipped.append((day, "No confirmed breakout")); continue

        ot     = "CE" if sig == "CALL" else "PE"
        strike = calc_strike(esp, sig)
        expiry = next_expiry(ets)
        dte    = max((expiry.date() - day).days, 1)

        ep = bsm_ask(esp, strike, dte / 365, ot, dte)
        if ep < MIN_PREM:
            skipped.append((day, f"Premium ₹{ep:.0f} < min ₹{MIN_PREM:.0f}")); continue

        trade = _simulate_trade(ddf, sig, ets, esp, strike, ot, expiry, dte, ep, day)
        trade["orb_range"] = round(orb_range, 1)
        trade["atr"]       = round(atr, 1)
        trades.append(trade)

    return trades, skipped


# ═══════════════════════════════════════════════════════════
#  SECTION 5 — LIVE BACKTEST
# ═══════════════════════════════════════════════════════════

async def run_backtest_live(df, start_date=None, end_date=None):
    if start_date:
        df = df[df["datetime"].dt.date >= pd.to_datetime(start_date).date()]
    if end_date:
        df = df[df["datetime"].dt.date <= pd.to_datetime(end_date).date()]
    if df.empty:
        print("❌ No data in range."); return [], []

    await _login_samco()
    today = date.today(); trades = []; skipped = []
    live_count = 0; bsm_count = 0

    for day in sorted(df["datetime"].dt.date.unique()):
        ddf = df[df["datetime"].dt.date == day].copy()
        orb = ddf[(ddf["datetime"].dt.time >= time(9, 15)) &
                  (ddf["datetime"].dt.time < time(9, 30))]
        if orb.empty:
            skipped.append((day, "No ORB data")); continue
        oh = orb["high"].max(); ol = orb["low"].min()
        orb_range = oh - ol;    atr = get_atr(ddf)
        if orb_range < MIN_ORB:
            skipped.append((day, f"ORB {orb_range:.0f}pts too small")); continue
        if orb_range < atr * ATR_MULT:
            skipped.append((day, f"ORB {orb_range:.0f}<{ATR_MULT}×ATR {atr:.0f} choppy")); continue

        bu = oh + BUF; bd = ol - BUF
        post = ddf[(ddf["datetime"].dt.time >= time(9, 30)) &
                   (ddf["datetime"].dt.time < NO_ENTRY)]
        sig = ets = esp = None
        for _, row in post.iterrows():
            if row["close"] > bu:   sig, ets, esp = "CALL", row["datetime"], row["close"]; break
            elif row["close"] < bd: sig, ets, esp = "PUT",  row["datetime"], row["close"]; break
        if sig is None:
            skipped.append((day, "No confirmed breakout")); continue

        expiry = next_expiry(ets); ot = "CE" if sig == "CALL" else "PE"
        dte    = max((expiry.date() - day).days, 1); is_today = (day == today)

        if is_today:
            ep_real, strike, symbol = await get_live_price(esp, sig, expiry)
        else:
            ep_real = None

        if ep_real is not None:
            ep = ep_real; live_count += 1; mode_tag = "SAMCO"
        else:
            strike = calc_strike(esp, sig)
            ep     = bsm_ask(esp, strike, dte / 365, ot, dte)
            bsm_count += 1; mode_tag = "BSM"

        if ep < MIN_PREM:
            skipped.append((day, f"Premium ₹{ep:.0f} < min ₹{MIN_PREM:.0f}")); continue

        trade = _simulate_trade(ddf, sig, ets, esp, strike, ot, expiry, dte, ep, day)
        trade["orb_range"] = round(orb_range, 1)
        trade["atr"]       = round(atr, 1)
        trade["mode"]      = mode_tag
        trades.append(trade)

    print(f"\n  📡 Pricing: {live_count} SAMCO live  |  {bsm_count} BSM (DTE-calibrated)")
    return trades, skipped


# ═══════════════════════════════════════════════════════════
#  SECTION 6 — TERMINAL REPORT
# ═══════════════════════════════════════════════════════════

def print_report(trades, skipped, data_info, live_mode=False):
    if not trades:
        print("\n  ❌ No trades generated.\n"); return

    tdf  = pd.DataFrame(trades)
    tot  = len(tdf)
    wins = tdf[tdf["pnl"] > 0]; losses = tdf[tdf["pnl"] <= 0]
    wr   = len(wins) / tot * 100; tp = tdf["pnl"].sum()
    aw   = wins["pnl"].mean()   if len(wins)   else 0
    al   = losses["pnl"].mean() if len(losses) else 0
    pf   = abs(wins["pnl"].sum() / losses["pnl"].sum()) \
           if len(losses) and losses["pnl"].sum() != 0 else 99
    eq   = tdf["pnl"].cumsum(); dd = (eq - eq.cummax()).min()
    rr   = abs(aw / al) if al else 0
    sh   = tdf["pnl"].mean() / tdf["pnl"].std() * sqrt(252 / 5) \
           if tdf["pnl"].std() > 0 else 0
    neg  = tdf[tdf["pnl"] < 0]["pnl"]
    so   = tdf["pnl"].mean() / neg.std() * sqrt(252 / 5) \
           if len(neg) > 1 and neg.std() > 0 else 0
    t1c  = int(tdf["t1_hit"].sum())
    ec   = tdf.groupby("exit_reason")["pnl"].agg(["count", "mean", "sum"])

    live_n   = len(tdf[tdf["mode"] == "SAMCO"]) if "mode" in tdf.columns else 0
    bsm_n    = tot - live_n
    avg_iv   = tdf["iv_pct"].mean() if "iv_pct" in tdf.columns else 16.0

    W = "═" * 80; S = "─" * 80
    mode_label = "🔴 SAMCO LIVE + BSM HISTORY" if live_mode else "📊 DTE-CALIBRATED IV"

    print(f"\n{W}")
    print(f"  🏆  LORDS BOT — BACKTEST RESULTS  [{mode_label}]")
    print(f"  Data   : {data_info}")
    print(f"  Config : {CFG_SOURCE}")
    print(f"{W}")

    print(f"\n  ┌─ PRICING MODEL {'─'*61}")
    if live_mode:
        print(f"  │  TODAY  : SAMCO option chain → real CE/PE LTP ({live_n} trade{'s' if live_n!=1 else ''})")
        print(f"  │  HISTORY: DTE-calibrated IV (avg {avg_iv:.0f}%) + ₹{SPREAD:.0f} spread each side")
    else:
        print(f"  │  DTE-calibrated IV (avg {avg_iv:.0f}%) — matched to real NSE option chain prices")
        print(f"  │  DTE 1→IV7%   DTE 2→IV9%   DTE 3→IV11%   DTE 4-5→IV13%   DTE≥7→IV15%")
        print(f"  │  Entry: BUY at ask (BSM + ₹{SPREAD:.0f})   Exit: SELL at bid (BSM − ₹{SPREAD:.0f})")
        print(f"  │  Round-trip friction: ₹{SPREAD*2:.0f}/option × {ORDER_QTY} lots = ₹{SPREAD*2*ORDER_QTY:.0f}/trade")
    print(f"  └{'─'*78}")

    print(f"\n  ┌─ BOT CONFIG {'─'*64}")
    print(f"  │  SL {SL_PCT*100:.0f}%  |  T1 {T1_PCT*100:.0f}% ({ORDER_QTY//2} lots)  "
          f"|  T2 {T2_PCT*100:.0f}% ({ORDER_QTY-ORDER_QTY//2} lots)  |  Trail {TRAIL_PCT*100:.0f}%")
    print(f"  │  Min prem ₹{MIN_PREM:.0f}  |  OTM +{OTM_DIST}  |  ATR ×{ATR_MULT}  "
          f"|  Buffer {BUF}pts  |  Qty {ORDER_QTY}")
    print(f"  │  No entry after {NO_ENTRY.strftime('%H:%M')}  "
          f"|  Square-off {SQ_OFF.strftime('%H:%M')}  "
          f"|  Trend {'ON' if TREND_ON else 'OFF'}")
    print(f"  └{'─'*78}")

    print(f"\n  ┌─ PERFORMANCE {'─'*63}")
    print(f"  │  Trades taken     : {tot}  ({len(skipped)} days skipped)")
    print(f"  │  Win Rate         : {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  │  Net P&L          : ₹{tp:+,.2f}")
    print(f"  │  Profit Factor    : {pf:.2f}x  "
          f"({'✅ Strong' if pf>=1.8 else '✅ Good' if pf>=1.5 else '⚠️  Marginal'})")
    print(f"  │  Avg Win          : ₹{aw:+,.2f}")
    print(f"  │  Avg Loss         : ₹{al:+,.2f}")
    print(f"  │  Reward/Risk      : {rr:.2f}x  "
          f"({'✅ Pro grade' if rr>=2.0 else '✅ Good' if rr>=1.5 else '⚠️'})")
    print(f"  ├─ RISK {'─'*70}")
    print(f"  │  Max Drawdown     : ₹{dd:,.2f}")
    print(f"  │  Sharpe Ratio     : {sh:.2f}  "
          f"({'✅ Good' if sh>=1.5 else '✅ OK' if sh>=1.0 else '⚠️'})")
    print(f"  │  Sortino Ratio    : {so:.2f}  "
          f"({'✅ Excellent' if so>=5 else '✅ Good' if so>=2 else '⚠️'})")
    print(f"  │  T1 partial hits  : {t1c}/{tot} ({t1c/tot*100:.0f}%) — locked partial profit")
    print(f"  └{'─'*78}")

    print(f"\n  EXIT BREAKDOWN")
    print(f"  {S}")
    for r, row in ec.iterrows():
        pct  = row["count"] / tot * 100
        bar  = "█" * min(int(row["count"] * 2), 22)
        icon = "✅" if row["mean"] > 0 else "❌"
        print(f"  {r:<12} {int(row['count']):>2} ({pct:>3.0f}%)  "
              f"avg ₹{row['mean']:>+8,.0f}  total ₹{row['sum']:>+9,.0f}  {icon}{bar}")

    print(f"\n  SIGNAL BREAKDOWN")
    print(f"  {S}")
    for sg in ["CALL", "PUT"]:
        s2 = tdf[tdf["signal"] == sg]
        if len(s2):
            swr = (s2["pnl"] > 0).mean() * 100
            print(f"  {sg}  {len(s2):>2} trades  WR {swr:.0f}%  P&L ₹{s2['pnl'].sum():>+9,.0f}")

    # ══ FULL TRADE TICKET LOG ═══════════════════════════════
        # ══ FULL TRADE TICKET LOG ═══════════════════════════════
    print(f"\n  TRADE TICKET LOG  — exact buy/sell prices per contract")
    print(f"  {S}")

    print(f"  {'#':>3}  {'Date':<12} {'Entry':<6} {'Exit':<6} "
          f"{'Contract':<20} {'Expiry':<12} "
          f"{'DTE':>3} {'IV':>3}  "
          f"{'Spot IN':>8} {'BUY ₹':>7} {'×Qty':>5} {'BuyVal':>9}  "
          f"{'Spot OUT':>9} {'SELL ₹':>7} {'SellVal':>9}  "
          f"{'Rsn':<7} {'P&L':>9}  Run")
    print(f"  {S}")

    cum = 0
    for i, (_, r) in enumerate(tdf.iterrows(), 1):
        cum  += r["pnl"]
        icon  = "✅" if r["pnl"] > 0 else "❌"
        t1m   = "🎯" if r["t1_hit"] else "  "

        rs = {"STOPLOSS": "SL", "TARGET_2": "T2",
              "TRAIL": "TSL", "EOD": "EOD", "—": "—"}.get(
              r["exit_reason"], r["exit_reason"][:4])

        sym   = str(r.get("symbol", "—"))[:20]
        exp_d = str(r.get("expiry", "—"))[:12]
        src   = "📡" if r.get("mode") == "SAMCO" else "  "

        buy_p  = r.get("buy_price",  r.get("entry_prem", 0))
        sell_p = r.get("sell_price", r.get("exit_price",  0))
        bv     = r.get("buy_value",  round(buy_p  * ORDER_QTY, 0))
        sv     = r.get("sell_value", round(sell_p * ORDER_QTY, 0))
        e_spot = r.get("entry_spot", 0)
        x_spot = r.get("exit_spot",  0)

        entry_t = r.get("entry_time", "--")
        exit_t  = r.get("exit_time", "--")

        print(f"  {i:>3}  {r['date']:<12} {entry_t:<6} {exit_t:<6} "
              f"{sym:<20} {exp_d:<12} "
              f"{int(r.get('dte',0)):>3} {int(r.get('iv_pct',0)):>2}%  "
              f"{e_spot:>8.0f} {buy_p:>7.2f} ×{ORDER_QTY:<4} ₹{bv:>8,.0f}  "
              f"{x_spot:>9.0f} {sell_p:>7.2f} ₹{sv:>8,.0f}  "
              f"{rs:<7} ₹{r['pnl']:>+8,.0f}  "
              f"{icon}{src}{t1m}  ₹{cum:>+9,.0f}")

    print(f"\n  Legend: 🎯=T1 partial booked  📡=SAMCO live price  ✅=Win  ❌=Loss")

    # ══ DETAILED T1 PARTIAL EXITS ═══════════════════════════
    partial = tdf[tdf["t1_hit"] == True]
    if len(partial) > 0:
        print(f"\n  T1 PARTIAL EXITS (50% sold at T1, 50% held)")
        print(f"  {S}")
        print(f"  {'#':>3}  {'Date':<12} {'Contract':<20}  "
              f"{'T1 SELL ₹':>10} {'T1 Spot':>8} {'T1 P&L':>9}  "
              f"{'T2 SELL ₹':>10} {'T2 Spot':>8} {'T2 P&L':>9}  {'Total P&L':>10}")
        print(f"  {S}")
        j = 0
        for _, r in partial.iterrows():
            j += 1
            sym    = str(r.get("symbol", "—"))[:20]
            t1p    = r.get("t1_price", r.get("t1_exit", 0))
            t1s    = r.get("t1_spot",  0)
            t1pnl  = r.get("t1_pnl",  0)
            t2p    = r.get("sell_price", r.get("exit_price", 0))
            t2s    = r.get("exit_spot", 0)
            t2pnl  = r.get("t2_pnl",  0)
            tot_pnl = r.get("pnl", 0)
            rs = {"STOPLOSS":"SL","TARGET_2":"T2","TRAIL":"TSL","EOD":"EOD","—":"—"}.get(
                  r["exit_reason"], r["exit_reason"][:4])
            print(f"  {j:>3}  {r['date']:<12} {sym:<20}  "
                  f"{t1p:>10.2f} {t1s:>8.0f} ₹{t1pnl:>+8,.0f}  "
                  f"{t2p:>10.2f} {t2s:>8.0f} ₹{t2pnl:>+8,.0f}  "
                  f"₹{tot_pnl:>+9,.0f}  [{rs}]")

    # ══ SKIPPED DAYS ════════════════════════════════════════
    print(f"\n  SKIPPED DAYS")
    print(f"  {S}")
    if skipped:
        for d, reason in skipped:
            print(f"  {str(d):<12}  {reason}")
    else:
        print(f"  (none)")

    # ══ MONTHLY P&L ═════════════════════════════════════════
    print(f"\n  MONTHLY P&L")
    print(f"  {S}")
    tdf["date"] = pd.to_datetime(tdf["date"])
    for p, mpnl in tdf.groupby(tdf["date"].dt.to_period("M"))["pnl"].sum().items():
        m   = tdf[tdf["date"].dt.to_period("M") == p]
        w   = (m["pnl"] > 0).sum(); l = (m["pnl"] <= 0).sum()
        bar = "█" * min(int(abs(mpnl) / 300), 28)
        sgn = "+" if mpnl >= 0 else "-"
        print(f"  {str(p):<10}  ₹{mpnl:>+10,.2f}  {len(m)} trades  {w}W/{l}L  {sgn}{bar}")

    # ══ SCALABILITY ═════════════════════════════════════════
    print(f"\n  SCALABILITY")
    print(f"  {S}")
    months      = max(tdf["date"].dt.to_period("M").nunique(), 1)
    monthly_avg = tp / months
    for lots in [1, 2, 3, 5, 10]:
        print(f"  {lots:>2} lot{'s' if lots > 1 else ' '}  "
              f"est monthly ₹{monthly_avg * lots:>+10,.0f}  "
              f"max DD ₹{dd * lots:>+9,.0f}  "
              f"capital ₹{abs(dd) * lots * 3:>9,.0f}")

    print(f"\n{W}")
    v = "✅ PROFITABLE" if tp > 0 else "❌ LOSS"
    print(f"  {v}  |  ₹{tp:+,.2f}  |  {tot} trades  |  WR {wr:.1f}%  |  Sharpe {sh:.2f}")
    print(f"  PF {pf:.2f}x  |  R:R {rr:.2f}x  |  Max DD ₹{dd:,.0f}  |  Sortino {so:.2f}")
    if live_mode:
        print(f"\n  📡 {live_n} SAMCO live  |  {bsm_n} DTE-calibrated BSM")
    else:
        print(f"\n  💡 DTE-calibrated IV + ₹{SPREAD:.0f} spread = closest BSM to real NSE prices.")
    print(f"{W}\n")

    tdf.to_csv(ROOT / "data" / "backtest_results.csv", index=False)
    print(f"  📁 Saved: data/backtest_results.csv\n")


# ═══════════════════════════════════════════════════════════
#  SECTION 7 — ENTRY POINT
# ═══════════════════════════════════════════════════════════

def _find_csv() -> str:
    data_dir = ROOT / "data"
    if not data_dir.exists():
        print("\n  ❌ data/ folder not found.\n"); sys.exit(1)
    csvs = [f for f in data_dir.glob("nifty_1min_*.csv") if "dataset" not in f.name]
    if not csvs: csvs = sorted(data_dir.glob("*.csv"), reverse=True)
    if not csvs:
        print("\n  ❌ No CSV in data/\n  Run: python download_nifty_data.py\n"); sys.exit(1)
    return str(sorted(csvs, reverse=True)[0])


def _load_df(csv_file: str):
    print(f"\n  📊 Loading {Path(csv_file).name}...")
    df = pd.read_csv(csv_file)
    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"])
    elif "date" in df.columns:
        print("  ⚠️  Daily OHLC file — needs 1-min data."); sys.exit(1)
    else:
        print("  ❌ No datetime column"); sys.exit(1)
    df   = df.sort_values("datetime").reset_index(drop=True)
    days = df["datetime"].dt.date.nunique()
    info = (f"{df['datetime'].min().date()} → {df['datetime'].max().date()} "
            f"| {len(df):,} candles | {days} days")
    print(f"  ✅ {info}")
    return df, info


def main():
    parser = argparse.ArgumentParser(description="Lords Bot Backtester")
    parser.add_argument("--file",  default=None)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end",   default=None)
    parser.add_argument("--live",  action="store_true",
                        help="Use SAMCO live prices for today")
    args = parser.parse_args()

    csv_file = args.file or _find_csv()
    if not args.file: print(f"\n  📂 Auto-detected: {csv_file}")
    if not Path(csv_file).exists():
        print(f"\n  ❌ File not found: {csv_file}\n"); sys.exit(1)

    df, data_info = _load_df(csv_file)

    if args.live:
        print(f"  ⚙️  Running (SAMCO live today + DTE-calibrated BSM history)...\n")
        trades, skipped = asyncio.run(run_backtest_live(df, args.start, args.end))
        print_report(trades, skipped, data_info, live_mode=True)
    else:
        print(f"  ⚙️  Running (DTE-calibrated IV + ₹{SPREAD:.0f} spread)...\n")
        trades, skipped = run_backtest_offline(df, args.start, args.end)
        print_report(trades, skipped, data_info, live_mode=False)


if __name__ == "__main__":
    main()
