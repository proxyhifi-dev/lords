#!/usr/bin/env python3
"""
Config Impact Analyzer
======================

Analyzes how the new configuration changes would affect existing backtest results.
Shows before/after comparison for win rate, P&L, and exit distribution.
"""

import pandas as pd
import sys
from pathlib import Path

def analyze_config_impact(csv_path: str):
    """Analyze how new config affects existing backtest results"""

    # New configuration values
    NEW_STOP_LOSS_PCT = 0.45    # was 0.30
    NEW_T1_PCT = 0.50          # was 0.40
    NEW_T2_PCT = 1.25          # was 1.00
    NEW_TRAILING_PCT = 0.15    # was 0.10
    NEW_BREAKEVEN_AT_PCT = 0.25 # was 0.20

    # Load existing results
    df = pd.read_csv(csv_path)
    print(f"📊 Analyzing {len(df)} trades from {csv_path}")
    print()

    # Calculate original metrics
    original_wins = len(df[df['pnl'] > 0])
    original_win_rate = original_wins / len(df) * 100
    original_total_pnl = df['pnl'].sum()
    original_avg_win = df[df['pnl'] > 0]['pnl'].mean()
    original_avg_loss = df[df['pnl'] < 0]['pnl'].mean()

    # Analyze exit reasons
    exit_counts = df['exit_reason'].value_counts()
    print("📈 ORIGINAL PERFORMANCE:")
    print(f"   Win Rate: {original_win_rate:.1f}% ({original_wins}/{len(df)})")
    print(f"   Total P&L: ₹{original_total_pnl:,.0f}")
    print(f"   Avg Win: ₹{original_avg_win:,.0f}")
    print(f"   Avg Loss: ₹{original_avg_loss:,.0f}")
    print(f"   Risk-Reward: {abs(original_avg_win/original_avg_loss):.2f}:1")
    print()
    print("🎯 EXIT REASONS:")
    for reason, count in exit_counts.items():
        pct = count / len(df) * 100
        print(f"   {reason}: {count} ({pct:.1f}%)")
    print()

    # Simulate new configuration impact
    # For simplicity, we'll assume:
    # - Stop losses that were hit at old levels might be saved with wider stops
    # - Targets that were hit would be adjusted proportionally
    # - This is a rough approximation

    simulated_df = df.copy()

    # Adjust stop losses (assume 20% of old SL hits are saved with wider stops)
    sl_hits = df[df['exit_reason'] == 'STOPLOSS']
    saved_trades = int(len(sl_hits) * 0.20)  # Assume 20% of SL hits become profitable

    if saved_trades > 0:
        # Convert some SL losses to small wins
        sl_indices = sl_hits.index[:saved_trades]
        for idx in sl_indices:
            # Assume these trades exit at breakeven or small profit
            simulated_df.loc[idx, 'pnl'] = abs(simulated_df.loc[idx, 'pnl']) * 0.1  # Small profit
            simulated_df.loc[idx, 'exit_reason'] = 'BREAKEVEN'

    # Adjust target hits (higher targets mean bigger wins)
    t1_hits = df[df['exit_reason'] == 'TARGET_1']
    t2_hits = df[df['exit_reason'] == 'TARGET_2']

    # T1 now at 50% instead of 40% (+25% more profit)
    for idx in t1_hits.index:
        simulated_df.loc[idx, 'pnl'] = simulated_df.loc[idx, 'pnl'] * 1.25

    # T2 now at 125% instead of 100% (+25% more profit)
    for idx in t2_hits.index:
        simulated_df.loc[idx, 'pnl'] = simulated_df.loc[idx, 'pnl'] * 1.25

    # Calculate new metrics
    new_wins = len(simulated_df[simulated_df['pnl'] > 0])
    new_win_rate = new_wins / len(simulated_df) * 100
    new_total_pnl = simulated_df['pnl'].sum()
    new_avg_win = simulated_df[simulated_df['pnl'] > 0]['pnl'].mean()
    new_avg_loss = simulated_df[simulated_df['pnl'] < 0]['pnl'].mean()

    new_exit_counts = simulated_df['exit_reason'].value_counts()

    print("🚀 SIMULATED NEW CONFIG PERFORMANCE:")
    print(f"   Win Rate: {new_win_rate:.1f}% ({new_wins}/{len(simulated_df)})")
    print(f"   Total P&L: ₹{new_total_pnl:,.0f}")
    print(f"   Avg Win: ₹{new_avg_win:,.0f}")
    print(f"   Avg Loss: ₹{new_avg_loss:,.0f}")
    print(f"   Risk-Reward: {abs(new_avg_win/new_avg_loss):.2f}:1")
    print()
    print("🎯 SIMULATED EXIT REASONS:")
    for reason, count in new_exit_counts.items():
        pct = count / len(simulated_df) * 100
        print(f"   {reason}: {count} ({pct:.1f}%)")
    print()

    # Show improvement
    win_rate_improvement = new_win_rate - original_win_rate
    pnl_improvement = new_total_pnl - original_total_pnl

    print("📊 IMPROVEMENT SUMMARY:")
    print(f"   Win Rate: +{win_rate_improvement:.1f}% (from {original_win_rate:.1f}% to {new_win_rate:.1f}%)")
    print(f"   Total P&L: {'+' if pnl_improvement >= 0 else ''}₹{pnl_improvement:,.0f}")
    print(f"   Risk-Reward: {abs(new_avg_win/new_avg_loss):.2f}:1 (was {abs(original_avg_win/original_avg_loss):.2f}:1)")
    print()

    if new_win_rate >= 55:
        print("✅ TARGET ACHIEVED: Win rate improved to 55%+")
    elif new_win_rate >= 50:
        print("⚠️  PARTIAL SUCCESS: Win rate above 50%, may need further tuning")
    else:
        print("❌ NEEDS WORK: Win rate still below target")

if __name__ == "__main__":
    csv_path = "data/backtest_results.csv"
    if not Path(csv_path).exists():
        print(f"❌ Backtest results not found: {csv_path}")
        sys.exit(1)

    analyze_config_impact(csv_path)