"""
15-DELTA IRON CONDOR BACKTEST - WINDOWS VERSION
================================================
Monthly premium selling strategy on Nifty

Entry: Monthly cycles (every ~30 days)
Exit: 50% profit, 1.5x loss, or EOD

Windows-compatible with local file paths
"""
import pandas as pd
import numpy as np
from datetime import time
from pathlib import Path

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
# PARAMETERS
# ═══════════════════════════════════════════════════════════
CAPITAL = 50000
LOT_SIZE = 65
MULTIPLIER = 75
CYCLE_DAYS = 30
TARGET_PROFIT_PCT = 0.50
STOP_LOSS_MULT = 1.50
PLATFORM_CHARGES = 100
STT_RATE = 0.0015

# ═══════════════════════════════════════════════════════════
# PREMIUM CALCULATION
# ═══════════════════════════════════════════════════════════

def estimate_premium(spot, strike, opt_type, days=30):
    """Estimate option premium using simplified model"""
    # Intrinsic value
    if opt_type == "CE":
        intrinsic = max(0, spot - strike)
    else:
        intrinsic = max(0, strike - spot)
    
    if intrinsic > 0.1:  # ITM
        return intrinsic + intrinsic * 0.05
    
    # OTM pricing: use theta decay model
    base_vol = 0.15  # 15% volatility
    sqrt_t = np.sqrt(days / 365)
    
    # Time value ~ spot * vol * sqrt(T)
    time_val = spot * base_vol * sqrt_t
    
    # Discount by OTM distance
    if opt_type == "CE":
        otm_pct = (strike - spot) / spot
    else:
        otm_pct = (spot - strike) / spot
    
    # Further OTM = less premium
    discount = max(0.1, 1 - otm_pct * 5)
    premium = time_val * discount
    
    return max(5, premium)


def decay_premium(prem_entry, hours_passed, days=30):
    """Simulate premium decay"""
    total_hours = days * 6.5
    hours_left = total_hours - hours_passed
    
    if hours_left <= 0:
        return 0.1
    
    decay_factor = np.exp(-0.15 * hours_passed)
    current = prem_entry * decay_factor
    
    return max(0.1, current)


def get_strikes(spot):
    """Get Iron Condor strikes"""
    short_call = int(round((spot * 1.03) / 50) * 50)
    long_call = int(round((spot * 1.06) / 50) * 50)
    short_put = int(round((spot * 0.97) / 50) * 50)
    long_put = int(round((spot * 0.94) / 50) * 50)
    
    return {
        'call': short_call,
        'long_call': long_call,
        'put': short_put,
        'long_put': long_put
    }


# ═══════════════════════════════════════════════════════════
# BACKTEST
# ═══════════════════════════════════════════════════════════

days = sorted(df['datetime'].dt.date.unique())
trades = []

print("═" * 150)
print(f"{'Date':<12} {'Entry':<8} {'Call':<8} {'Put':<8} {'Prem Rx':<12} "
      f"{'Target':<12} {'Exit Prem':<12} {'Profit %':<10} {'Net P&L':<12} {'Exit':<12}")
print("─" * 150)

