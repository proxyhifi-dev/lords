"""
15-DELTA IRON CONDOR BACKTEST - WINDOWS VERSION (CORRECTED)
============================================================
Monthly premium selling strategy on Nifty

Entry: Monthly cycles (every ~30 days)
Exit: 50% profit, 1.5x loss, or EOD

Windows-compatible with local file paths
Integrated with Lords bot Iron Condor strategy
"""
import pandas as pd
import numpy as np
from datetime import time, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import sys
import os

# Add backend to path
sys.path.append(str(Path(__file__).parent / "backend"))

from backend.app.strategy.iron_condor_strategy import IronCondorStrategy
from backend.app.core.config_loader import get_settings

IST = ZoneInfo("Asia/Kolkata")

# ═══════════════════════════════════════════════════════════
# LOAD DATA - AUTO DETECT FILE LOCATION
# ═══════════════════════════════════════════════════════════

# Try multiple possible locations
possible_paths = [
    # Windows local paths
    Path(r"C:\Users\bollu\github\lords\data\nifty_1min_20260501.csv"),
    Path(r"C:\Users\bollu\Downloads\nifty_1min_20260501.csv"),
    Path(r".\data\nifty_1min_20260501.csv"),
    Path(r".\nifty_1min_20260501.csv"),
    
    # Linux paths (if running on WSL)
    Path("/mnt/user-data/uploads/nifty_1min_20260501.csv"),
    Path("./nifty_1min_20260501.csv"),
]

csv_file = None
for path in possible_paths:
    if path.exists():
        csv_file = path
        print(f"✅ Found data file: {csv_file}")
        break

if csv_file is None:
    print("❌ ERROR: Could not find nifty_1min_20260501.csv")
    print(f"\nSearched in:")
    for path in possible_paths:
        print(f"  • {path}")
    print(f"\n💡 Please ensure the file is in one of these locations:")
    print(f"   1. C:\\Users\\bollu\\github\\lords\\data\\")
    print(f"   2. C:\\Users\\bollu\\Downloads\\")
    print(f"   3. Current working directory: {Path.cwd()}")
    exit(1)

# Load data
df = pd.read_csv(csv_file)
df['datetime'] = pd.to_datetime(df['datetime'])

print(f"📊 Loading: Nifty 1-min data")
print(f"✅ Loaded {len(df):,} candles")
print(f"📅 Range: {df['datetime'].min()} to {df['datetime'].max()}\n")

# ═══════════════════════════════════════════════════════════
# LOAD BOT CONFIG & STRATEGY
# ═══════════════════════════════════════════════════════════

# Load settings from .env
settings = get_settings()
strategy = IronCondorStrategy()

print(f"🤖 Loaded Lords bot config:")
print(f"   Strategy: {settings.strategy_type}")
print(f"   Capital: ₹{settings.capital:,}")
print(f"   Lot Size: {settings.order_qty}")
print(f"   Short OTM %: {settings.ic_short_otm_pct*100:.1f}%")
print(f"   Target Profit: {settings.ic_target_profit_pct*100:.1f}%")
print(f"   Stop Loss Mult: {settings.ic_stop_loss_multiple:.1f}x")
print(f"   Platform Charges: ₹{settings.ic_platform_charges}")
print(f"   STT Rate: {settings.ic_stt_rate*100:.2f}%\n")

# ═══════════════════════════════════════════════════════════
# PARAMETERS (FROM BOT CONFIG)
# ═══════════════════════════════════════════════════════════
CAPITAL = settings.capital
LOT_SIZE = settings.order_qty
CYCLE_DAYS = 30  # Monthly cycles
TARGET_PROFIT_PCT = settings.ic_target_profit_pct
STOP_LOSS_MULT = settings.ic_stop_loss_multiple
PLATFORM_CHARGES = settings.ic_platform_charges
STT_RATE = settings.ic_stt_rate

# ═══════════════════════════════════════════════════════════
# BACKTEST
# ═══════════════════════════════════════════════════════════

days = sorted(df['datetime'].dt.date.unique())
trades = []

print("=" * 150)
print(f"{'Date':<12} {'Entry':<8} {'Call':<8} {'Put':<8} {'Prem Rx':<12} "
      f"{'Target':<12} {'Exit Prem':<12} {'Profit %':<10} {'Net P&L':<12} {'Exit':<12}")
print("─" * 150)

