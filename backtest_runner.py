"""
Lords Bot — Backtest Runner
============================
Runs backtest using your EXACT bot config + real Samco 1-min data.

USAGE:
  python backtest_runner.py
  python backtest_runner.py --file data/nifty_1min_20260410.csv
  python backtest_runner.py --file data/nifty_1min_20260410.csv --start 2026-03-01 --end 2026-04-10
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime, time, timedelta
from math import log, sqrt, exp

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

try:
    import pandas as pd
    from scipy.stats import norm
except ImportError:
    print("\n❌  Run: pip install pandas scipy\n"); sys.exit(1)

# ── Load your real bot config ───────────────────────────
try:
    from backend.app.core.config_loader import get_settings
    s = get_settings()
    SL_PCT      = getattr(s, "stop_loss_pct",          0.25)
    T1_PCT      = getattr(s, "t1_pct",                 0.40)
    T2_PCT      = getattr(s, "t2_pct",                 1.00)
    TRAIL_PCT   = getattr(s, "trailing_pct",            0.25)
    MIN_PREM    = getattr(s, "min_entry_premium",       50.0)
    OTM_DIST    = getattr(s, "otm_distance",            1)
    ATR_MULT    = getattr(s, "orb_atr_multiplier",      1.0)
    BUF         = getattr(s, "breakout_buffer",         2.0)
    MIN_ORB     = getattr(s, "min_orb_range",           5.0)
    TREND_ON    = getattr(s, "trend_filter_enabled",    False)
    ORDER_QTY   = getattr(s, "order_qty",               50)
    MAX_LOSS    = getattr(s, "max_daily_loss",          5000.0)
    NO_ENTRY    = time(*map(int, getattr(s,"no_entry_after","13:30").split(":")))
    SQ_OFF      = time(*map(int, getattr(s,"square_off","15:10").split(":")))
    CFG_SOURCE  = "✅ Loaded from config_loader.py"
except Exception as e:
    SL_PCT=0.25; T1_PCT=0.40; T2_PCT=1.00; TRAIL_PCT=0.25
    MIN_PREM=50.0; OTM_DIST=1; ATR_MULT=1.0; BUF=2.0
    MIN_ORB=5.0; TREND_ON=False; ORDER_QTY=50; MAX_LOSS=5000.0
    NO_ENTRY=time(13,30); SQ_OFF=time(15,10)
    CFG_SOURCE = f"⚠️  Using defaults ({e})"


def bsm(S,K,T,opt="CE",r=0.065,iv=0.16):
    if T<=0: return max(S-K,0.05) if opt=="CE" else max(K-S,0.05)
    d1=(log(S/K)+(r+0.5*iv**2)*T)/(iv*sqrt(T))
    d2=d1-iv*sqrt(T)
    if opt=="CE": return max(S*norm.cdf(d1)-K*exp(-r*T)*norm.cdf(d2),0.05)
    return max(K*exp(-r*T)*norm.cdf(-d2)-S*norm.cdf(-d1),0.05)

def next_thu(dt):
    d=(3-dt.weekday())%7
    if d==0 and dt.hour>=15 and dt.minute>=30: d=7
    return dt+timedelta(days=d)

def atm(s,step=50): return int(round(s/step)*step)

def otm_strike(s,sig,steps=1,step=50):
    a=atm(s,step); return a+steps*step if sig=="CALL" else a-steps*step

def get_atr(day_df):
    orb=day_df[(day_df["datetime"].dt.time>=time(9,15))&
               (day_df["datetime"].dt.time<time(9,30))]
    if len(orb)<2: return 15.0
    return max((orb["high"]-orb["low"]).mean(), 1.0)


def run_backtest(df, start_date=None, end_date=None):
    if start_date:
        df=df[df["datetime"].dt.date>=pd.to_datetime(start_date).date()]
    if end_date:
        df=df[df["datetime"].dt.date<=pd.to_datetime(end_date).date()]
    if df.empty:
        print("❌ No data in range."); return [],[]

    trades=[]; skipped=[]

    for day in sorted(df["datetime"].dt.date.unique()):
        ddf=df[df["datetime"].dt.date==day].copy()

        orb=ddf[(ddf["datetime"].dt.time>=time(9,15))&
                (ddf["datetime"].dt.time<time(9,30))]
        if orb.empty: skipped.append((day,"No ORB data")); continue

        oh=orb["high"].max(); ol=orb["low"].min()
        orb_range=oh-ol
        atr=get_atr(ddf)

        if orb_range < MIN_ORB:
            skipped.append((day,f"ORB {orb_range:.0f}pts too small")); continue
        if orb_range < atr*ATR_MULT:
            skipped.append((day,f"ORB {orb_range:.0f}<{ATR_MULT}×ATR {atr:.0f} choppy")); continue

        bu=oh+BUF; bd=ol-BUF
        post=ddf[(ddf["datetime"].dt.time>=time(9,30))&
                 (ddf["datetime"].dt.time<NO_ENTRY)]

        sig=ets=esp=None
        for _,row in post.iterrows():
            # CANDLE CLOSE CONFIRMATION
            if row["close"]>bu:
                sig,ets,esp="CALL",row["datetime"],row["close"]; break
            elif row["close"]<bd:
                sig,ets,esp="PUT",row["datetime"],row["close"]; break

        if sig is None: skipped.append((day,"No confirmed breakout")); continue

        strike = otm_strike(esp, sig, steps=OTM_DIST)
        expiry = next_thu(ets)
        dte    = max((expiry.date()-day).days, 1)
        ot     = "CE" if sig=="CALL" else "PE"
        ep     = round(bsm(esp, strike, dte/365, ot), 2)

        if ep < MIN_PREM:
            skipped.append((day,f"Premium ₹{ep:.0f} < min ₹{MIN_PREM:.0f}")); continue

        sl  = round(ep*(1-SL_PCT), 2)
        t1  = round(ep*(1+T1_PCT), 2)
        t2  = round(ep*(1+T2_PCT), 2)
        t1q = ORDER_QTY//2
        t2q = ORDER_QTY-t1q

        maxp=ep; t1b=False; t1e=0.0
        xp=xr=xts=None

        for _,row in ddf[ddf["datetime"]>ets].iterrows():
            t=row["datetime"].time()
            dn=max((expiry.date()-row["datetime"].date()).days,0)
            curr=round(bsm(row["close"],strike,max(dn/365,1/365),ot),2)
            maxp=max(maxp,curr)
            trail=round(maxp*(1-TRAIL_PCT),2)

            if t>=SQ_OFF:      xp,xr,xts=curr,"EOD",row["datetime"]; break
            if curr<=sl:       xp,xr,xts=sl,"STOPLOSS",row["datetime"]; break
            if not t1b and curr>=t1: t1b=True; t1e=t1
            if t1b:
                if curr>=t2:   xp,xr,xts=t2,"TARGET_2",row["datetime"]; break
                if curr<trail: xp,xr,xts=trail,"TRAIL",row["datetime"]; break

        if xp is None:
            last=ddf[ddf["datetime"]>ets]
            if not last.empty:
                r=last.iloc[-1]; dn=max((expiry.date()-r["datetime"].date()).days,0)
                xp=round(bsm(r["close"],strike,max(dn/365,1/365),ot),2)
                xts=r["datetime"]
            else:
                xp=ep; xts=ets
            xr="—"

        pnl = round((t1e-ep)*t1q+(xp-ep)*t2q,2) if t1b \
              else round((xp-ep)*ORDER_QTY,2)

        # Time size label
        m=ets.hour*60+ets.minute
        sz="FULL" if m<=630 else ("MED" if m<=720 else "HALF")

        trades.append({
            "date":str(day),"signal":sig,
            "orb_range":round(orb_range,1),"atr":round(atr,1),
            "size":sz,"entry_time":ets.strftime("%H:%M"),
            "entry_spot":round(esp,2),"strike":strike,
            "entry_prem":ep,"sl":sl,"t1":t1,"t2":t2,
            "t1_hit":t1b,"t1_exit":round(t1e,2) if t1b else 0,
            "exit_time":xts.strftime("%H:%M") if xts else "—",
            "exit_price":xp,"exit_reason":xr,"pnl":pnl,
        })

    return trades, skipped


def print_report(trades, skipped, data_info):
    if not trades:
        print("\n  ❌ No trades generated.\n"); return

    tdf=pd.DataFrame(trades)
    tot=len(tdf)
    wins=tdf[tdf["pnl"]>0]; losses=tdf[tdf["pnl"]<=0]
    wr=len(wins)/tot*100; tp=tdf["pnl"].sum()
    aw=wins["pnl"].mean() if len(wins) else 0
    al=losses["pnl"].mean() if len(losses) else 0
    pf=abs(wins["pnl"].sum()/losses["pnl"].sum()) \
       if len(losses) and losses["pnl"].sum()!=0 else 99
    eq=tdf["pnl"].cumsum(); dd=(eq-eq.cummax()).min()
    rr=abs(aw/al) if al else 0
    sh=tdf["pnl"].mean()/tdf["pnl"].std()*sqrt(252/5) \
       if tdf["pnl"].std()>0 else 0
    neg=tdf[tdf["pnl"]<0]["pnl"]
    so=tdf["pnl"].mean()/neg.std()*sqrt(252/5) \
       if len(neg)>1 and neg.std()>0 else 0
    t1c=int(tdf["t1_hit"].sum())
    ec=tdf.groupby("exit_reason")["pnl"].agg(["count","mean","sum"])

    W="═"*72; S="─"*72

    print(f"\n{W}")
    print(f"  🏆  LORDS BOT — BACKTEST RESULTS")
    print(f"  Data   : {data_info}")
    print(f"  Config : {CFG_SOURCE}")
    print(f"{W}")

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
    for r,row in ec.iterrows():
        pct=row["count"]/tot*100
        bar="█"*min(int(row["count"]*2),22)
        icon="✅" if row["mean"]>0 else "❌"
        print(f"  {r:<12} {int(row['count']):>2} ({pct:>3.0f}%)  "
              f"avg ₹{row['mean']:>+8,.0f}  "
              f"total ₹{row['sum']:>+9,.0f}  {icon}{bar}")

    print(f"\n  SIGNAL BREAKDOWN")
    print(f"  {S}")
    for sg in ["CALL","PUT"]:
        s2=tdf[tdf["signal"]==sg]
        if len(s2):
            swr=(s2["pnl"]>0).mean()*100
            print(f"  {sg}  {len(s2):>2} trades  WR {swr:.0f}%  "
                  f"P&L ₹{s2['pnl'].sum():>+9,.0f}")

    print(f"\n  TRADE LOG")
    print(f"  {S}")
    print(f"  {'Date':<12} {'Sig':<5} {'ORB':>5} {'In':>5} "
          f"{'Prem':>6} {'SL':>6} {'T1':>6} {'T1?':>4} "
          f"{'ExitP':>7} {'Rsn':<8} {'Out':>5} {'P&L':>9}  Running")
    print(f"  {S}")
    cum=0
    for _,r in tdf.iterrows():
        cum+=r["pnl"]
        icon="✅" if r["pnl"]>0 else "❌"
        t1m="🎯" if r["t1_hit"] else "  "
        rs={"STOPLOSS":"SL","TARGET_2":"T2",
            "TRAIL":"TSL","EOD":"EOD","—":"—"}.get(
            r["exit_reason"], r["exit_reason"][:4])
        print(f"  {r['date']:<12} {r['signal']:<5} "
              f"{r['orb_range']:>5.0f} {r['entry_time']:>5} "
              f"{r['entry_prem']:>6.1f} {r['sl']:>6.1f} {r['t1']:>6.1f} "
              f"{t1m:>4} {r['exit_price']:>7.1f} {rs:<8} "
              f"{r['exit_time']:>5} ₹{r['pnl']:>+7,.0f}  "
              f"{icon}  ₹{cum:>+8,.0f}")

    print(f"\n  SKIPPED DAYS")
    print(f"  {S}")
    for d,reason in skipped:
        print(f"  {str(d):<12}  {reason}")

    print(f"\n  MONTHLY P&L")
    print(f"  {S}")
    tdf["date"]=pd.to_datetime(tdf["date"])
    for p,mpnl in tdf.groupby(tdf["date"].dt.to_period("M"))["pnl"].sum().items():
        m=tdf[tdf["date"].dt.to_period("M")==p]
        w=(m["pnl"]>0).sum(); l=(m["pnl"]<=0).sum()
        bar="█"*min(int(abs(mpnl)/300),28)
        sgn="+" if mpnl>=0 else "-"
        print(f"  {str(p):<10}  ₹{mpnl:>+10,.2f}  "
              f"{len(m)} trades  {w}W/{l}L  {sgn}{bar}")

    print(f"\n  SCALABILITY")
    print(f"  {S}")
    for lots in [1,2,3,5,10]:
        print(f"  {lots:>2} lot{'s' if lots>1 else ' '}  "
              f"est monthly ₹{tp/1.3*lots:>+10,.0f}  "
              f"max DD ₹{dd*lots:>+9,.0f}  "
              f"capital ₹{abs(dd)*lots*3:>9,.0f}")

    print(f"\n{W}")
    v="✅ PROFITABLE" if tp>0 else "❌ LOSS"
    print(f"  {v}  |  ₹{tp:+,.2f}  |  {tot} trades  |  "
          f"WR {wr:.1f}%  |  Sharpe {sh:.2f}")
    print(f"  PF {pf:.2f}x  |  R:R {rr:.2f}x  |  "
          f"Max DD ₹{dd:,.0f}  |  Sortino {so:.2f}")
    print(f"\n  ⚠️  Premiums via Black-Scholes IV=16%. Real fills vary ±15%.")
    print(f"{W}\n")

    tdf.to_csv(ROOT/"data"/"backtest_results.csv", index=False)
    print(f"  📁 Saved: data/backtest_results.csv\n")


def main():
    parser=argparse.ArgumentParser(
        description="Lords Bot Backtester — uses your real bot config"
    )
    parser.add_argument("--file",  default=None)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end",   default=None)
    args=parser.parse_args()

    # Auto-find CSV
    csv_file=args.file
    if not csv_file:
        data_dir=ROOT/"data"
        if data_dir.exists():
            # Prefer 1-min files over daily files
            csvs=[f for f in data_dir.glob("nifty_1min_*.csv")
                  if "dataset" not in f.name]
            if not csvs:
                csvs=sorted(data_dir.glob("*.csv"), reverse=True)
            if csvs:
                csv_file=str(sorted(csvs, reverse=True)[0])
                print(f"\n  📂 Auto-detected: {csv_file}")
            else:
                print("\n  ❌ No CSV found in data/ folder.")
                print("     Run: python download_nifty_data.py")
                print("     Choose option 2 (intraday) daily after 3:30 PM\n")
                sys.exit(1)
        else:
            print("\n  ❌ data/ folder not found.\n")
            sys.exit(1)

    if not Path(csv_file).exists():
        print(f"\n  ❌ File not found: {csv_file}\n")
        sys.exit(1)

    print(f"\n  📊 Loading {Path(csv_file).name}...")
    df=pd.read_csv(csv_file)

    # Handle both daily and 1-min files
    if "datetime" in df.columns:
        df["datetime"]=pd.to_datetime(df["datetime"])
    elif "date" in df.columns:
        print("  ⚠️  Daily OHLC file detected — ORB backtest needs 1-min data")
        print("  Run: python download_nifty_data.py → option 2 (intraday daily)")
        sys.exit(1)
    else:
        print("  ❌ No datetime/date column found"); sys.exit(1)

    df=df.sort_values("datetime").reset_index(drop=True)
    days=df["datetime"].dt.date.nunique()
    data_info=(f"{df['datetime'].min().date()} → {df['datetime'].max().date()} "
               f"| {len(df):,} candles | {days} days")
    print(f"  ✅ {data_info}")
    print(f"  ⚙️  Running backtest...\n")

    trades, skipped = run_backtest(df, args.start, args.end)
    print_report(trades, skipped, data_info)


if __name__ == "__main__":
    main()