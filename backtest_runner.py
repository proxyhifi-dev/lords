"""
Lords Bot — Backtest Runner  (Production Grade)
================================================
TWO MODES:

  MODE 1 — OFFLINE (default, works any time, no login needed):
      1-min NIFTY spot CSV + BSM pricing + ₹2 bid/ask spread.
      python backtest_runner.py

  MODE 2 — LIVE (today only, during market hours 09:15–15:30):
      SAMCO option chain → real CE/PE LTP for TODAY's trades.
      Historical dates automatically fall back to BSM (SAMCO
      does not provide historical option chain data via REST).
      python backtest_runner.py --live

USAGE:
  python backtest_runner.py
  python backtest_runner.py --file data/nifty_1min_20260413.csv
  python backtest_runner.py --start 2026-03-01 --end 2026-04-13
  python backtest_runner.py --live
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

LOT_SIZE = 65
STEP     = 50
SPREAD   = 2.0
API_CALL_DELAY = 0.3   # seconds between SAMCO API calls


# ═══════════════════════════════════════════════════════════
#  SECTION 1 — OPTION PRICE HELPERS
# ═══════════════════════════════════════════════════════════

def bsm(S, K, T, opt="CE", r=0.065, iv=0.16) -> float:
    if T <= 0:
        return max(S - K, 0.05) if opt == "CE" else max(K - S, 0.05)
    d1 = (log(S / K) + (r + 0.5 * iv ** 2) * T) / (iv * sqrt(T))
    d2 = d1 - iv * sqrt(T)
    if opt == "CE":
        return max(S * norm.cdf(d1) - K * exp(-r * T) * norm.cdf(d2), 0.05)
    return max(K * exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1), 0.05)


def bsm_ask(S, K, T, opt) -> float:
    return round(bsm(S, K, T, opt) + SPREAD, 2)


def bsm_bid(S, K, T, opt) -> float:
    return round(max(bsm(S, K, T, opt) - SPREAD, 0.05), 2)


def next_thu(dt) -> datetime:
    d = (3 - dt.weekday()) % 7
    if d == 0 and dt.hour >= 15 and dt.minute >= 30:
        d = 7
    return dt + timedelta(days=d)


def calc_atm(spot: float) -> int:
    return int(round(spot / STEP) * STEP)


def calc_strike(spot: float, sig: str) -> int:
    a = calc_atm(spot)
    return a + OTM_DIST * STEP if sig == "CALL" else a - OTM_DIST * STEP


def make_symbol(strike: int, ot: str, expiry: datetime) -> str:
    """NSE format: NIFTY26APR24000CE"""
    mon = expiry.strftime("%b").upper()
    yr  = expiry.strftime("%y")
    return f"NIFTY{yr}{mon}{strike}{ot}"


def get_atr(day_df) -> float:
    orb = day_df[(day_df["datetime"].dt.time >= time(9, 15)) &
                 (day_df["datetime"].dt.time < time(9, 30))]
    if len(orb) < 2:
        return 15.0
    return max((orb["high"] - orb["low"]).mean(), 1.0)


# ═══════════════════════════════════════════════════════════
#  SECTION 2 — LIVE SAMCO OPTION PRICE FETCHER
#
#  IMPORTANT:
#  SAMCO REST API only provides CURRENT option chain (live LTP).
#  It does NOT store historical option prices.
#  So --live mode gives real prices for TODAY only.
#  Past dates automatically use BSM fallback.
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
    print("  ℹ️  SAMCO API provides live prices for TODAY only.")
    print("       Historical dates will use BSM fallback automatically.\n")


async def _fetch_chain_safe(spot: float, expiry: datetime, ot: str) -> dict:
    """Fetch option chain with timeout + rate limit. Returns {} on failure."""
    import time as _time

    exp_str = expiry.strftime("%Y-%m-%d")
    key     = (exp_str, ot)

    cached = _chain_cache.get(key)
    if cached and (_time.time() - cached["ts"]) < _CHAIN_TTL:
        return cached["data"]

    client = _get_samco()
    await asyncio.sleep(API_CALL_DELAY)   # rate limit

    try:
        resp = await asyncio.wait_for(
            client.get_option_chain(
                search_symbol_name="NIFTY",
                exchange="NFO",
                expiry_date=exp_str,
                strike_price=str(calc_atm(spot)),
                option_type=ot,
            ),
            timeout=5.0,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        return {}

    rows   = resp.get("optionChainDetails") or resp.get("data") or []
    result = {}
    for row in rows:
        try:
            k   = int(float(row.get("strikePrice", 0)))
            ltp = float(str(row.get("lastTradedPrice", 0) or
                            row.get("ltp", 0) or 0).replace(",", ""))
            if ltp > 0:
                result[k] = ltp
        except (ValueError, TypeError):
            continue

    if result:
        _chain_cache[key] = {"ts": _time.time(), "data": result}
    return result


async def get_live_price(spot: float, sig: str, expiry: datetime) -> tuple:
    """Returns (ltp, strike, symbol). ltp=None if unavailable."""
    ot     = "CE" if sig == "CALL" else "PE"
    strike = calc_strike(spot, sig)
    symbol = make_symbol(strike, ot, expiry)

    chain = await _fetch_chain_safe(spot, expiry, ot)
    if not chain:
        return None, strike, symbol

    for try_k in [strike, calc_atm(spot),
                  *sorted(chain.keys(), key=lambda k: abs(k - strike))]:
        if try_k in chain and chain[try_k] > 0:
            return round(chain[try_k], 2), try_k, make_symbol(try_k, ot, expiry)

    return None, strike, symbol


# ═══════════════════════════════════════════════════════════
#  SECTION 3 — OFFLINE BACKTEST ENGINE
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
            if row["close"] > bu:
                sig, ets, esp = "CALL", row["datetime"], row["close"]; break
            elif row["close"] < bd:
                sig, ets, esp = "PUT",  row["datetime"], row["close"]; break

        if sig is None:
            skipped.append((day, "No confirmed breakout")); continue

        ot     = "CE" if sig == "CALL" else "PE"
        strike = calc_strike(esp, sig)
        expiry = next_thu(ets)
        dte    = max((expiry.date() - day).days, 1)
        symbol = make_symbol(strike, ot, expiry)

        ep = bsm_ask(esp, strike, dte / 365, ot)
        if ep < MIN_PREM:
            skipped.append((day, f"Premium ₹{ep:.0f} < min ₹{MIN_PREM:.0f}")); continue

        sl = round(ep*(1-SL_PCT),2); t1 = round(ep*(1+T1_PCT),2)
        t2 = round(ep*(1+T2_PCT),2)
        t1q = ORDER_QTY // 2; t2q = ORDER_QTY - t1q

        maxp = ep; t1b = False; t1e = 0.0
        xp = xr = xts = None

        for _, row in ddf[ddf["datetime"] > ets].iterrows():
            t  = row["datetime"].time()
            dn = max((expiry.date() - row["datetime"].date()).days, 0)
            curr  = bsm_bid(row["close"], strike, max(dn/365, 1/365), ot)
            maxp  = max(maxp, curr)
            trail = round(maxp * (1 - TRAIL_PCT), 2)

            if t >= SQ_OFF:      xp, xr, xts = curr,  "EOD",      row["datetime"]; break
            if curr <= sl:       xp, xr, xts = sl,    "STOPLOSS",  row["datetime"]; break
            if not t1b and curr >= t1: t1b = True; t1e = t1
            if t1b:
                if curr >= t2:   xp, xr, xts = t2,    "TARGET_2", row["datetime"]; break
                if curr < trail: xp, xr, xts = trail, "TRAIL",    row["datetime"]; break

        if xp is None:
            last = ddf[ddf["datetime"] > ets]
            if not last.empty:
                r  = last.iloc[-1]
                dn = max((expiry.date() - r["datetime"].date()).days, 0)
                xp  = bsm_bid(r["close"], strike, max(dn/365, 1/365), ot)
                xts = r["datetime"]
            else:
                xp = ep; xts = ets
            xr = "—"

        pnl = round((t1e-ep)*t1q + (xp-ep)*t2q, 2) if t1b \
              else round((xp-ep)*ORDER_QTY, 2)
        m  = ets.hour*60 + ets.minute
        sz = "FULL" if m<=630 else ("MED" if m<=720 else "HALF")

        trades.append({
            "date": str(day), "signal": sig, "symbol": symbol,
            "strike": strike, "orb_range": round(orb_range,1), "atr": round(atr,1),
            "size": sz, "entry_time": ets.strftime("%H:%M"),
            "entry_spot": round(esp,2), "entry_prem": ep,
            "sl": sl, "t1": t1, "t2": t2,
            "t1_hit": t1b, "t1_exit": round(t1e,2) if t1b else 0,
            "exit_time": xts.strftime("%H:%M") if xts else "—",
            "exit_price": xp, "exit_reason": xr, "pnl": pnl,
            "mode": "BSM",
        })

    return trades, skipped


# ═══════════════════════════════════════════════════════════
#  SECTION 4 — LIVE BACKTEST ENGINE
#  Real SAMCO prices for TODAY, BSM for history.
# ═══════════════════════════════════════════════════════════

async def run_backtest_live(df, start_date=None, end_date=None):
    if start_date:
        df = df[df["datetime"].dt.date >= pd.to_datetime(start_date).date()]
    if end_date:
        df = df[df["datetime"].dt.date <= pd.to_datetime(end_date).date()]
    if df.empty:
        print("❌ No data in range."); return [], []

    await _login_samco()

    today      = date.today()
    trades     = []; skipped = []
    live_count = 0;  bsm_count = 0

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
            if row["close"] > bu:
                sig, ets, esp = "CALL", row["datetime"], row["close"]; break
            elif row["close"] < bd:
                sig, ets, esp = "PUT",  row["datetime"], row["close"]; break

        if sig is None:
            skipped.append((day, "No confirmed breakout")); continue

        expiry   = next_thu(ets)
        ot       = "CE" if sig == "CALL" else "PE"
        is_today = (day == today)

        if is_today:
            ep_real, strike, symbol = await get_live_price(esp, sig, expiry)
        else:
            ep_real = None

        if ep_real is not None:
            ep = ep_real; mode_tag = "SAMCO"; live_count += 1
        else:
            strike   = calc_strike(esp, sig)
            dte      = max((expiry.date() - day).days, 1)
            ep       = bsm_ask(esp, strike, dte / 365, ot)
            symbol   = make_symbol(strike, ot, expiry)
            mode_tag = "BSM"; bsm_count += 1

        if ep < MIN_PREM:
            skipped.append((day, f"Premium ₹{ep:.0f} < min ₹{MIN_PREM:.0f}")); continue

        sl = round(ep*(1-SL_PCT),2); t1 = round(ep*(1+T1_PCT),2)
        t2 = round(ep*(1+T2_PCT),2)
        t1q = ORDER_QTY // 2; t2q = ORDER_QTY - t1q

        maxp = ep; t1b = False; t1e = 0.0
        xp = xr = xts = None

        for _, row in ddf[ddf["datetime"] > ets].iterrows():
            t  = row["datetime"].time()
            dn = max((expiry.date() - row["datetime"].date()).days, 0)

            if is_today:
                curr_real, _, _ = await get_live_price(row["close"], sig, expiry)
                curr = curr_real if curr_real else bsm_bid(row["close"], strike, max(dn/365, 1/365), ot)
            else:
                curr = bsm_bid(row["close"], strike, max(dn/365, 1/365), ot)

            maxp  = max(maxp, curr)
            trail = round(maxp * (1 - TRAIL_PCT), 2)

            if t >= SQ_OFF:      xp, xr, xts = curr,  "EOD",      row["datetime"]; break
            if curr <= sl:       xp, xr, xts = sl,    "STOPLOSS",  row["datetime"]; break
            if not t1b and curr >= t1: t1b = True; t1e = t1
            if t1b:
                if curr >= t2:   xp, xr, xts = t2,    "TARGET_2", row["datetime"]; break
                if curr < trail: xp, xr, xts = trail, "TRAIL",    row["datetime"]; break

        if xp is None:
            last = ddf[ddf["datetime"] > ets]
            if not last.empty:
                r  = last.iloc[-1]
                dn = max((expiry.date() - r["datetime"].date()).days, 0)
                if is_today:
                    p, _, _ = await get_live_price(r["close"], sig, expiry)
                    xp = p if p else bsm_bid(r["close"], strike, max(dn/365, 1/365), ot)
                else:
                    xp = bsm_bid(r["close"], strike, max(dn/365, 1/365), ot)
                xts = r["datetime"]
            else:
                xp = ep; xts = ets
            xr = "—"

        pnl = round((t1e-ep)*t1q + (xp-ep)*t2q, 2) if t1b \
              else round((xp-ep)*ORDER_QTY, 2)
        m  = ets.hour*60 + ets.minute
        sz = "FULL" if m<=630 else ("MED" if m<=720 else "HALF")

        trades.append({
            "date": str(day), "signal": sig, "symbol": symbol,
            "strike": strike, "orb_range": round(orb_range,1), "atr": round(atr,1),
            "size": sz, "entry_time": ets.strftime("%H:%M"),
            "entry_spot": round(esp,2), "entry_prem": ep,
            "sl": sl, "t1": t1, "t2": t2,
            "t1_hit": t1b, "t1_exit": round(t1e,2) if t1b else 0,
            "exit_time": xts.strftime("%H:%M") if xts else "—",
            "exit_price": xp, "exit_reason": xr, "pnl": pnl,
            "mode": mode_tag,
        })

    print(f"\n  📡 Pricing: {live_count} SAMCO live  |  {bsm_count} BSM fallback")
    return trades, skipped


# ═══════════════════════════════════════════════════════════
#  SECTION 5 — TERMINAL REPORT
# ═══════════════════════════════════════════════════════════

def print_report(trades, skipped, data_info, live_mode=False):
    if not trades:
        print("\n  ❌ No trades generated.\n"); return

    tdf  = pd.DataFrame(trades)
    tot  = len(tdf)
    wins   = tdf[tdf["pnl"] > 0]
    losses = tdf[tdf["pnl"] <= 0]
    wr   = len(wins) / tot * 100
    tp   = tdf["pnl"].sum()
    aw   = wins["pnl"].mean()   if len(wins)   else 0
    al   = losses["pnl"].mean() if len(losses) else 0
    pf   = abs(wins["pnl"].sum() / losses["pnl"].sum()) \
           if len(losses) and losses["pnl"].sum() != 0 else 99
    eq   = tdf["pnl"].cumsum()
    dd   = (eq - eq.cummax()).min()
    rr   = abs(aw / al) if al else 0
    sh   = tdf["pnl"].mean() / tdf["pnl"].std() * sqrt(252 / 5) \
           if tdf["pnl"].std() > 0 else 0
    neg  = tdf[tdf["pnl"] < 0]["pnl"]
    so   = tdf["pnl"].mean() / neg.std() * sqrt(252 / 5) \
           if len(neg) > 1 and neg.std() > 0 else 0
    t1c  = int(tdf["t1_hit"].sum())
    ec   = tdf.groupby("exit_reason")["pnl"].agg(["count", "mean", "sum"])

    live_n = len(tdf[tdf["mode"] == "SAMCO"]) if "mode" in tdf.columns else 0
    bsm_n  = tot - live_n

    W = "═" * 72
    S = "─" * 72
    mode_label = "🔴 SAMCO LIVE + BSM HISTORY" if live_mode else "📊 BSM + ₹2 SPREAD"

    print(f"\n{W}")
    print(f"  🏆  LORDS BOT — BACKTEST RESULTS  [{mode_label}]")
    print(f"  Data   : {data_info}")
    print(f"  Config : {CFG_SOURCE}")
    print(f"{W}")

    print(f"\n  ┌─ PRICING MODEL {'─'*53}")
    if live_mode:
        print(f"  │  TODAY  : SAMCO option chain → real CE/PE LTP ({live_n} trade{'s' if live_n!=1 else ''})")
        print(f"  │  HISTORY: BSM IV=16% + ₹{SPREAD:.0f} spread each side ({bsm_n} trades)")
        print(f"  │  Note   : SAMCO REST API has no historical option data")
    else:
        print(f"  │  BSM IV=16%  |  Entry: ask = mid + ₹{SPREAD:.0f}  |  Exit: bid = mid − ₹{SPREAD:.0f}")
        print(f"  │  Round-trip: ₹{SPREAD*2:.0f}/option × {ORDER_QTY} lots = ₹{SPREAD*2*ORDER_QTY:.0f}/trade friction")
    print(f"  └{'─'*70}")

    print(f"\n  ┌─ BOT CONFIG USED {'─'*52}")
    print(f"  │  SL {SL_PCT*100:.0f}%"
          f"  |  T1 {T1_PCT*100:.0f}% ({ORDER_QTY//2} lots)"
          f"  |  T2 {T2_PCT*100:.0f}% ({ORDER_QTY-ORDER_QTY//2} lots)"
          f"  |  Trail {TRAIL_PCT*100:.0f}%")
    print(f"  │  Min prem ₹{MIN_PREM:.0f}"
          f"  |  OTM +{OTM_DIST}"
          f"  |  ATR ×{ATR_MULT}"
          f"  |  Buffer {BUF}pts"
          f"  |  Qty {ORDER_QTY}")
    print(f"  │  No entry after {NO_ENTRY.strftime('%H:%M')}"
          f"  |  Square-off {SQ_OFF.strftime('%H:%M')}"
          f"  |  Trend filter {'ON' if TREND_ON else 'OFF'}")
    print(f"  └{'─'*70}")

    print(f"\n  ┌─ PERFORMANCE {'─'*56}")
    print(f"  │  Trades taken     : {tot}  ({len(skipped)} days skipped)")
    print(f"  │  Win Rate         : {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  │  Net P&L          : ₹{tp:+,.2f}")
    print(f"  │  Profit Factor    : {pf:.2f}x  "
          f"({'✅ Strong' if pf>=1.8 else '✅ Good' if pf>=1.5 else '⚠️  Marginal'})")
    print(f"  │  Avg Win          : ₹{aw:+,.2f}")
    print(f"  │  Avg Loss         : ₹{al:+,.2f}")
    print(f"  │  Reward/Risk      : {rr:.2f}x  "
          f"({'✅ Pro grade' if rr>=2.0 else '✅ Good' if rr>=1.5 else '⚠️'})")
    print(f"  ├─ RISK {'─'*62}")
    print(f"  │  Max Drawdown     : ₹{dd:,.2f}")
    print(f"  │  Sharpe Ratio     : {sh:.2f}  "
          f"({'✅ Good' if sh>=1.5 else '✅ OK' if sh>=1.0 else '⚠️'})")
    print(f"  │  Sortino Ratio    : {so:.2f}  "
          f"({'✅ Excellent' if so>=5 else '✅ Good' if so>=2 else '⚠️'})")
    print(f"  │  T1 partial hits  : {t1c}/{tot} ({t1c/tot*100:.0f}%) "
          f"— locked partial profit")
    print(f"  └{'─'*70}")

    print(f"\n  EXIT BREAKDOWN")
    print(f"  {S}")
    for r, row in ec.iterrows():
        pct  = row["count"] / tot * 100
        bar  = "█" * min(int(row["count"] * 2), 22)
        icon = "✅" if row["mean"] > 0 else "❌"
        print(f"  {r:<12} {int(row['count']):>2} ({pct:>3.0f}%)  "
              f"avg ₹{row['mean']:>+8,.0f}  "
              f"total ₹{row['sum']:>+9,.0f}  {icon}{bar}")

    print(f"\n  SIGNAL BREAKDOWN")
    print(f"  {S}")
    for sg in ["CALL", "PUT"]:
        s2 = tdf[tdf["signal"] == sg]
        if len(s2):
            swr = (s2["pnl"] > 0).mean() * 100
            print(f"  {sg}  {len(s2):>2} trades  WR {swr:.0f}%  "
                  f"P&L ₹{s2['pnl'].sum():>+9,.0f}")

    print(f"\n  TRADE LOG")
    print(f"  {S}")
    print(f"  {'Date':<12} {'Sig':<5} {'Symbol':<18} {'ORB':>5} {'In':>5} "
          f"{'Prem':>6} {'SL':>6} {'T1':>6} {'T1?':>4} "
          f"{'ExitP':>7} {'Rsn':<8} {'Out':>5} {'P&L':>9}  Running")
    print(f"  {S}")
    cum = 0
    for _, r in tdf.iterrows():
        cum  += r["pnl"]
        icon  = "✅" if r["pnl"] > 0 else "❌"
        t1m   = "🎯" if r["t1_hit"] else "  "
        rs    = {"STOPLOSS": "SL", "TARGET_2": "T2",
                 "TRAIL": "TSL", "EOD": "EOD", "—": "—"}.get(
                 r["exit_reason"], r["exit_reason"][:4])
        sym   = str(r.get("symbol", "—"))[:18]
        src   = "📡" if r.get("mode") == "SAMCO" else "  "
        print(f"  {r['date']:<12} {r['signal']:<5} {sym:<18} "
              f"{r['orb_range']:>5.0f} {r['entry_time']:>5} "
              f"{r['entry_prem']:>6.1f} {r['sl']:>6.1f} {r['t1']:>6.1f} "
              f"{t1m:>4} {r['exit_price']:>7.1f} {rs:<8} "
              f"{r['exit_time']:>5} ₹{r['pnl']:>+7,.0f}  "
              f"{icon}{src}  ₹{cum:>+8,.0f}")

    print(f"\n  SKIPPED DAYS")
    print(f"  {S}")
    if skipped:
        for d, reason in skipped:
            print(f"  {str(d):<12}  {reason}")
    else:
        print(f"  (none)")

    print(f"\n  MONTHLY P&L")
    print(f"  {S}")
    tdf["date"] = pd.to_datetime(tdf["date"])
    for p, mpnl in tdf.groupby(tdf["date"].dt.to_period("M"))["pnl"].sum().items():
        m   = tdf[tdf["date"].dt.to_period("M") == p]
        w   = (m["pnl"] > 0).sum()
        l   = (m["pnl"] <= 0).sum()
        bar = "█" * min(int(abs(mpnl) / 300), 28)
        sgn = "+" if mpnl >= 0 else "-"
        print(f"  {str(p):<10}  ₹{mpnl:>+10,.2f}  "
              f"{len(m)} trades  {w}W/{l}L  {sgn}{bar}")

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
    print(f"  {v}  |  ₹{tp:+,.2f}  |  {tot} trades  |  "
          f"WR {wr:.1f}%  |  Sharpe {sh:.2f}")
    print(f"  PF {pf:.2f}x  |  R:R {rr:.2f}x  |  "
          f"Max DD ₹{dd:,.0f}  |  Sortino {so:.2f}")
    if live_mode:
        print(f"\n  📡 {live_n} trade(s) used SAMCO real LTP  |  "
              f"{bsm_n} used BSM fallback")
    else:
        print(f"\n  💡 BSM + ₹{SPREAD:.0f} spread each side = "
              f"₹{SPREAD*2*ORDER_QTY:.0f}/trade round-trip cost.")
    print(f"{W}\n")

    tdf.to_csv(ROOT / "data" / "backtest_results.csv", index=False)
    print(f"  📁 Saved: data/backtest_results.csv\n")


# ═══════════════════════════════════════════════════════════
#  SECTION 6 — ENTRY POINT
# ═══════════════════════════════════════════════════════════

def _find_csv() -> str:
    data_dir = ROOT / "data"
    if not data_dir.exists():
        print("\n  ❌ data/ folder not found.\n"); sys.exit(1)
    csvs = [f for f in data_dir.glob("nifty_1min_*.csv") if "dataset" not in f.name]
    if not csvs:
        csvs = sorted(data_dir.glob("*.csv"), reverse=True)
    if not csvs:
        print("\n  ❌ No CSV found in data/\n  Run: python download_nifty_data.py\n")
        sys.exit(1)
    return str(sorted(csvs, reverse=True)[0])


def _load_df(csv_file: str):
    print(f"\n  📊 Loading {Path(csv_file).name}...")
    df = pd.read_csv(csv_file)
    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"])
    elif "date" in df.columns:
        print("  ⚠️  Daily OHLC file — needs 1-min data.")
        print("  Run: python download_nifty_data.py"); sys.exit(1)
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
    parser.add_argument("--file",  default=None,        help="1-min NIFTY CSV")
    parser.add_argument("--start", default=None,        help="Start YYYY-MM-DD")
    parser.add_argument("--end",   default=None,        help="End YYYY-MM-DD")
    parser.add_argument("--live",  action="store_true",
                        help="Use SAMCO live prices for today (BSM for history)")
    args = parser.parse_args()

    csv_file = args.file or _find_csv()
    if not args.file:
        print(f"\n  📂 Auto-detected: {csv_file}")

    if not Path(csv_file).exists():
        print(f"\n  ❌ File not found: {csv_file}\n"); sys.exit(1)

    df, data_info = _load_df(csv_file)

    if args.live:
        print(f"  ⚙️  Running (SAMCO live today + BSM history)...\n")
        trades, skipped = asyncio.run(
            run_backtest_live(df, args.start, args.end)
        )
        print_report(trades, skipped, data_info, live_mode=True)
    else:
        print(f"  ⚙️  Running backtest (BSM + ₹{SPREAD:.0f} spread)...\n")
        trades, skipped = run_backtest_offline(df, args.start, args.end)
        print_report(trades, skipped, data_info, live_mode=False)


if __name__ == "__main__":
    main()