# Entry indices for 4 cycles (approximately every 30 days)
entry_indices = [0, 30, 60, 90]

for day_idx, day in enumerate(days):
    # Only enter on cycle days
    if day_idx not in entry_indices:
        continue
    
    day_df = df[df['datetime'].dt.date == day].copy()
    
    if len(day_df) < 100:
        continue
    
    # Find entry window (9:20-10:00)
    entry_window = day_df[
        (day_df['datetime'].dt.time >= time(9, 20)) & 
        (day_df['datetime'].dt.time < time(10, 0))
    ]
    
    if entry_window.empty:
        continue
    
    entry_candle = entry_window.iloc[0]
    entry_time = entry_candle['datetime']
    spot = entry_candle['close']
    
    # ✅ FIXED: Use corrected calculate_strikes() with proper keys
    strikes = strategy.calculate_strikes(spot)
    
    if not strikes:
        continue
    
    # Get premiums using strategy
    sc_prem = strategy.estimate_option_premium(spot, strikes['short_call'], "CE", CYCLE_DAYS)
    lc_prem = strategy.estimate_option_premium(spot, strikes['long_call'], "CE", CYCLE_DAYS)
    sp_prem = strategy.estimate_option_premium(spot, strikes['short_put'], "PE", CYCLE_DAYS)
    lp_prem = strategy.estimate_option_premium(spot, strikes['long_put'], "PE", CYCLE_DAYS)
    
    net_prem = (sc_prem + sp_prem) - (lc_prem + lp_prem)
    
    if net_prem < 50:
        continue
    
    # Exit simulation
    target_prem = net_prem * (1 - TARGET_PROFIT_PCT)
    stop_loss_prem = net_prem * STOP_LOSS_MULT
    
    exit_prem = net_prem
    exit_reason = "EOD"
    
    # ✅ FIXED: Iterate through day_df to simulate decay
    for _, row in day_df[day_df['datetime'] > entry_time].iterrows():
        current_time = row['datetime']
        current_time_obj = current_time.time()
        
        # ✅ FIXED: Use corrected estimate_current_premium() with datetime objects
        curr_prem = strategy.estimate_current_premium(net_prem, entry_time, current_time)
        
        # ✅ FIXED: Use corrected get_exit_reason() with proper signatures
        reason = strategy.get_exit_reason(entry_time, current_time, net_prem, curr_prem)
        
        if reason:
            exit_prem = curr_prem
            exit_reason = reason
            break
    
    # ✅ FIXED: Use corrected compute_pnl() with proper parameters
    pnl_result = strategy.compute_pnl(net_prem, exit_prem, LOT_SIZE)
    net_pnl = pnl_result['net_pnl']
    prem_profit = net_prem - exit_prem
    profit_pct = (prem_profit / net_prem * 100) if net_prem > 0 else 0
    
    trades.append({
        'date': day,
        'call': strikes['short_call'],
        'put': strikes['short_put'],
        'net_prem': net_prem,
        'exit_prem': exit_prem,
        'net_pnl': net_pnl,
        'profit_pct': profit_pct,
        'exit_reason': exit_reason,
        'prem_profit': prem_profit
    })
    
    status = "✅" if net_pnl > 0 else "❌"
    target = net_prem * (1 - TARGET_PROFIT_PCT)
    
    print(f"  {day} | {entry_time.strftime('%H:%M')} | {strikes['short_call']:<8} | "
          f"{strikes['short_put']:<8} | ₹{net_prem:<10.0f} | ₹{target:<10.0f} | "
          f"₹{exit_prem:<10.0f} | {profit_pct:>7.1f}% | ₹{net_pnl:>10.0f} | "
          f"{status} ({exit_reason})")

# ═══════════════════════════════════════════════════════════
# STATISTICS
# ═══════════════════════════════════════════════════════════

print("\n" + "=" * 150)
print("🏆 15-DELTA IRON CONDOR BACKTEST RESULTS")
print("=" * 150)