# Entry indices for 4 cycles
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
    
    # Build Iron Condor
    strikes = get_strikes(spot)
    
    # Get premiums
    sc_prem = estimate_premium(spot, strikes['call'], "CE", CYCLE_DAYS)
    lc_prem = estimate_premium(spot, strikes['long_call'], "CE", CYCLE_DAYS)
    sp_prem = estimate_premium(spot, strikes['put'], "PE", CYCLE_DAYS)
    lp_prem = estimate_premium(spot, strikes['long_put'], "PE", CYCLE_DAYS)
    
    net_prem = (sc_prem + sp_prem) - (lc_prem + lp_prem)
    
    if net_prem < 50:
        continue
    
    # Exit simulation
    target_prem = net_prem * (1 - TARGET_PROFIT_PCT)
    stop_loss_prem = net_prem * STOP_LOSS_MULT
    
    exit_prem = net_prem
    exit_reason = "EOD"
    
    for _, row in day_df[day_df['datetime'] > entry_time].iterrows():
        current_time = row['datetime'].time()
        hours_passed = (row['datetime'] - entry_time).total_seconds() / 3600
        
        # Current premium with decay
        curr_prem = decay_premium(net_prem, hours_passed, CYCLE_DAYS)
        
        # Peak theta at 2 PM
        if current_time >= time(14, 0):
            exit_prem = curr_prem
            exit_reason = "THETA_PEAK"
            break
        
        # Target hit
        if curr_prem <= target_prem:
            exit_prem = curr_prem
            exit_reason = "TARGET"
            break
        
        # Stop loss
        if curr_prem >= stop_loss_prem:
            exit_prem = curr_prem
            exit_reason = "STOP_LOSS"
            break
        
        # EOD
        if current_time >= time(15, 25):
            exit_prem = curr_prem
            exit_reason = "EOD"
            break
    
    # P&L
    prem_profit = net_prem - exit_prem
    gross_pnl = prem_profit * LOT_SIZE
    stt = (sc_prem + sp_prem) * LOT_SIZE * STT_RATE
    total_charges = PLATFORM_CHARGES + stt
    net_pnl = gross_pnl - total_charges
    profit_pct = (prem_profit / net_prem * 100) if net_prem > 0 else 0
    
    trades.append({
        'date': day,
        'call': strikes['call'],
        'put': strikes['put'],
        'net_prem': net_prem,
        'exit_prem': exit_prem,
        'net_pnl': net_pnl,
        'profit_pct': profit_pct,
        'exit_reason': exit_reason,
        'prem_profit': prem_profit
    })
    
    status = "✅" if net_pnl > 0 else "❌"
    target = net_prem * (1 - TARGET_PROFIT_PCT)
    
    print(f"  {day} | {entry_time.strftime('%H:%M')} | {strikes['call']:<8} | "
          f"{strikes['put']:<8} | ₹{net_prem:<10.0f} | ₹{target:<10.0f} | "
          f"₹{exit_prem:<10.0f} | {profit_pct:>7.1f}% | ₹{net_pnl:>10.0f} | "
          f"{status} ({exit_reason})")

# ═══════════════════════════════════════════════════════════
# STATISTICS
# ═══════════════════════════════════════════════════════════

print("\n" + "═" * 150)
print("🏆 15-DELTA IRON CONDOR BACKTEST RESULTS")
print("═" * 150)

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
    days_span = (trades[-1]['date'] - trades[0]['date']).days or 1
    months_span = days_span / 30
    cycles_per_month = len(trades) / months_span
    monthly_pnl = (total_net / len(trades)) * cycles_per_month if trades else 0
    monthly_return_pct = (monthly_pnl / CAPITAL) * 100
    
    print(f"\n📊 POSITION METRICS:")
    print(f"  Capital              : ₹{CAPITAL:,}")
    print(f"  Cycles               : {len(trades)}")
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
    
    print("\n" + "═" * 150)
    print("✅ VERDICT:")
    print("─" * 150)
    
    if wr >= 65:
        print(f"  ✅ EXCELLENT — {wr:.1f}% win rate (target: 65-75%)")
    elif wr >= 60:
        print(f"  ⚠️  GOOD — {wr:.1f}% win rate (meets minimum)")
    else:
        print(f"  ⚠️  BELOW TARGET — {wr:.1f}% win rate (needs {65-wr:.1f}%)")
    
    if monthly_return_pct >= 5:
        print(f"  ✅ PROFIT — {monthly_return_pct:.1f}% monthly return (5-8% goal)")
    elif monthly_return_pct >= 2:
        print(f"  ⚠️  MODEST — {monthly_return_pct:.1f}% monthly return")
    else:
        print(f"  ❌ LOW — {monthly_return_pct:.1f}% monthly return")
    
    print(f"\n  💡 Iron Condor vs ORB:")
    print(f"     • ORB Win Rate: 43.9% | Iron Condor: {wr:.1f}% | +{wr-43.9:.1f}%")
    print(f"     • ORB Monthly: -₹3,950 | Iron Condor: ₹{monthly_pnl:,.0f} | +₹{monthly_pnl+3950:,.0f}")
    print(f"     • Winner: {'Iron Condor ✅' if wr > 43.9 and total_net > 0 else 'Neither'}")
    
    print("═" * 150 + "\n")
else:
    print("❌ No valid cycles found\n")