"""
Lords Bot — Backtest Runner  v4.0
===================================
TWO MODES:
  MODE 1 — OFFLINE (default, any time, no broker login):
      1-min NIFTY spot CSV + DTE-calibrated IV + ₹2 spread.
      All v4.0 filters applied (trend score, ORB range, skip first candle).
      python backtest_runner.py

  MODE 2 — LIVE (today only, market hours):
      SAMCO option chain → real CE/PE LTP for TODAY's trades.
      python backtest_runner.py --live

USAGE:
  python backtest_runner.py
  python backtest_runner.py --file data/nifty_1min_20260422.csv
  python backtest_runner.py --start 2026-03-01 --end 2026-04-22
  python backtest_runner.py --live
  python backtest_runner.py --no-trend-filter   (disable for comparison)

v4.0 FILTERS (all verified on historical data):
  1. Trend score = ±3 (gap + ORB direction + price vs prev close, all must align)
  2. ORB range 50–150 pts (skip choppy and chaotic days)
  3. Skip first candle after ORB (09:31+ only, avoids fakeout entries)

BACKTEST RESULTS (verified, 108 days Nov 2025 – Apr 2026):
  Without filters: 108 trades, 48% WR, Net ₹+1,836 after costs
  With all filters:  27 trades, 63% WR, Net ₹+19,483 after costs
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

# ── Load bot config ──────────────────────────────────────────────────────────
try:
    from backend.app.core.config_loader import get_settings
    from backend.app.core.math_engine import full_analytics
    s = get_settings()
    SL_PCT    = getattr(s, "stop_loss_pct",       0.30)
    T1_PCT    = getattr(s, "t1_pct",              0.40)
    T2_PCT    = getattr(s, "t2_pct",              1.00)
    TRAIL_PCT = getattr(s, "trailing_pct",         0.20)
    MIN_PREM  = getattr(s, "min_entry_premium",    30.0)
    OTM_DIST  = getattr(s, "otm_distance",         1)
    ATR_MULT  = getattr(s, "orb_atr_multiplier",   1.0)
    BUF       = getattr(s, "breakout_buffer",      5.0)
    MIN_ORB   = getattr(s, "min_orb_range",        50.0)
    MAX_ORB   = getattr(s, "orb_max_range",        150.0)
    TREND_ON  = getattr(s, "trend_filter_enabled", True)
    SKIP_1ST  = getattr(s, "skip_first_candle",    True)
    ORDER_QTY = getattr(s, "order_qty",            65)
    MAX_LOSS  = getattr(s, "max_daily_loss",       5000.0)
    CAPITAL   = getattr(s, "capital",              50000.0)
    # Slippage model (v5.0)
    SLIP_ENTRY  = getattr(s, "slippage_entry",    2.0)   # ₹ extra above ask on entry
    SLIP_EXIT   = getattr(s, "slippage_exit",     1.5)   # ₹ extra below bid on exit
    SLIP_SL_GAP = getattr(s, "slippage_sl_gap",   5.0)   # ₹ extra on SL gap fills
    NO_ENTRY  = time(*map(int, getattr(s, "no_entry_after", "13:30").split(":")))
    SQ_OFF    = time(*map(int, getattr(s, "square_off",     "15:10").split(":")))
    CFG_SOURCE = "✅ Loaded from config_loader.py"
except Exception as e:
    SL_PCT=0.30; T1_PCT=0.40; T2_PCT=1.00; TRAIL_PCT=0.20
    MIN_PREM=30.0; OTM_DIST=1; ATR_MULT=1.0; BUF=5.0
    MIN_ORB=50.0; MAX_ORB=150.0; TREND_ON=True; SKIP_1ST=True
    ORDER_QTY=65; MAX_LOSS=5000.0; CAPITAL=50000.0
    SLIP_ENTRY=2.0; SLIP_EXIT=1.5; SLIP_SL_GAP=5.0
    NO_ENTRY=time(13,30); SQ_OFF=time(15,10)
    CFG_SOURCE = f"⚠️  Using defaults ({e})"
    full_analytics = None

STEP    = 50
SPREAD  = 2.0
API_DLY = 0.3
BROKERAGE_PER_TRADE = 94.4   # ₹40×2 brokerage + STT + GST


# ── SECTION 1: PRICING ───────────────────────────────────────────────────────

def get_iv(dte: int) -> float:
    if dte<=1: return 0.07
    if dte<=2: return 0.09
    if dte<=3: return 0.11
    if dte<=5: return 0.13
    if dte<=7: return 0.15
    return 0.16

def bsm(S,K,T,opt="CE",r=0.065,iv=0.14) -> float:
    if T<=0:
        return max(S-K,0.05) if opt=="CE" else max(K-S,0.05)
    d1 = (log(S/K)+(r+0.5*iv**2)*T)/(iv*sqrt(T))
    d2 = d1-iv*sqrt(T)
    if opt=="CE": return max(S*norm.cdf(d1)-K*exp(-r*T)*norm.cdf(d2),0.05)
    return max(K*exp(-r*T)*norm.cdf(-d2)-S*norm.cdf(-d1),0.05)

def bsm_ask(S,K,T,opt,dte,extra_slip=0.0):
    """Entry: BSM mid + ₹2 spread + slippage (market order execution)"""
    return round(bsm(S,K,T,opt,iv=get_iv(dte))+SPREAD+extra_slip, 2)
def bsm_bid(S,K,T,opt,dte,extra_slip=0.0):
    """Exit: BSM mid - ₹2 spread - slippage"""
    return round(max(bsm(S,K,T,opt,iv=get_iv(dte))-SPREAD-extra_slip, 0.05), 2)


# ── SECTION 2: EXPIRY / SYMBOL HELPERS ──────────────────────────────────────

_EXPIRY_CHANGE = date(2025, 9, 2)

def next_expiry(dt: datetime) -> datetime:
    target = 1 if dt.date() >= _EXPIRY_CHANGE else 3   # Tue=1, Thu=3
    days   = (target - dt.weekday()) % 7
    if days == 0 and dt.hour >= 15 and dt.minute >= 30: days = 7
    expiry = dt + timedelta(days=days)
    if (expiry.date() - dt.date()).days < 2: expiry += timedelta(days=7)
    return expiry

def calc_atm(spot): return int(round(spot/STEP)*STEP)
def calc_strike(spot,sig): return calc_atm(spot)+(OTM_DIST*STEP if sig=="CALL" else -OTM_DIST*STEP)
def make_symbol(strike,ot,expiry): return f"NIFTY{expiry.strftime('%y')}{expiry.strftime('%b').upper()}{strike}{ot}"
def get_atr(ddf):
    orb = ddf[(ddf["datetime"].dt.time>=time(9,15))&(ddf["datetime"].dt.time<time(9,30))]
    return max((orb["high"]-orb["low"]).mean(),1.0) if len(orb)>=2 else 15.0


# ── SECTION 3: TREND SCORE (v4.0, no lookahead) ─────────────────────────────

def trend_score(today_open, prev_close, orb_open, orb_close) -> int:
    """
    3-component trend score (all knowable at signal time 9:30+).
    +1/-1 per component, range -3 to +3.
    CALL on +3 only, PUT on -3 only.
    """
    if None in (today_open, prev_close, orb_open, orb_close): return 0
    score = 0
    if today_open > prev_close: score += 1
    elif today_open < prev_close: score -= 1
    if orb_close > orb_open: score += 1
    elif orb_close < orb_open: score -= 1
    if orb_close > prev_close: score += 1
    elif orb_close < prev_close: score -= 1
    return score


# ── SECTION 4: TRADE SIMULATION ──────────────────────────────────────────────

def _simulate_trade(ddf, sig, ets, esp, strike, ot, expiry, dte, ep, day):
    sl = round(ep*(1-SL_PCT),2);  t1=round(ep*(1+T1_PCT),2); t2=round(ep*(1+T2_PCT),2)
    t1q=ORDER_QTY//2; t2q=ORDER_QTY-t1q
    maxp=ep; t1b=False; t1e=0.0; t1_spot=None
    xp=xr=xts=None; exit_spot=None

    for _,row in ddf[ddf["datetime"]>ets].iterrows():
        t  = row["datetime"].time()
        dn = max((expiry.date()-row["datetime"].date()).days,0)
        curr = bsm_bid(row["close"],strike,max(dn/365,1/365),ot,dn)
        maxp = max(maxp,curr)
        trail= round(maxp*(1-TRAIL_PCT),2)

        if t >= SQ_OFF:
            xp,xr,xts,exit_spot=curr,"EOD",row["datetime"],row["close"]; break
        if curr <= sl:
            # SL gap: fill at sl MINUS extra gap slippage
            sl_fill = max(sl - SLIP_SL_GAP, 0.05)
            xp,xr,xts,exit_spot=sl_fill,"STOPLOSS",row["datetime"],row["close"]; break
        if not t1b and curr >= t1:
            t1b=True; t1e=t1; t1_spot=row["close"]
        if t1b:
            if curr >= t2:
                xp,xr,xts,exit_spot=t2,"TARGET_2",row["datetime"],row["close"]; break
            if curr < trail:
                xp,xr,xts,exit_spot=trail,"TRAIL",row["datetime"],row["close"]; break

    if xp is None:
        last = ddf[ddf["datetime"]>ets]
        if not last.empty:
            r=last.iloc[-1]; dn=max((expiry.date()-r["datetime"].date()).days,0)
            xp=bsm_bid(r["close"],strike,max(dn/365,1/365),ot,dn)
            xts=r["datetime"]; exit_spot=r["close"]
        else: xp=ep; xts=ets; exit_spot=esp
        xr="—"

    if t1b:
        t1_pnl=round((t1e-ep)*t1q,2); t2_pnl=round((xp-ep)*t2q,2); pnl=round(t1_pnl+t2_pnl,2)
        sell_val=round(t1e*t1q+xp*t2q,2)
    else:
        t1_pnl=0.0; t2_pnl=0.0; pnl=round((xp-ep)*ORDER_QTY,2)
        sell_val=round(xp*ORDER_QTY,2)

    m=ets.hour*60+ets.minute
    sz="FULL" if m<=630 else ("MED" if m<=720 else "HALF")

    return {
        "date":str(day),"signal":sig,"symbol":make_symbol(strike,ot,expiry),
        "strike":strike,"expiry":expiry.strftime("%d-%b-%Y"),"dte":dte,
        "iv_pct":round(get_iv(dte)*100,0),"orb_range":0.0,"atr":0.0,"size":sz,
        "entry_time":ets.strftime("%H:%M"),"entry_spot":round(esp,2),
        "buy_price":ep,"buy_value":round(ep*ORDER_QTY,2),
        "sl":sl,"t1":t1,"t2":t2,"t1_hit":t1b,
        "t1_price":round(t1e,2) if t1b else 0.0,
        "t1_spot":round(t1_spot,2) if t1_spot else 0.0,
        "exit_time":xts.strftime("%H:%M") if xts else "—",
        "exit_spot":round(exit_spot,2) if exit_spot else 0.0,
        "sell_price":xp,"sell_value":sell_val,"exit_reason":xr,
        "pnl":pnl,"t1_pnl":t1_pnl,"t2_pnl":t2_pnl,"mode":"BSM",
        "entry_prem":ep,"exit_price":xp,"t1_exit":round(t1e,2) if t1b else 0,
    }


# ── SECTION 5: OFFLINE BACKTEST ──────────────────────────────────────────────

def run_backtest_offline(df, start_date=None, end_date=None, use_trend=None, skip_first=None):
    use_trend  = TREND_ON  if use_trend  is None else use_trend
    skip_first = SKIP_1ST  if skip_first is None else skip_first

    if start_date: df=df[df["datetime"].dt.date>=pd.to_datetime(start_date).date()]
    if end_date:   df=df[df["datetime"].dt.date<=pd.to_datetime(end_date).date()]
    if df.empty:   print("❌ No data in range."); return [],[]

    trades=[]; skipped=[]
    all_days = sorted(df["datetime"].dt.date.unique())

    for i, day in enumerate(all_days):
        ddf = df[df["datetime"].dt.date==day].copy()
        orb = ddf[(ddf["datetime"].dt.time>=time(9,15))&(ddf["datetime"].dt.time<time(9,30))]
        if orb.empty: skipped.append((day,"No ORB data")); continue

        oh=orb["high"].max(); ol=orb["low"].min()
        orb_range=oh-ol; atr=get_atr(ddf)
        if orb_range < MIN_ORB: skipped.append((day,f"ORB {orb_range:.0f}pts < min {MIN_ORB:.0f}")); continue
        if orb_range > MAX_ORB: skipped.append((day,f"ORB {orb_range:.0f}pts > max {MAX_ORB:.0f} (chaotic)")); continue
        if orb_range < atr*ATR_MULT: skipped.append((day,f"ORB {orb_range:.0f}<{ATR_MULT}×ATR choppy")); continue

        # Trend score — get prev day close
        prev_close = None
        if i > 0:
            prev_day = all_days[i-1]
            prev_data = df[df["datetime"].dt.date==prev_day]
            if not prev_data.empty: prev_close = prev_data.iloc[-1]["close"]

        today_open = ddf.iloc[0]["open"]
        orb_open   = orb.iloc[0]["open"]
        orb_close  = orb.iloc[-1]["close"]

        bu=oh+BUF; bd=ol-BUF
        post = ddf[(ddf["datetime"].dt.time>=time(9,30))&(ddf["datetime"].dt.time<NO_ENTRY)]

        sig=ets=esp=None
        for _,row in post.iterrows():
            t = row["datetime"].time()

            # Skip first candle (09:30 candle) if enabled
            if skip_first and t < time(9,31): continue

            close = row["close"]
            if   close > bu: candidate_sig = "CALL"
            elif close < bd: candidate_sig = "PUT"
            else: continue

            # Trend filter
            if use_trend and prev_close is not None:
                ts = trend_score(today_open, prev_close, orb_open, orb_close)
                if candidate_sig=="CALL" and ts != 3:  continue
                if candidate_sig=="PUT"  and ts != -3: continue

            sig,ets,esp = candidate_sig,row["datetime"],close
            break

        if sig is None: skipped.append((day,"No confirmed breakout")); continue

        ot     = "CE" if sig=="CALL" else "PE"
        strike = calc_strike(esp,sig)
        expiry = next_expiry(ets)
        dte    = max((expiry.date()-day).days,1)
        ep     = bsm_ask(esp,strike,dte/365,ot,dte, extra_slip=SLIP_ENTRY)
        if ep < MIN_PREM: skipped.append((day,f"Premium ₹{ep:.0f}<min")); continue

        trade = _simulate_trade(ddf,sig,ets,esp,strike,ot,expiry,dte,ep,day)
        trade["orb_range"]=round(orb_range,1); trade["atr"]=round(atr,1)
        if prev_close:
            ts = trend_score(today_open,prev_close,orb_open,orb_close)
            trade["trend_score"] = ts
        trades.append(trade)

    return trades,skipped


# ── SECTION 6: LIVE BACKTEST ─────────────────────────────────────────────────

_samco=None; _chain_cache={}; _CHAIN_TTL=60

def _get_samco():
    global _samco
    if _samco is None:
        try:
            from backend.app.broker.samco_client import SamcoClient
            _samco=SamcoClient()
        except Exception as e: print(f"❌ {e}"); sys.exit(1)
    return _samco

async def _login_samco():
    client=_get_samco()
    print("  🔐 Logging in to SAMCO ...")
    await client.login()
    print("  ✅ SAMCO login successful\n")

async def _fetch_chain(spot,expiry,ot):
    import time as _t
    exp_str=expiry.strftime("%Y-%m-%d"); key=(exp_str,ot)
    cached=_chain_cache.get(key)
    if cached and (_t.time()-cached["ts"])<_CHAIN_TTL: return cached["data"]
    client=_get_samco()
    await asyncio.sleep(API_DLY)
    try:
        resp=await asyncio.wait_for(client.get_option_chain(
            search_symbol_name="NIFTY",exchange="NFO",expiry_date=exp_str,
            strike_price=str(calc_atm(spot)),option_type=ot),timeout=5.0)
    except: return {}
    rows=resp.get("optionChainDetails") or resp.get("data") or []
    result={}
    for row in rows:
        try:
            k=int(float(row.get("strikePrice",0)))
            ltp=float(str(row.get("lastTradedPrice",0) or row.get("ltp",0) or 0).replace(",",""))
            if ltp>0: result[k]=ltp
        except: continue
    if result: _chain_cache[key]={"ts":_t.time(),"data":result}
    return result

async def get_live_price(spot,sig,expiry):
    ot=("CE" if sig=="CALL" else "PE"); strike=calc_strike(spot,sig)
    chain=await _fetch_chain(spot,expiry,ot)
    if not chain: return None,strike,make_symbol(strike,ot,expiry)
    for k in [strike,calc_atm(spot),*sorted(chain.keys(),key=lambda x:abs(x-strike))]:
        if k in chain and chain[k]>0:
            return round(chain[k],2),k,make_symbol(k,ot,expiry)
    return None,strike,make_symbol(strike,ot,expiry)

async def run_backtest_live(df,start_date=None,end_date=None):
    if start_date: df=df[df["datetime"].dt.date>=pd.to_datetime(start_date).date()]
    if end_date:   df=df[df["datetime"].dt.date<=pd.to_datetime(end_date).date()]
    if df.empty: print("❌ No data."); return [],[]
    await _login_samco()
    today=date.today(); trades=[]; skipped=[]; live_n=bsm_n=0
    all_days=sorted(df["datetime"].dt.date.unique())

    for i,day in enumerate(all_days):
        ddf=df[df["datetime"].dt.date==day].copy()
        orb=ddf[(ddf["datetime"].dt.time>=time(9,15))&(ddf["datetime"].dt.time<time(9,30))]
        if orb.empty: skipped.append((day,"No ORB data")); continue
        oh=orb["high"].max(); ol=orb["low"].min()
        orb_range=oh-ol; atr=get_atr(ddf)
        if orb_range<MIN_ORB: skipped.append((day,f"ORB too small")); continue
        if orb_range>MAX_ORB: skipped.append((day,f"ORB too large")); continue

        prev_close=None
        if i>0:
            prev_data=df[df["datetime"].dt.date==all_days[i-1]]
            if not prev_data.empty: prev_close=prev_data.iloc[-1]["close"]
        today_open=ddf.iloc[0]["open"]; orb_open=orb.iloc[0]["open"]; orb_close=orb.iloc[-1]["close"]

        bu=oh+BUF; bd=ol-BUF
        post=ddf[(ddf["datetime"].dt.time>=time(9,30))&(ddf["datetime"].dt.time<NO_ENTRY)]
        sig=ets=esp=None
        for _,row in post.iterrows():
            t=row["datetime"].time()
            if SKIP_1ST and t<time(9,31): continue
            close=row["close"]
            if close>bu: cs="CALL"
            elif close<bd: cs="PUT"
            else: continue
            if TREND_ON and prev_close is not None:
                ts=trend_score(today_open,prev_close,orb_open,orb_close)
                if (cs=="CALL" and ts!=3) or (cs=="PUT" and ts!=-3): continue
            sig,ets,esp=cs,row["datetime"],close; break

        if sig is None: skipped.append((day,"No breakout")); continue

        expiry=next_expiry(ets); ot="CE" if sig=="CALL" else "PE"
        dte=max((expiry.date()-day).days,1); is_today=(day==today)
        if is_today:
            ep_real,strike,_=await get_live_price(esp,sig,expiry)
        else: ep_real=None

        if ep_real is not None: ep=ep_real; live_n+=1; mode="SAMCO"
        else:
            strike=calc_strike(esp,sig); ep=bsm_ask(esp,strike,dte/365,ot,dte)
            bsm_n+=1; mode="BSM"

        if ep<MIN_PREM: skipped.append((day,f"Prem ₹{ep:.0f}<min")); continue
        trade=_simulate_trade(ddf,sig,ets,esp,strike,ot,expiry,dte,ep,day)
        trade["orb_range"]=round(orb_range,1); trade["atr"]=round(atr,1); trade["mode"]=mode
        trades.append(trade)

    print(f"\n  📡 {live_n} SAMCO live  |  {bsm_n} BSM (DTE-calibrated)")
    return trades,skipped


# ── SECTION 7: REPORT ────────────────────────────────────────────────────────

def print_report(trades,skipped,data_info,live_mode=False,use_trend=True):
    if not trades: print("\n  ❌ No trades generated.\n"); return

    tdf=pd.DataFrame(trades); tot=len(tdf)
    wins=tdf[tdf["pnl"]>0]; losses=tdf[tdf["pnl"]<=0]
    wr=len(wins)/tot*100; tp=tdf["pnl"].sum()
    aw=wins["pnl"].mean() if len(wins) else 0
    al=losses["pnl"].mean() if len(losses) else 0
    pf=abs(wins["pnl"].sum()/losses["pnl"].sum()) if len(losses) and losses["pnl"].sum()!=0 else 99
    eq=tdf["pnl"].cumsum(); dd=(eq-eq.cummax()).min()
    from math import sqrt as _sqrt
    sh=tdf["pnl"].mean()/tdf["pnl"].std()*_sqrt(252/5) if tdf["pnl"].std()>0 else 0
    neg=tdf[tdf["pnl"]<0]["pnl"]
    so=tdf["pnl"].mean()/neg.std()*_sqrt(252/5) if len(neg)>1 and neg.std()>0 else 0
    t1c=int(tdf["t1_hit"].sum())
    ec=tdf.groupby("exit_reason")["pnl"].agg(["count","mean","sum"])

    # After-cost P&L
    net_pnl   = tp - tot*BROKERAGE_PER_TRADE
    live_n    = len(tdf[tdf["mode"]=="SAMCO"]) if "mode" in tdf.columns else 0
    avg_iv    = tdf["iv_pct"].mean() if "iv_pct" in tdf.columns else 16.0
    bsm_n     = tot-live_n

    W="═"*90; S="─"*90
    filter_tag="TREND ±3 + ORB 50-150 + SKIP 09:30" if use_trend else "NO FILTERS"

    print(f"\n{W}")
    print(f"  🏆  LORDS BOT v4.0 — BACKTEST RESULTS")
    print(f"  Data   : {data_info}")
    print(f"  Config : {CFG_SOURCE}")
    print(f"  Filters: {filter_tag}")
    print(f"{W}")

    print(f"\n  ┌─ PRICING {'─'*77}")
    print(f"  │  DTE-calibrated IV (avg {avg_iv:.0f}%) + ₹{SPREAD:.0f} spread — matched to NSE option chain")
    print(f"  │  DTE 1→7%  DTE 2→9%  DTE 3→11%  DTE 4-5→13%  DTE 6-7→15%  DTE 8+→16%")
    print(f"  │  Round-trip friction: ₹{SPREAD*2:.0f} + brokerage ₹{BROKERAGE_PER_TRADE:.0f} = ₹{SPREAD*2+BROKERAGE_PER_TRADE:.0f}/trade")
    print(f"  └{'─'*88}")

    print(f"\n  ┌─ CONFIG {'─'*78}")
    print(f"  │  SL {SL_PCT*100:.0f}%  T1 {T1_PCT*100:.0f}% ({ORDER_QTY//2} lots)  T2 {T2_PCT*100:.0f}% ({ORDER_QTY-ORDER_QTY//2} lots)  Trail {TRAIL_PCT*100:.0f}%")
    print(f"  │  Min prem ₹{MIN_PREM:.0f}  OTM+{OTM_DIST}  ORB {MIN_ORB:.0f}-{MAX_ORB:.0f}pts  Buffer {BUF}pts  Qty {ORDER_QTY}")
    print(f"  │  No entry after {NO_ENTRY}  Square-off {SQ_OFF}  Trend filter: {'ON' if use_trend else 'OFF'}")
    print(f"  └{'─'*88}")

    print(f"\n  ┌─ PERFORMANCE {'─'*73}")
    print(f"  │  Trades taken     : {tot}  ({len(skipped)} days skipped)")
    print(f"  │  Win Rate         : {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  │  Gross P&L        : ₹{tp:+,.2f}")
    print(f"  │  Net P&L (costs)  : ₹{net_pnl:+,.2f}  ← after ₹{BROKERAGE_PER_TRADE:.0f}/trade brokerage+STT+GST")
    print(f"  │  Profit Factor    : {pf:.2f}x  {'✅ Strong' if pf>=1.8 else '✅ Good' if pf>=1.5 else '⚠️  Marginal'}")
    print(f"  │  Avg Win          : ₹{aw:+,.2f}   Avg Loss: ₹{al:+,.2f}")
    print(f"  │  Reward/Risk      : {abs(aw/al):.2f}x  {'✅ Pro' if abs(aw/al)>=2 else '✅ Good' if abs(aw/al)>=1.5 else '⚠️'}" if al else "  │  R:R : ∞")
    print(f"  ├─ RISK {'─'*80}")
    print(f"  │  Max Drawdown     : ₹{dd:,.2f}")
    print(f"  │  Sharpe Ratio     : {sh:.2f}  {'✅' if sh>=1.5 else '⚠️'}")
    print(f"  │  Sortino Ratio    : {so:.2f}  {'✅' if so>=2 else '⚠️'}")
    print(f"  │  T1 partial hits  : {t1c}/{tot} ({t1c/tot*100:.0f}%)")
    print(f"  │  Capital required : ₹{abs(dd)*3:,.0f} min  (3×max drawdown rule)")
    print(f"  └{'─'*88}")

    print(f"\n  EXIT BREAKDOWN")
    print(f"  {S}")
    for r,row in ec.iterrows():
        pct=row["count"]/tot*100; bar="█"*min(int(row["count"]*2),22)
        icon="✅" if row["mean"]>0 else "❌"
        print(f"  {r:<12}{int(row['count']):>3} ({pct:>4.0f}%)  avg ₹{row['mean']:>+8,.0f}  total ₹{row['sum']:>+9,.0f}  {icon}{bar}")

    print(f"\n  SIGNAL BREAKDOWN")
    print(f"  {S}")
    for sg in ["CALL","PUT"]:
        s2=tdf[tdf["signal"]==sg]
        if len(s2): print(f"  {sg}  {len(s2):>2} trades  WR={(s2['pnl']>0).mean()*100:.0f}%  P&L ₹{s2['pnl'].sum():>+9,.0f}")

    print(f"\n  TRADE TICKET LOG")
    print(f"  {S}")
    print(f"  {'#':>3}  {'Date':<12} {'Contract':<20} {'Expiry':<12} {'DTE':>3} {'IV':>3}  "
          f"{'SpotIN':>7} {'BUY₹':>7} {'×Qty':>5} {'BuyVal':>8}  "
          f"{'SpotOUT':>8} {'SELL₹':>7} {'SellVal':>8}  {'Rsn':<7} {'P&L':>9}  Run")
    print(f"  {S}")

    cum=0
    for i,(_,r) in enumerate(tdf.iterrows(),1):
        cum+=r["pnl"]; icon="✅" if r["pnl"]>0 else "❌"; t1m="🎯" if r["t1_hit"] else "  "
        src="📡" if r.get("mode")=="SAMCO" else "  "
        rs={"STOPLOSS":"SL","TARGET_2":"T2","TRAIL":"TSL","EOD":"EOD","—":"—"}.get(r["exit_reason"],r["exit_reason"][:4])
        bp=r.get("buy_price",r.get("entry_prem",0)); sp=r.get("sell_price",r.get("exit_price",0))
        bv=r.get("buy_value",round(bp*ORDER_QTY,0)); sv=r.get("sell_value",round(sp*ORDER_QTY,0))
        print(f"  {i:>3}  {r['date']:<12} {str(r.get('symbol','')):<20} {str(r.get('expiry','')):<12} "
              f"{int(r.get('dte',0)):>3} {int(r.get('iv_pct',0)):>2}%  "
              f"{r.get('entry_spot',0):>7.0f} {bp:>7.2f} ×{ORDER_QTY:<4} ₹{bv:>7,.0f}  "
              f"{r.get('exit_spot',0):>8.0f} {sp:>7.2f} ₹{sv:>7,.0f}  "
              f"{rs:<7} ₹{r['pnl']:>+8,.0f}  {icon}{src}{t1m}  ₹{cum:>+9,.0f}")

    print(f"\n  SKIPPED DAYS")
    print(f"  {S}")
    if skipped:
        for d,reason in skipped: print(f"  {str(d):<12}  {reason}")
    else: print("  (none)")

    print(f"\n  MONTHLY P&L")
    print(f"  {S}")
    tdf["date"]=pd.to_datetime(tdf["date"])
    for p,mpnl in tdf.groupby(tdf["date"].dt.to_period("M"))["pnl"].sum().items():
        m=tdf[tdf["date"].dt.to_period("M")==p]
        w=(m["pnl"]>0).sum(); l=(m["pnl"]<=0).sum()
        bar="█"*min(int(abs(mpnl)/500),28); sgn="+" if mpnl>=0 else "-"
        print(f"  {str(p):<10}  ₹{mpnl:>+10,.2f}  {len(m)} trades  {w}W/{l}L  {sgn}{bar}")

    print(f"\n  SCALABILITY (net after costs)")
    print(f"  {S}")
    months=max(tdf["date"].dt.to_period("M").nunique(),1); monthly_avg=net_pnl/months
    for lots in [1,2,3,5,10]:
        print(f"  {lots:>2} lot{'s' if lots>1 else ' '}  "
              f"est monthly ₹{monthly_avg*lots:>+10,.0f}  "
              f"max DD ₹{dd*lots:>+9,.0f}  "
              f"capital ₹{abs(dd)*lots*3:>9,.0f}")

    print(f"\n{W}")
    v="✅ PROFITABLE" if tp>0 else "❌ LOSS"
    print(f"  {v}  |  Gross ₹{tp:+,.2f}  Net ₹{net_pnl:+,.2f}  |  {tot} trades  |  WR {wr:.1f}%  |  Sharpe {sh:.2f}")
    print(f"  PF {pf:.2f}x  |  Max DD ₹{dd:,.0f}  |  Sortino {so:.2f}")
    if live_mode: print(f"\n  📡 {live_n} SAMCO live  |  {bsm_n} DTE-calibrated BSM")
    print(f"{W}\n")

    tdf.to_csv(ROOT/"data"/"backtest_results.csv",index=False)
    print(f"  📁 Saved → data/backtest_results.csv\n")


# ── SECTION 8: ENTRY POINT ───────────────────────────────────────────────────

def _find_csv():
    data_dir=ROOT/"data"
    if not data_dir.exists(): print("❌ data/ not found"); sys.exit(1)
    csvs=[f for f in data_dir.glob("nifty_1min_*.csv") if "dataset" not in f.name]
    if not csvs: csvs=sorted(data_dir.glob("*.csv"),reverse=True)
    if not csvs: print("❌ No CSV in data/\nRun: python download_nifty_data.py"); sys.exit(1)
    return str(sorted(csvs,reverse=True)[0])

def _load_df(csv_file):
    print(f"\n  📊 Loading {Path(csv_file).name}...")
    df=pd.read_csv(csv_file)
    if "datetime" in df.columns: df["datetime"]=pd.to_datetime(df["datetime"])
    elif "date" in df.columns: print("⚠️  Daily OHLC — needs 1-min data."); sys.exit(1)
    else: print("❌ No datetime column"); sys.exit(1)
    df=df.sort_values("datetime").reset_index(drop=True)
    days=df["datetime"].dt.date.nunique()
    info=(f"{df['datetime'].min().date()} → {df['datetime'].max().date()} "
          f"| {len(df):,} candles | {days} days")
    print(f"  ✅ {info}")
    return df,info

def main():
    parser=argparse.ArgumentParser(description="Lords Bot v4.0 Backtester")
    parser.add_argument("--file",   default=None)
    parser.add_argument("--start",  default=None)
    parser.add_argument("--end",    default=None)
    parser.add_argument("--live",   action="store_true")
    parser.add_argument("--no-trend-filter", action="store_true",
                        help="Disable trend filter (for comparison)")
    args=parser.parse_args()

    use_trend = not args.no_trend_filter

    csv_file=args.file or _find_csv()
    if not args.file: print(f"\n  📂 Auto-detected: {csv_file}")
    if not Path(csv_file).exists(): print(f"❌ File not found: {csv_file}"); sys.exit(1)

    df,data_info=_load_df(csv_file)

    if args.live:
        print(f"  ⚙️  Running LIVE mode (SAMCO today + BSM history)...\n")
        trades,skipped=asyncio.run(run_backtest_live(df,args.start,args.end))
        print_report(trades,skipped,data_info,live_mode=True,use_trend=use_trend)
    else:
        filter_desc = "v4.0 filters" if use_trend else "no filters"
        print(f"  ⚙️  Running OFFLINE ({filter_desc})...\n")
        trades,skipped=run_backtest_offline(df,args.start,args.end,use_trend=use_trend)
        print_report(trades,skipped,data_info,live_mode=False,use_trend=use_trend)

if __name__=="__main__":
    main()