if trades:
    total_net = sum(t['net_pnl'] for t in trades)
    wins = sum(1 for t in trades if t['net_pnl'] > 0)
    wr = (wins / len(trades) * 100) if trades else 0
    
    win_trades = [t for t in trades if t['net_pnl'] > 0]
    loss_trades = [t for t in trades if t['net_pnl'] < 0]
    
    avg_win = sum(t['net_pnl'] for t in win_trades) / len(win_trades) if win_trades else 0
    avg_loss = sum(t['net_pnl'] for t in loss_trades) / len(loss_trades) if loss_trades else 0
    
    max_win = max([t['net_pnl'] for t in trades])
    max_loss = min([t['net_pnl'] for t in trades])
    
    # Monthly projection
    if len(trades) > 1:
        days_span = (trades[-1]['date'] - trades[0]['date']).days or 1
        months_span = max(days_span / 30, 1)
        cycles_per_month = len(trades) / months_span
    else:
        cycles_per_month = 1
    
    monthly_pnl = (total_net / len(trades)) * cycles_per_month if trades else 0
    monthly_return_pct = (monthly_pnl / CAPITAL) * 100
    
    print(f"\n📊 POSITION METRICS:")
    print(f"  Capital              : ₹{CAPITAL:,}")
    print(f"  Cycles               : {len(trades)}")
    if len(trades) > 1:
        print(f"  Time Span            : {days_span} days")
        print(f"  Cycles/Month         : {cycles_per_month:.2f}")
    
    print(f"\n📈 WIN RATE:")
    print(f"  Wins                 : {wins}/{len(trades)} ({wr:.1f}%)")
    print(f"  Losses               : {len(loss_trades)}/{len(trades)} ({100-wr:.1f}%)")
    
    print(f"\n💰 PROFIT & LOSS:")
    print(f"  Total Net P&L        : ₹{total_net:,.0f}")
    print(f"  Avg Win              : ₹{avg_win:,.0f}")
    print(f"  Avg Loss             : ₹{avg_loss:,.0f}")
    print(f"  Max Win              : ₹{max_win:,.0f}")
    print(f"  Max Loss             : ₹{max_loss:,.0f}")
    if avg_loss != 0:
        print(f"  Win/Loss Ratio       : {abs(avg_win/avg_loss):.2f}:1")
    
    print(f"\n📅 MONTHLY PROJECTION:")
    print(f"  Per Cycle            : ₹{total_net/len(trades) if trades else 0:,.0f}")
    print(f"  Monthly Expected     : ₹{monthly_pnl:,.0f}")
    print(f"  Monthly Return %     : {monthly_return_pct:.1f}%")
    print(f"  Annual Expected      : ₹{monthly_pnl*12:,.0f}")
    
    # Exit breakdown
    print(f"\n{'─'*150}")
    print("BY EXIT REASON:")
    for reason in ["TARGET", "THETA_PEAK", "STOP_LOSS", "EOD"]:
        r_trades = [t for t in trades if t['exit_reason'] == reason]
        if r_trades:
            r_wins = sum(1 for t in r_trades if t['net_pnl'] > 0)
            r_wr = (r_wins / len(r_trades) * 100) if r_trades else 0
            r_pnl = sum(t['net_pnl'] for t in r_trades)
            print(f"  {reason:<15}: {len(r_trades)} trades | WR: {r_wr:>5.1f}% | Net: ₹{r_pnl:>9,.0f}")
    
    print("\n" + "=" * 150)
    print("✅ VERDICT:")
    print("─" * 150)
    
    if wr >= 65:
        print(f"  ✅ EXCELLENT — {wr:.1f}% win rate (target: 65-75%)")
    elif wr >= 60:
        print(f"  ✅ GOOD — {wr:.1f}% win rate (meets minimum)")
    else:
        print(f"  ⚠️  BELOW TARGET — {wr:.1f}% win rate (needs {65-wr:.1f}%)")
    
    if monthly_return_pct >= 5:
        print(f"  ✅ PROFIT — {monthly_return_pct:.1f}% monthly return (5-8% goal)")
    elif monthly_return_pct >= 2:
        print(f"  ⚠️  MODEST — {monthly_return_pct:.1f}% monthly return")
    else:
        print(f"  ❌ LOW — {monthly_return_pct:.1f}% monthly return")
    
    if wr > 43.9 and total_net > 0:
        print(f"\n  💡 Iron Condor vs ORB:")
        print(f"     • ORB Win Rate: 43.9% | Iron Condor: {wr:.1f}% | +{wr-43.9:.1f}%")
        print(f"     • ORB Monthly: -₹3,950 | Iron Condor: ₹{monthly_pnl:,.0f} | +₹{monthly_pnl+3950:,.0f}")
        print(f"     • Winner: Iron Condor ✅")
    
    print("=" * 150 + "\n")
else:
    print("❌ No valid cycles found\n")