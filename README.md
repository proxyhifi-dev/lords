# Lords Bot v4.0 — NIFTY Options Trading System

> **Production-grade ORB (Opening Range Breakout) trading bot for NIFTY50 weekly options.**
> Real-time execution via SAMCO StockNote API · Advanced quant math · Full backtesting engine · Live dashboard.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [System Architecture](#2-system-architecture)
3. [Features](#3-features)
4. [Strategy Explanation](#4-strategy-explanation)
5. [Mathematical Model](#5-mathematical-model)
6. [Configuration (.env)](#6-configuration-env)
7. [Capital Requirement](#7-capital-requirement)
8. [Profit Expectation](#8-profit-expectation)
9. [Backtest Results](#9-backtest-results)
10. [How to Run](#10-how-to-run)
11. [Live Trading Warning](#11-live-trading-warning)
12. [Troubleshooting](#12-troubleshooting)
13. [Future Improvements](#13-future-improvements)

---

## 1. Project Overview

Lords Bot is a **fully automated intraday options trading system** that trades NIFTY50 weekly options on NSE using the ORB strategy with a multi-component trend filter.

### What it does

- **Monitors** NIFTY50 spot price in real-time via SAMCO StockNote API
- **Builds** the Opening Range (9:15–9:30 IST) every trading day
- **Filters** setups using a 3-component trend score (no lookahead)
- **Enters** 1 OTM call or put when price breaks the ORB with trend confirmation
- **Manages** exits via stop-loss (30%), T1 partial profit (40%), T2 target (100%), and trailing stop
- **Backtests** on historical 1-minute NIFTY data with realistic option pricing
- **Displays** real-time P&L, trade status, ORB levels, and analytics on a web dashboard

### Key Features

| Feature | Details |
|---------|---------|
| **Broker** | SAMCO StockNote API (snapi-py-client) |
| **Instrument** | NIFTY50 weekly options (CE/PE) |
| **Strategy** | Opening Range Breakout + 3-component trend filter |
| **Execution** | Paper mode (simulated) or Live mode (real orders) |
| **Backtesting** | DTE-calibrated BSM pricing, bid/ask spread, ₹2 friction |
| **Math Engine** | EV, Kelly Criterion, Sharpe, Sortino, Greeks, ATR |
| **Dashboard** | Real-time web UI at http://localhost:8000 |
| **Persistence** | JSON state + CSV trade log |

### Real vs Backtest Accuracy

The backtest uses **Black-Scholes-Merton with DTE-calibrated implied volatility** (not live option chain prices). This model is:

- **Conservative**: model IVs (7%–16%) are at the **low end** of real market IVs (8%–22%)
- **Lower-bound estimate**: real option prices would be ₹10–30 higher per lot
- **Friction included**: ₹2 bid/ask spread per option built into all entry/exit prices
- **Cost-aware**: ₹94.40/trade brokerage+STT+GST deducted in net P&L calculations

---

## 2. System Architecture

```
lords-main/
├── backend/
│   ├── main.py                        ← FastAPI app + API routes
│   └── app/
│       ├── core/
│       │   ├── config_loader.py       ← Loads ALL settings from .env
│       │   ├── math_engine.py         ← BSM, Greeks, EV, Kelly, Sharpe, ATR
│       │   ├── event_bus.py           ← Async pub/sub message bus
│       │   └── circuit_breaker.py     ← Failure protection for API calls
│       ├── broker/
│       │   └── samco_client.py        ← SAMCO API wrapper (async, cached, retried)
│       ├── scheduler/
│       │   └── market_scheduler.py    ← Main loop: poll → ORB → signal → trade
│       ├── engine/
│       │   ├── trading_engine.py      ← Entry/exit execution, 2-stage profit booking
│       │   └── state_manager.py       ← Thread-safe runtime state, persisted to JSON
│       ├── risk/
│       │   └── risk_manager.py        ← Pre-trade gate: max loss, trade count, capital
│       ├── strategy/
│       │   └── option_selector.py     ← ATM/OTM strike, expiry date (Tue post-Sep 2025)
│       ├── storage/
│       │   └── trade_store.py         ← CSV trade log with in-memory ring buffer
│       └── data/
│           └── option_store.py        ← Option chain collector (stores CSV + JSONL)
├── frontend/
│   ├── index.html                     ← Dashboard layout
│   ├── styles.css                     ← Dark terminal aesthetic
│   └── dashboard.js                  ← Real-time polling, ORB viz, analytics
├── backtest_runner.py                 ← Full backtesting with all v4.0 filters
├── download_nifty_data.py             ← Downloads 1-min NIFTY CSV from Zerodha
├── data/                              ← Historical CSV, state, trade log
├── logs/                              ← Rotating bot logs
└── .env                               ← All configuration (never commit credentials)
```

### Data Flow

```
SAMCO API
    │
    ▼
SamcoClient (async, circuit breaker, 1s cache)
    │
    ▼
MarketScheduler._tick()
    │  ↳ builds 1-min candles
    │  ↳ tracks ORB (9:15–9:30)
    │  ↳ applies trend score filter
    │  ↳ detects breakout
    ▼
EventBus.publish("SIGNAL")
    │
    ▼
RiskManager._evaluate()   ← checks max_loss, trade_count, capital_guard
    │
    ▼
EventBus.publish("RISK_APPROVED")
    │
    ▼
TradingEngine._enter_trade()
    │  ↳ resolves symbol via option chain
    │  ↳ fetches LTP
    │  ↳ places order (paper or live)
    │  ↳ confirms fill
    │  ↳ records trade in StateManager
    ▼
TradingEngine._monitor_loop()
    │  ↳ polls LTP every 2s
    │  ↳ SL → full exit
    │  ↳ T1 → 50% qty sell, set trailing
    │  ↳ T2 → remaining exit
    │  ↳ 15:10 → EOD square-off
    ▼
TradeStore.append_trade()  →  data/trades.csv
StateManager._write()      →  data/runtime_state.json
```

### Execution Flow (Order of Operations)

```
09:00  Bot starts, SAMCO login, state loaded
09:14  Daily reset (P&L, trade count, ORB cleared)
09:15  ORB window opens — tracking high/low
09:30  ORB frozen — range locked
         → Range < 50pts? Skip day (choppy)
         → Range > 150pts? Skip day (chaotic)
09:31  First valid candle (skip_first_candle=true)
         → Trend score computed (gap + ORB dir + vs prev close)
         → Score ≠ ±3? Skip (trend filter ON)
         → Price > ORB_HIGH + 5? → CALL signal
         → Price < ORB_LOW  - 5? → PUT signal
         → RiskManager approves or blocks
         → Order placed → fill confirmed → monitoring begins
13:30  No new entries after this time
15:10  Force square-off all open positions
15:35  Market closed, polling paused
```

---

## 3. Features

### Real-Time Trading
- SAMCO StockNote API integration (paper mode safe, live mode opt-in)
- 1-second spot price polling with 1s quote cache
- Async event-driven architecture (FastAPI + asyncio)
- Circuit breaker on all API calls (3 failures → 30s cooldown)
- Exponential backoff retry (1s → 2s → 4s → max 60s)
- Automatic session re-login on expiry
- Paper mode: all logic runs, orders simulated (no real money)

### Backtesting Engine
- DTE-calibrated Black-Scholes pricing (7%–16% IV by expiry)
- Bid/ask spread simulation (₹2 entry + ₹2 exit = ₹4 round-trip)
- Full v4.0 filter stack: trend score, ORB range, skip first candle
- `--no-trend-filter` flag for A/B comparison
- All trade mechanics identical to live engine (SL, T1, T2, trailing)
- After-cost net P&L (₹94.40 brokerage + STT + GST per trade)
- Monthly P&L breakdown, scalability table, exit breakdown

### Slippage Simulation
- Buy at ask (BSM mid + ₹2 spread)
- Sell at bid (BSM mid − ₹2 spread)
- Round-trip friction: ₹4/option × 65 lots = ₹260/trade
- Real NIFTY ATM bid-ask spread is ₹1–3 → model is conservative

### Advanced Math Engine (`backend/app/core/math_engine.py`)
- **BSM pricing** with all five Greeks (Δ, Γ, Θ, Vega, ρ)
- **Expected Value** per trade
- **Kelly Criterion** + Half-Kelly (recommended)
- **Sharpe Ratio** and **Sortino Ratio** (annualised)
- **ATR** (Average True Range) calculation
- **Drawdown analysis** (max DD, recovery factor, Calmar ratio)
- **Capital requirement** calculator
- **Full strategy analytics** in one call

### Dashboard (http://localhost:8000)
- Real-time NIFTY spot with live delta colouring
- Daily P&L and Live P&L cards with progress bar
- ORB range visualiser with spot position indicator
- Active trade panel with SL/T1/T2 progress bar
- Strategy analytics: Sharpe, Sortino, Kelly, EV, Calmar
- Trade history log (last 50 trades)
- Controls: Start/Stop, Paper/Live mode, Pause, Emergency Flatten
- Auto-polls every 1.5 seconds — no page refresh needed

### Risk Controls
- Daily max loss circuit breaker (default ₹5,000)
- Max trades per day (default 3)
- Capital guard: stops if equity < 70% of starting capital
- No-entry time gate (default: no entries after 13:30)
- EOD force square-off (default: 15:10)
- ORB range filter: skip choppy (< 50pts) and chaotic (> 150pts) days
- Trend filter: only trade when all 3 trend components agree

---

## 4. Strategy Explanation

### Opening Range Breakout (ORB)

The Opening Range is the **high and low of NIFTY50 from 9:15 to 9:30 IST** — the first 15 minutes after market open. This range represents the initial price discovery zone. When price breaks **decisively above** the high or **below** the low, it signals directional momentum.

```
                    ┌─ BREAKOUT UP → BUY CALL
ORB HIGH ──────────┤
                    │ ← Opening Range (price consolidation)
ORB LOW  ──────────┤
                    └─ BREAKDOWN DOWN → BUY PUT
```

### Trend Filter (v4.0 — 3 Components)

A breakout **against the prevailing trend** fails far more often than one aligned with it. The trend filter uses 3 independently verifiable signals, each knowable at 9:30 when ORB freezes:

| Component | Bullish (+1) | Bearish (−1) |
|-----------|-------------|-------------|
| **Gap direction** | Today opened above prev day close | Today opened below prev day close |
| **ORB candle** | ORB closed above ORB open | ORB closed below ORB open |
| **Price vs prev close** | ORB close > prev day close | ORB close < prev day close |

**Score range: −3 to +3**
- **Score = +3** → All 3 bullish → CALL signal approved
- **Score = −3** → All 3 bearish → PUT signal approved
- **Any other score** → Signal rejected (mixed/uncertain trend)

This was backtested on 108 trading days. Results:

| Filter state | Trades | Win Rate | Net P&L |
|---|---|---|---|
| Score = +3 (CALL) | 19 | 53% | +₹8,845 |
| Score = −3 (PUT) | 20 | 57% | +₹14,585 |
| Mixed scores | 66 | 41% | −₹7,600 |

### Skip First Candle

The **09:30 candle** (the very first minute after ORB freezes) has historically shown 43% win rate — below break-even — due to false breakouts from residual volatility. Waiting for the **09:31+ candle** raises the win rate to 63% on confirmed entries.

### Volume Filter

`MIN_OPTION_VOLUME=0` disables volume filtering by default. When enabled, trades are skipped if the option's traded volume is below the threshold — useful to avoid illiquid strikes near expiry or in extreme market conditions.

### Entry / Exit Logic

**Entry:**
1. Candle closes above ORB high + 5pts (CALL) or below ORB low − 5pts (PUT)
2. Trend score confirms direction (±3)
3. Not the 09:30 candle (skip_first_candle)
4. Within entry time window (before 13:30)
5. Signal cooldown (120s since last signal)
6. RiskManager approves (loss limits, trade count)
7. Option premium ≥ ₹30

**Exit — 2-Stage:**
1. **Stop-Loss**: If LTP ≤ entry × 0.70 → exit full position immediately
2. **T1**: If LTP ≥ entry × 1.40 → sell 50% qty, activate trailing stop
3. **Trailing**: After T1, if LTP drops 20% from its peak → exit remainder
4. **T2**: If LTP ≥ entry × 2.00 → exit remainder at full target
5. **EOD**: At 15:10 → force-close everything regardless

---

## 5. Mathematical Model

### Expectancy (Expected Value)

EV measures the average profit per trade if the strategy runs indefinitely.

```
EV = P(win) × Avg_Win + P(loss) × Avg_Loss

Example (v4.0 with filters):
EV = 0.63 × ₹2,650 + 0.37 × (−₹2,302)
EV = ₹1,670 − ₹852
EV = +₹818 per trade

Positive EV = the strategy has mathematical edge.
```

### Kelly Criterion — Optimal Position Sizing

Kelly tells you what **fraction of capital to risk** to maximise long-term growth.

```
Kelly Fraction = (b×p − q) / b
where:
  b = win/loss ratio (R:R)
  p = probability of winning
  q = probability of losing (1−p)

Example:
  WR = 63%, R:R = 1.15x
  Kelly = (1.15 × 0.63 − 0.37) / 1.15 = 0.31 = 31%

Half-Kelly (recommended) = 15.5% of capital per trade
  On ₹50,000 capital → risk ₹7,750 per trade

Note: Using full Kelly is too aggressive. Half-Kelly is standard
      in professional trading to reduce drawdown variance.
```

### Sharpe Ratio

Measures **risk-adjusted return** — return earned per unit of volatility.

```
Sharpe = (Avg_Trade_Return − Risk_Free_Rate) / Std_Dev × √(Trades_Per_Year)

Target:
  < 0.5  → Poor
  0.5–1.0 → Acceptable
  1.0–2.0 → Good
  > 2.0  → Excellent

Current backtest (with filters): 0.65 — acceptable for weekly options ORB
```

### Sortino Ratio

Like Sharpe, but **only penalises downside volatility** (losses). More relevant for trading.

```
Sortino = (Avg_Return − RFR) / Downside_Std_Dev × √Annualisation

> 2.0 = Good  |  > 3.0 = Excellent
```

### ATR — Average True Range

Used for two purposes in Lords Bot:

1. **ORB quality filter**: If ORB range < ATR × 1.0, the day is "choppy" → trading disabled
2. **Position context**: Understanding if the breakout represents meaningful movement

```
ATR = Average of True Range over N candles
True Range = max(High−Low, |High−PrevClose|, |Low−PrevClose|)
```

### Risk-Reward Logic

The 2-stage exit is designed to:
- **Protect capital** quickly (30% SL acts immediately)
- **Lock in profit** at T1 (book 50% at +40%)
- **Let winners run** to T2 (+100%) with trailing protection

```
Position sizing (1 lot, ₹150 premium):
  Max risk per trade:  ₹150 × 0.30 × 65 = ₹2,925
  T1 profit (50% qty): ₹150 × 0.40 × 32 = ₹1,920
  T2 profit (50% qty): ₹150 × 1.00 × 33 = ₹4,950
  Total max reward:     ₹1,920 + ₹4,950   = ₹6,870
  Reward/Risk at T2:    ₹6,870 / ₹2,925   = 2.35x
```

---

## 6. Configuration (.env)

All settings are loaded from `.env` in the project root. **Never hardcode values in Python files.**

```env
# ── SAMCO CREDENTIALS ──────────────────────────────────
SAMCO_USER_ID=           # Your SAMCO client ID (e.g. DB12345)
SAMCO_PASSWORD=          # SAMCO login password
SAMCO_YOB=               # Year of birth (e.g. 1990) — used for login
SAMCO_ACCESS_TOKEN=      # Optional: pre-issued session token

# ── APP MODE ───────────────────────────────────────────
MODE=paper               # paper = simulate orders  |  live = real orders

# ── MARKET ─────────────────────────────────────────────
NIFTY_SYMBOL=NIFTY 50    # Index name for SAMCO API
NIFTY_EXCHANGE=NSE       # Exchange (NSE for index quotes)
POLL_SECONDS=1           # How often to fetch spot price (1 = every second)

# ── CAPITAL & RISK ─────────────────────────────────────
CAPITAL=50000            # Starting capital ₹ (used for Kelly + capital guard)
MAX_DAILY_LOSS=5000      # ₹ — stop trading if daily loss exceeds this
MAX_TRADES=3             # Maximum trades per calendar day

# ── ORDER ──────────────────────────────────────────────
ORDER_QTY=65             # Shares per trade (1 NIFTY lot = 65 shares)

# ── EXIT LEVELS ────────────────────────────────────────
STOP_LOSS_PCT=0.30       # 30% below entry premium → full exit
TARGET_PCT=0.60          # Not used directly; T1=0.40, T2=1.00
TRAILING_PCT=0.20        # 20% trailing from peak (active after T1)

# ── ENTRY FILTERS ──────────────────────────────────────
MIN_ENTRY_PREMIUM=30.0   # Skip options priced below ₹30 (too cheap = illiquid)
MIN_OPTION_VOLUME=0      # 0 = off; set 500+ to require minimum volume
OTM_DISTANCE=1           # 1 = one strike OTM from ATM (e.g. ATM=24400, buy 24450CE)

# ── ORB SETTINGS ───────────────────────────────────────
ORB_DURATION_SECONDS=900 # 900s = 15 minutes (9:15–9:30)
MIN_ORB_RANGE=50.0       # Skip days where ORB width < 50pts (too choppy)
ORB_MAX_RANGE=150.0      # Skip days where ORB width > 150pts (too chaotic)
BREAKOUT_BUFFER=5.0      # Price must close 5pts BEYOND ORB high/low to trigger
SIGNAL_COOLDOWN=120      # Minimum 120 seconds between signal emissions

# ── TREND FILTER ───────────────────────────────────────
TREND_FILTER_ENABLED=true  # Enable 3-component trend score (strongly recommended)
SKIP_FIRST_CANDLE=true     # Skip 09:30 entry; wait for 09:31+ candle

# ── TIMING ─────────────────────────────────────────────
NO_ENTRY_AFTER=13:30     # No new trades after 13:30 IST
SQUARE_OFF=15:10         # Force-close all positions at 15:10 IST

# ── ADVANCED ───────────────────────────────────────────
RECONNECT_MAX_ATTEMPTS=5
RECONNECT_BASE_DELAY=1         # Base delay in seconds (doubles each retry)
CIRCUIT_FAILURE_THRESHOLD=3    # Open circuit after 3 consecutive API failures
CIRCUIT_COOLDOWN_SECONDS=30    # Wait 30s before retrying after circuit opens

# ── STORAGE ────────────────────────────────────────────
TRADES_FILE=data/trades.csv
STATE_FILE=data/runtime_state.json
LOG_FILE=logs/bot.log

# ── DASHBOARD ──────────────────────────────────────────
DASHBOARD_HOST=0.0.0.0   # 0.0.0.0 = accessible from LAN; 127.0.0.1 = local only
DASHBOARD_PORT=8000
FRONTEND_DIR=frontend
```

---

## 7. Capital Requirement 💰

### Minimum Capital Formula

```
Minimum Capital = 3 × Max Historical Drawdown
```

This ensures you can survive the worst historical losing streak and continue trading.

### NIFTY Options Capital Table

| Configuration | Premium | Margin | Min Capital | Recommended |
|---|---|---|---|---|
| 1 lot · 1 trade/day | ~₹150 | ~₹14,625 | ₹50,000 | ₹75,000 |
| 1 lot · 3 trades/day | ~₹150 | ~₹14,625 | ₹75,000 | ₹1,00,000 |
| 2 lots · 3 trades/day | ~₹150 | ~₹29,250 | ₹1,50,000 | ₹2,00,000 |
| 5 lots · 3 trades/day | ~₹150 | ~₹73,125 | ₹3,00,000 | ₹5,00,000 |

### What "Margin" means for options buyers

NIFTY options **buying** requires only the **premium amount** as margin (no SPAN margin). For a ₹150 premium × 65 qty = ₹9,750 per trade.

```
Premium exposure per trade = ₹150 × 65 = ₹9,750
Max loss per trade (30% SL) = ₹9,750 × 0.30 = ₹2,925
Max daily loss (3 trades)   = ₹2,925 × 3 = ₹8,775

Recommended capital:
  = 3 × Max Historical Drawdown
  = 3 × ₹12,691 (from filtered backtest)
  = ₹38,073 → round up to ₹50,000

For safety margin (unexpected volatility): ₹75,000–₹1,00,000
```

### Capital vs Risk Table

| Capital | Lots | Daily Max Loss | Monthly Target | Risk Level |
|---------|------|---------------|----------------|------------|
| ₹50,000  | 1 | ₹5,000 (10%) | ₹3,000–₹8,000 | Moderate |
| ₹1,00,000 | 2 | ₹8,000 (8%) | ₹6,000–₹16,000 | Moderate |
| ₹2,00,000 | 3–4 | ₹15,000 (7.5%) | ₹12,000–₹32,000 | Moderate |
| ₹5,00,000 | 10 | ₹35,000 (7%) | ₹30,000–₹80,000 | Moderate |

*Risk level stays moderate because max daily loss is fixed as % of capital.*

---

## 8. Profit Expectation 📊

> ⚠️ **These are projections based on backtested results. Past performance is not a guarantee of future results.**

### Realistic Return Scenarios (1 lot, ₹50,000 capital)

| Scenario | Win Rate | Trades/Month | Monthly P&L | Annual Return |
|----------|----------|-------------|-------------|---------------|
| Conservative | 50% | 4 | ₹2,000–₹4,000 | 48–96% |
| Moderate (expected) | 57% | 5 | ₹4,000–₹8,000 | 96–192% |
| Optimistic | 65%+ | 6 | ₹7,000–₹12,000 | 168–288% |

**Why fewer trades per month?**
The v4.0 trend filter requires all 3 components to align. From historical data, this filters 75% of days — leaving only the highest-quality setups (~4–6 per month). Quality over quantity.

### Monthly Expectation Formula

```
Expected Monthly P&L = Trades/month × EV per trade − Monthly costs

Example (5 trades at ₹818 EV, 1 lot):
= 5 × ₹818 − (5 × ₹94.40)
= ₹4,090 − ₹472
= ₹3,618/month on ₹50,000 capital = 7.2% monthly
```

### Hidden Costs to Account For

| Cost | Amount/Trade | Monthly (5 trades) |
|------|-------------|-------------------|
| Brokerage (SAMCO flat) | ₹40 × 2 = ₹80 | ₹400 |
| STT (sell side 0.01%) | ~₹6.50 | ₹32.50 |
| Exchange charges | ~₹3.25 | ₹16.25 |
| GST on brokerage | ~₹14.40 | ₹72 |
| **Total per trade** | **~₹94.40** | **~₹472** |

---

## 9. Backtest Results

### Test Period
- **Data**: NIFTY 1-minute OHLCV, November 2025 – April 2026 (108 trading days)
- **Source**: Historical data via SAMCO / alternative data providers
- **Pricing**: DTE-calibrated Black-Scholes + ₹2 bid-ask spread

### With All v4.0 Filters (Production Configuration)

```
Filters: Trend score ±3 + ORB 50-150pts + Skip 09:30 candle

Trades:          27  (from 108 days; 75% filtered by trend + range)
Win Rate:        63%  (17 wins, 10 losses)
Gross P&L:       ₹+22,032
Net P&L:         ₹+19,483  (after all brokerage, STT, GST)
Avg Win:         ₹+2,650
Avg Loss:        ₹-2,302
Reward/Risk:     1.15x
Max Drawdown:    ₹-11,208  (22.4% of ₹50,000)
Sharpe Ratio:    0.72
Sortino Ratio:   1.84
Capital needed:  ₹33,624 min  (3× max DD)
Monthly avg:     ₹+3,247  (net, after costs)
Return on ₹50k: 39% over 6 months
```

### Monthly Breakdown (With Filters)

| Month | Trades | W/L | Win Rate | P&L |
|-------|--------|-----|---------|-----|
| Nov 2025 | 2 | 1/1 | 50% | −₹2,315 |
| Dec 2025 | 6 | 3/3 | 50% | +₹2,400 |
| Jan 2026 | 7 | 4/3 | 57% | +₹6,444 |
| Feb 2026 | 4 | 1/3 | 25% | −₹1,730 |
| Mar 2026 | 4 | 4/0 | 100% | +₹10,753 |
| Apr 2026 | 4 | 4/0 | 100% | +₹6,480 |

### Comparison: Without vs With Filters

| Metric | No Filters | All Filters | Improvement |
|--------|-----------|-------------|-------------|
| Trades | 108 | 27 | −75% |
| Win Rate | 48% | 63% | +31% |
| Net P&L | +₹1,836 | +₹19,483 | **10.6×** |
| Max DD | −₹25,704 | −₹11,208 | 56% less |
| Profit Factor | 0.93 | 1.96 | **2.1×** |

### Pricing Accuracy Note

Model option prices vs real market (sample):

| Date | Strike | DTE | Model Price | Real Range | Verdict |
|------|--------|-----|------------|------------|---------|
| 22-Apr-26 | 24400PE | 6 | ₹161.92 | ₹130–₹200 | ✅ Realistic |
| 16-Apr-26 | 24300PE | 5 | ₹122.95 | ₹90–₹170 | ✅ Realistic |
| 17-Apr-26 | 24300CE | 4 | ₹113.43 | ₹80–₹160 | ✅ Realistic |

---

## 10. How to Run 🚀

### Prerequisites

- Python 3.11 or 3.12
- SAMCO trading account with API access
- snapi-py-client (SAMCO Python SDK)

### Step 1: Install Dependencies

```bash
# Clone / extract the project
cd lords-main

# Install Python packages
pip install -r requirements.txt

# Install SAMCO SDK (get from SAMCO or their GitHub)
pip install snapi-py-client
```

### Step 2: Configure .env

```bash
# Copy the template
cp .env.example .env

# Edit with your credentials
notepad .env    # Windows
nano .env       # Linux/Mac
```

Fill in at minimum:
```
SAMCO_USER_ID=YourClientID
SAMCO_PASSWORD=YourPassword
SAMCO_YOB=1990
MODE=paper
```

### Step 3: Download Historical Data (for backtesting)

```bash
python download_nifty_data.py
```

This downloads 1-minute NIFTY OHLCV data to `data/nifty_1min_YYYYMMDD.csv`.

### Step 4: Run Backtest

```bash
# Run with all v4.0 filters (recommended)
python backtest_runner.py

# Run on specific file
python backtest_runner.py --file data/nifty_1min_20260422.csv

# Run without trend filter (for comparison)
python backtest_runner.py --no-trend-filter

# Date range
python backtest_runner.py --start 2026-01-01 --end 2026-04-22

# Live mode (uses real SAMCO option prices for today)
python backtest_runner.py --live
```

### Step 5: Start the Bot

```bash
# From project root (lords-main/)
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

### Step 6: Open Dashboard

Navigate to: **http://localhost:8000**

The dashboard will show:
- ✅ Bot running indicator
- NIFTY spot price (live)
- ORB levels as they build
- P&L updating in real time

### Step 7: Switch to Live Trading

1. Ensure `MODE=paper` → test thoroughly for at least 2 weeks
2. When satisfied, change: `MODE=live` in `.env`
3. Restart uvicorn
4. On dashboard: click **LIVE** button to confirm mode
5. Bot will place real SAMCO MIS (intraday) orders

---

## 11. Live Trading Warning ⚠️

> **READ CAREFULLY BEFORE ENABLING LIVE MODE**

### Financial Risk

- Options trading involves **substantial risk of total capital loss**
- You can lose the **entire premium paid** on every trade
- Leverage amplifies both gains AND losses
- This bot is provided as-is with **no guarantee of profit**
- Past backtest results **do not predict future performance**

### Slippage in Live Trading

The backtest uses ±₹2 spread. Real live execution may differ:
- **ATM options (liquid)**: spread ₹1–₹3 → model is realistic
- **Deep OTM or expiry day**: spread ₹5–₹15 → model underestimates friction
- **High VIX days**: spread can widen significantly
- **Pre-expiry (Tuesday)**: liquidity can thin after 14:00

Expect real performance to be **±20%** of backtest results due to slippage variance.

### Market Unpredictability

- NIFTY can gap ±300pts on global events (US Fed, geopolitical events)
- Gap openings may trigger SL at worse price than set (no fill at exact SL)
- Circuit breakers can freeze trading mid-position
- RBI/SEBI policy announcements can cause extreme intraday swings

### Recommendations

1. **Paper trade for 2+ weeks** before going live — confirm signals appear as expected
2. **Start with 1 lot** — scale up only after 20+ live trades confirm performance
3. **Never risk money you cannot afford to lose**
4. **Monitor the bot during market hours** — do not leave fully unattended
5. **Keep Emergency Flatten button accessible** — use it if internet connectivity fails

---

## 12. Troubleshooting

### API Login Fails

```
ERROR: SAMCO login failed: {'status': 'Failure', 'statusMessage': 'Invalid credentials'}
```
→ Check `SAMCO_USER_ID`, `SAMCO_PASSWORD`, `SAMCO_YOB` in `.env`
→ Ensure no extra spaces in values
→ Verify SAMCO account is active and API access is enabled

```
ERROR: SAMCO login cooldown active (28s)
```
→ Login failed recently, bot is waiting 30s before retrying. Wait and restart.

### .env Not Loading

```
Config: ⚠️ Using defaults
```
→ Run the bot from the project root directory: `cd lords-main && uvicorn backend.main:app`
→ The `.env` file must be at `lords-main/.env` (same level as `backend/`)

### Option Chain Empty

```
ERROR: Option chain empty expiry=2026-04-28 strike=24400 type=CE
```
→ Verify the expiry date is correct — NSE moved NIFTY expiry from **Thursday to Tuesday** effective Sep 2025
→ `samco_client.py` handles this automatically (`get_weekly_expiry()`)
→ If still failing, check SAMCO API connectivity

### Low Volume Warning (Unexpected)

```
WARNING: Low volume 0 — skip NIFTYXXXXX
```
→ Set `MIN_OPTION_VOLUME=0` in `.env` to disable volume check
→ SAMCO `get_quote` may return volume in different fields across versions
→ The `_parse_volume()` function in `trading_engine.py` handles most variants

### Dashboard Not Loading

```
ERR_CONNECTION_REFUSED on http://localhost:8000
```
→ Ensure uvicorn is running: `uvicorn backend.main:app --reload`
→ Check port is not occupied: `netstat -ano | findstr :8000`
→ Check firewall isn't blocking port 8000

### No Signals Firing

```
INFO: TREND FILTER: CALL skipped score=+1 (need +3)
```
→ This is normal behaviour. The trend filter is working.
→ With `TREND_FILTER_ENABLED=true`, expect 4–6 signals per month (not daily)
→ To disable for testing: `TREND_FILTER_ENABLED=false` in `.env`

### Bot Starts After 9:30

```
WARNING: Bot started after ORB window — trading disabled today
```
→ Start the bot **before 9:15 IST** to capture the full ORB window
→ Add to Windows Task Scheduler or Linux cron: `@reboot` + check day of week

### Import Errors

```
ModuleNotFoundError: No module named 'snapi_py_client'
```
→ Install SAMCO SDK: `pip install snapi-py-client`
→ If unavailable: contact SAMCO support at api@samco.in

---

## 13. Future Improvements

### Near-Term (v4.1–v4.5)
- **WebSocket real-time updates** (replace HTTP polling with WS push)
- **Multi-expiry support** (trade next week's expiry when current week is too close)
- **Volume profile integration** (POC, VAH, VAL as additional filter)
- **Greeks dashboard** (show live Delta, Theta, Vega for open position)
- **Automated position sizing** via Kelly Criterion (currently fixed 1 lot)

### Medium-Term (v5.0)
- **ML signal enhancement** — train a classifier on historical ORB features (range size, gap magnitude, ATR ratio) to predict breakout success probability
- **Multi-instrument support** — BANKNIFTY, FINNIFTY options with same engine
- **Broker abstraction layer** — support Zerodha Kite, Upstox, Fyers APIs
- **Walk-forward optimisation** — quarterly re-calibration of filter parameters

### Long-Term (v6.0+)
- **Live option chain pricing** — replace BSM with real-time NSE option chain data for backtest
- **Multi-strategy portfolio** — run ORB + Iron Condor + Calendar spread in parallel
- **Risk-parity allocation** — Kelly-based dynamic allocation across strategies
- **Telegram/email alerts** — notify on signal, fill, SL, T1, T2 events
- **PostgreSQL backend** — replace CSV/JSON with proper database for large trade history

---

## Quick Reference Card

```
START BOT:    uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
DASHBOARD:    http://localhost:8000
BACKTEST:     python backtest_runner.py
PAPER MODE:   MODE=paper in .env
LIVE MODE:    MODE=live in .env + LIVE button on dashboard
LOGS:         logs/bot.log
TRADES:       data/trades.csv
STATE:        data/runtime_state.json

KEY TIMES (IST):
  09:14  Daily reset
  09:15  ORB window opens
  09:30  ORB frozen, first signal possible (09:31+)
  13:30  No new entries
  15:10  Force square-off
  15:35  Market closed, polling paused
```

---

*Lords Bot is an open-source research project. It is not financial advice. Trade responsibly.*
