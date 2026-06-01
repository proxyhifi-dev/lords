# Lords Bot — NIFTY Iron Condor v6.0

Automated NIFTY weekly Iron Condor bot. Paper and live modes, delta-targeted strikes, live IV cascade, full audit trail, and Telegram alerts.

---

## What it does

- **Delta-targeted strike selection** — Black-Scholes binary search places short strikes at configurable delta (default 10Δ ≈ 85% PoP). At NIFTY 24000, IV 15%, 7 DTE → shorts land ~600 pts OTM, not 200.
- **Live IV cascade** — IV sourced in priority order: index quote → India VIX (60s cache) → ATM CE implied vol (Newton-Raphson) → assumed fallback. Entry blocked if IV is stale (>90s).
- **Entry window** — configurable IST window (default 09:30–10:30), one trade per day, skip expiry day.
- **Economics filter** — rejects entries where slippage-adjusted credit can't cover charges + reward/risk floor.
- **Multi-layer exit** — target profit (50%), stop-loss (2×), extreme-loss (3×), EOD square-off, spot-proximity exit, expiry-day safety.
- **Duplicate exit guard** — `status=CLOSING` + `exit_in_progress` flag prevents concurrent exit calls from executing twice.
- **Emergency exit with fresh quotes** — fetches live bid/ask for each IC leg before closing; falls back to cached only if broker is unreachable.
- **Health loop** — adaptive interval (10s during active trade, 60s idle). Broker failure triggers alert and disables new entries — does **not** blindly exit the position.
- **State durability** — SQLite WAL + journal + idempotency keys (24h TTL); survives crashes mid-trade.
- **Audit trail** — `TRADE_ENTRY` and `TRADE_EXIT` journal events with full leg detail, charges, and P&L.
- **Telegram alerts** — critical events push to your phone within seconds.

---

## Quick start (paper mode)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — fill SAMCO credentials if you want real market data
# Leave MODE=paper for safe testing

# 3. Run
uvicorn backend.main:app --host 0.0.0.0 --port 8000

# 4. Open dashboard
# http://localhost:8000
```

`MODE=paper` simulates fills against live SAMCO quotes. No real orders are placed.  
`PAPER_MODE_USE_BROKER=true` (default) fetches real NIFTY prices even in paper mode.

---

## Going live — checklist

Complete every item before setting `MODE=live`:

**Credentials & connectivity**
- [ ] `SAMCO_USER_ID`, `SAMCO_PASSWORD`, `SAMCO_YOB` filled in `.env`
- [ ] Bot logs in successfully in paper mode with `PAPER_MODE_USE_BROKER=true`
- [ ] Telegram alerts working — you received the startup message on your phone

**Paper validation (minimum 10 trades)**
- [ ] Win rate ≥ 60% over the last 10 paper trades
- [ ] No phantom P&L (entry and exit prices look realistic)
- [ ] EOD exits firing correctly at `IC_EXIT_TIME`
- [ ] Stop-loss exits firing when premium hits `IC_STOP_LOSS_MULTIPLE × entry`
- [ ] Manual flatten works via `POST /api/trade/flatten`
- [ ] `data/trades.csv` charges look correct (not ₹0, not ₹10,000)

**Risk settings confirmed**
- [ ] `CAPITAL` matches your actual trading account balance
- [ ] `MAX_DAILY_LOSS` ≤ 6% of capital (default ₹3000 on ₹50000)
- [ ] `IC_MARGIN_REQUIRED` matches SAMCO's actual margin block for NIFTY IC
- [ ] `ORDER_QTY=65` verified against your margin availability

**Go live**
```bash
# In .env: set MODE=live, then restart
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Live mode is fail-closed: any uncertain order or fatal exception disables trading. Re-enable via `POST /api/trading-enabled`.

---

## Configuration reference

All settings live in `.env`. Copy `.env.example` as your starting point.

### Credentials
| Setting | Description |
|---------|-------------|
| `SAMCO_USER_ID` | SAMCO login user ID |
| `SAMCO_PASSWORD` | SAMCO login password |
| `SAMCO_YOB` | Year of birth (used for SAMCO login) |
| `SAMCO_ACCESS_TOKEN` | Optional pre-auth token |

### Mode & capital
| Setting | Default | Description |
|---------|---------|-------------|
| `MODE` | `paper` | `paper` or `live` |
| `CAPITAL` | `50000` | Account capital for drawdown calculations |
| `ORDER_QTY` | `65` | Qty per IC leg — matches your lot size |
| `MAX_DAILY_LOSS` | `3000` | Halt trading if daily P&L drops below −₹3000 |
| `MAX_TRADES` | `10` | Max trades per day |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Halt after N consecutive losing trades |
| `MAX_DRAWDOWN_PCT` | `0.20` | Halt if equity drawdown exceeds 20% |

### Entry timing
| Setting | Default | Description |
|---------|---------|-------------|
| `IC_ENTRY_WINDOW_START` | `09:30` | Earliest entry time (IST) |
| `IC_ENTRY_WINDOW_END` | `10:30` | Latest entry time — first hour has best IV stability |
| `NO_ENTRY_AFTER` | `10:30` | Hard cutoff; no new positions after this |
| `SQUARE_OFF` | `14:55` | Force-close all positions at this time |
| `IC_EXIT_TIME` | `15:00` | EOD exit time if not already closed |

### Strike construction
| Setting | Default | Description |
|---------|---------|-------------|
| `IC_SHORT_DISTANCE` | `400` | Fallback fixed-distance (pts) when delta calc unavailable |
| `IC_WING_WIDTH` | `100` | Long strike offset from short strike |
| `IC_STRIKE_ROUNDING` | `50` | Snap strikes to nearest N points |

The bot uses **BSM delta targeting** when live IV is available (default 10Δ). `IC_SHORT_DISTANCE` is only used as fallback when IV is unavailable.

### Entry economics
| Setting | Default | Description |
|---------|---------|-------------|
| `IC_MIN_ENTRY_PREMIUM` | `20` | Minimum net credit (pts) to accept entry |
| `IC_SLIPPAGE_PER_LEG` | `3` | Pts deducted per leg for bid-ask slippage (×4 legs = 12 pts total) |
| `IC_MIN_REWARD_RISK` | `0.12` | Minimum credit / max-loss ratio |
| `IC_MIN_NET_AFTER_COST_BUFFER` | `40` | Net credit must exceed charges by this many pts |
| `IC_MIN_CREDIT_TO_COST_RATIO` | `1.30` | Credit must be ≥ 1.3× estimated round-trip cost |
| `IC_EXPECTED_MOVE_BUFFER` | `1.30` | Short strikes must be ≥ 1.3× expected move from spot |

### Exit thresholds
| Setting | Default | Description |
|---------|---------|-------------|
| `IC_TARGET_PROFIT_PCT` | `0.70` | Exit when current premium = 30% of entry (collect 70% of credit) |
| `IC_STOP_LOSS_MULTIPLE` | `1.50` | Exit when premium = 1.5× entry credit |
| `IC_EXTREME_LOSS_MULTIPLE` | `3.00` | Immediate exit when premium = 3× entry credit |

### IV & model
| Setting | Default | Description |
|---------|---------|-------------|
| `IC_ASSUMED_IV` | `0.15` | Fallback IV (15%) when live data unavailable |
| `IC_HIGH_PROBABILITY_MODE` | `true` | Enforce VIX sweet-spot filter on entry |
| `IC_REQUIRE_LIVE_IV` | `false` | If `true`, block entry when live IV unavailable |
| `IC_MIN_LIVE_IV` | `0.14` | Block entry if India VIX < 14% — not enough premium |
| `IC_MAX_LIVE_IV` | `0.20` | Block entry if India VIX > 20% — regime too volatile for IC |
| `IC_DAYS_TO_EXPIRY` | `1` | Model DTE assumption; `1` for same-day/weekly |

### Safety & connectivity
| Setting | Default | Description |
|---------|---------|-------------|
| `DEADMAN_TIMEOUT` | `30` | Trigger fail-safe if no quote received for 30s |
| `SCHEDULER_STALL_HARD_SECONDS` | `60` | Trigger fail-safe if scheduler stalls for 60s |
| `CIRCUIT_FAILURE_THRESHOLD` | `3` | Open circuit breaker after N consecutive broker failures |
| `CIRCUIT_COOLDOWN_SECONDS` | `30` | Circuit stays open for 30s before probing recovery |
| `RECONCILIATION_INTERVAL_SECONDS` | `300` | Broker position reconciliation every 5 min |

---

## Architecture

```
.env / config_loader  →  Settings (frozen dataclass, singleton)
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
    SamcoClient          StateManager          TradeStore
    (broker API,         (SQLite WAL,          (CSV audit,
     circuit breaker,     journal events,       charges,
     200ms rate limit,    idempotency keys,     P&L)
     IV cascade)          peak_equity,
                          rollover)
          │
          ▼
    MarketScheduler  ─── ticks NIFTY every 1s ───► EventBus
    (IV staleness gate,                               │
     IC entry gate,                     ┌────────────┼────────────┐
     daily reset,                       ▼            ▼            ▼
     stall watchdog)              RiskManager  TradingEngine  Reconciliation
                                  (validates   (IC entry,     (broker sync,
                                   signal →     delta strikes, position check)
                                   RISK_APPROVED monitor loop,      │
                                   event)       exit pipeline,      ▼
                                                health loop)  TelegramNotifier
                                                              (critical alerts)
          │
          ▼
    FastAPI  (/api/dashboard, /api/iron-condor/stats, /api/analytics, ...)
```

### Key flows

**Entry:** `_tick` → `_iron_condor_can_enter` (gates: time window, IV staleness, one-per-day, live readiness) → `SIGNAL` event → `RiskManager` → `RISK_APPROVED` → `_enter_iron_condor_trade` → BSM delta strikes → quote snapshot → slippage check → economics filter → place 4 legs → `TRADE_ENTRY` journal

**Exit:** `_monitor_iron_condor_trade` polls premium every tick → `get_exit_reason` checks target/SL/extreme/EOD/proximity → `_exit_iron_condor_trade` (CLOSING guard) → `_execute_iron_condor_exit_legs` (fallback price on ₹0) → `TRADE_EXIT` journal → state cleared

**Emergency:** broker unreachable for 3 consecutive health checks → disable new entries → alert → human intervention (does NOT force-exit position blindly)

---

## API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/dashboard` | Full state snapshot, active trade, recent trades |
| `GET` | `/api/iron-condor/stats` | Live IC position, current premium, exit thresholds, leg prices |
| `GET` | `/api/analytics` | Win rate, Sharpe, Sortino, drawdown, Kelly fraction |
| `GET` | `/api/status` | Bot running, mode, trading enabled |
| `GET` | `/api/pnl` | Daily P&L, live P&L, trade count |
| `GET` | `/api/trades` | All trades (normalized) |
| `GET` | `/api/reconciliation` | Latest reconciliation result |
| `POST` | `/api/trade/flatten` | Close active position immediately |
| `POST` | `/api/emergency-flatten` | Flatten + reconcile |
| `POST` | `/api/reconcile` | Force broker reconciliation now |
| `POST` | `/api/trading-enabled` | Enable or disable new entries (`{"enabled": true/false}`) |
| `POST` | `/api/start` | Start scheduler |
| `POST` | `/api/stop` | Stop scheduler |

---

## Telegram setup (2 minutes)

1. Message `@BotFather` on Telegram → `/newbot` → follow prompts → copy the token.
2. Send any message to your new bot.
3. Message `@userinfobot` → it replies with your numeric chat ID.
4. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-your-token
   TELEGRAM_CHAT_ID=987654321
   ```
5. Restart the bot. You will receive a startup message.

Leave both vars blank to run silently — no errors, no setup needed for paper testing.

---


## Disclaimer

Live options trading on NIFTY carries real financial risk. Default thresholds are calibrated for a ₹50,000 account with conservative risk parameters, but have not been validated against extended real-money NIFTY data. Always run paper mode first. Start with minimum capital. Monitor Telegram alerts actively.

The bot includes multiple fail-safes (circuit breaker, health loop, kill-switch, reconciliation) but no software system is infallible. Always know how to manually square off your NIFTY positions in the SAMCO web/app interface as a backup.






















# Iron Condor Strategy — Complete Guide (Basics to Advanced)

*A practical, honest reference for trading iron condors on Nifty / Bank Nifty.*

---

## 0. One honest note before you start

There is **no configuration that gives a guaranteed high success rate.** Anyone who tells you otherwise is selling something. Index options are efficiently priced, so a clean iron condor is roughly break-even *before* costs and slightly negative *after* costs. The edge does not come from "selling premium and hoping." It comes from three things this guide focuses on:

1. **Strike placement** — so your probability of profit is high.
2. **Regime and volatility selection** — so you only trade when conditions favor the strategy.
3. **Cost control** — so fees don't eat the small edge.

This document gives you the configuration that maximizes *sustainable expectancy*, which is the real meaning of "max success." A high win rate that loses money is worthless; a moderate win rate that is profitable after costs is the goal.

---

## 1. What an iron condor is

An iron condor is a **four-leg, defined-risk, premium-selling** options strategy. You build it by combining two credit spreads — one on the call side, one on the put side:

| Leg | Action | Position |
|---|---|---|
| 1 | **Sell** | Out-of-the-money (OTM) call (short call) |
| 2 | **Buy** | Further OTM call (long call — the "wing") |
| 3 | **Sell** | OTM put (short put) |
| 4 | **Buy** | Further OTM put (long put — the "wing") |

You **collect a net credit** (premium) upfront. The two short options are where you earn; the two long options are insurance that caps your maximum loss.

### The payoff shape
- You keep the full credit if the underlying expires **between the two short strikes**.
- You lose money if the underlying moves **beyond a short strike**.
- Your maximum loss is capped at **(wing width − net credit) × lot size**, because the long wings limit how far the loss can run.

This is the "tent with a flat top" payoff: a wide profit plateau in the middle, capped losses on both sides.

---

## 2. How it makes and loses money

You are acting like an **insurance company**. You collect a premium and profit if "nothing dramatic happens" — i.e., the underlying stays range-bound. Two forces work in your favor:

- **Theta (time decay):** options lose value as expiry approaches. Since you are net short options, that decay is your profit.
- **Volatility contraction:** if implied volatility falls after you enter, your short options cheapen, helping you.

Two forces work against you:

- **A large directional move** in either direction pushes the underlying past a short strike.
- **A volatility spike (rising IV)** inflates your short options against you, even without a big price move.

The strategy wins often (small, frequent gains) and loses rarely but larger. Managing that asymmetry is the entire skill.

---

## 3. The core math you must understand

### Probability of profit (POP) vs. breakeven win rate
This is the single most important concept. Two numbers must line up:

- **Probability of profit (POP):** set by *how far out your strikes are*. Strikes far from price → high POP.
- **Breakeven win rate:** set by your *profit target and stop loss ratio* plus costs. The smaller your target relative to your stop, the higher the win rate you need just to break even.

**Rule: your POP must be HIGHER than your breakeven win rate.** If it isn't, the strategy is mathematically designed to lose, no matter how good your software is.

### Breakeven win rate formula
If you win `+T` (target) and lose `−S` (stop) per trade, the win rate `p` needed to break even is:

```
p × T = (1 − p) × S
p = S / (T + S)
```

Examples (ignoring costs):
| Target | Stop | Breakeven win rate |
|---|---|---|
| 25% of credit | 160% | ~87% (very hard — avoid) |
| 50% of credit | 100% (1×) | ~67% (achievable) |
| 50% of credit | 200% (2×) | ~80% (hard) |

The 50%-target / 1×-stop combination is the realistic sweet spot.

### Expectancy (the only number that matters)
```
Expectancy = (win rate × avg win) − (loss rate × avg loss) − costs
```
A strategy is only worth trading if expectancy is **positive after costs.** Win rate alone tells you nothing.

---

## 4. The Greeks that matter

- **Delta:** directional exposure. You want the position near delta-neutral at entry. Short-strike delta (~0.15) also approximates the probability that strike finishes in the money.
- **Theta:** your friend — positive theta means you earn as time passes.
- **Vega:** your enemy when IV rises. You are short vega, so you want to enter when IV is *high* (so it can fall) and avoid entering in dead-low IV.
- **Gamma:** risk that accelerates near expiry and near your strikes. Closer to expiry = higher gamma risk = faster losses if breached.

---

## 5. Configuration parameters — recommended ranges and why

This is the "1 to 100" setup. Values are starting points to validate, not promises.

| Parameter | Recommended | Why |
|---|---|---|
| **Underlying** | Nifty 50 (most liquid) | Tight spreads, deep liquidity reduce slippage |
| **Expiry / DTE** | 2–7 days to expiry | Weekly expiries decay fast (good theta) but watch gamma; further DTE = lower gamma risk |
| **Entry time** | ~09:45–11:00 IST | Let the opening volatility settle; avoid the first 15 min |
| **Short strike delta** | **~0.15–0.20** each side | ≈ 80–85% chance each short finishes OTM → high POP. THE key setting |
| **Strike distance (alt.)** | ≈ 1 expected move (≈ ATM straddle price) | Equivalent way to place strikes by volatility, not a fixed point count |
| **Wing width** | 100–200 pts (Nifty) | Wide enough for meaningful credit; defines max loss. Avoid tiny 50-pt wings (poor risk-reward) |
| **Profit target** | **50% of net credit** | Take profit early; don't squeeze the last rupee where gamma risk is highest |
| **Stop loss** | **1×–1.5× net credit**, or exit if a short strike is breached | Keeps the loss controlled so breakeven win rate stays ~67–75% |
| **IV filter** | Enter only when IV is elevated (e.g. IV Rank > 30–50) | You sell premium — richer premium = better edge. Don't sell in low IV |
| **Trend filter** | Skip strongly trending days | Condors get run over in trends. Use ADX or an open-range/gap check |
| **Gap filter** | Skip if overnight gap > ~1% | Big gaps blow past strikes before you can react |
| **Position size** | Risk **1–2% of capital** per trade (by max loss) | One worst-case loss shouldn't dent the account |
| **Max trades/day** | 1–3 | Quality over quantity; avoid overtrading |
| **Max daily loss** | 2–3% of capital → hard stop | Survive bad days |
| **Max consecutive losses** | 3 → pause | Forces a reset when the regime turns against you |

### Adjustment rules (keep them mechanical)
- If one side is tested (price approaches a short strike), either **close the whole trade at your stop** (simplest, recommended for beginners) or **roll the untested side closer** to collect more credit (advanced).
- Never "average down" or add to a losing condor hoping it recovers — that's how small losses become account-ending ones.
- Always exit before expiry day's final gamma spike if profit isn't already taken.

---

## 6. The recommended "best practice" configuration (summary)

A solid, validate-this starting setup for a Nifty iron condor:

- **Strikes:** short legs at ~0.15 delta both sides; wings 150 points out.
- **Profit target:** close at 50% of credit collected.
- **Stop loss:** close at 1× credit lost, or immediately if a short strike is breached.
- **Entry filter:** only when IV Rank > 30 **and** the day is range-bound (no strong trend, gap < 1%).
- **Entry window:** ~09:45–11:00 IST.
- **Sizing:** quantity set so max loss = 1–2% of capital.
- **Limits:** max 2 trades/day, daily loss stop at 2–3%, pause after 3 losses.

With ~0.15-delta strikes, POP is roughly 68–72%; with a 50%/1× target/stop, breakeven win rate is ~67%. That gives a **thin positive edge before costs** — which is exactly why cost control (step 8) and IV selection are decisive.

---

## 7. Honest expectations (read this twice)

- A well-built index iron condor typically wins **65–80%** of trades — but the losses are bigger than the wins, so a high win rate does **not** mean profit.
- After costs (STT, exchange fees, GST, stamp duty — and brokerage if any), expectancy on a vanilla condor is **near zero or slightly negative.** Your job is to push it positive through IV timing, regime selection, and minimizing costs.
- There is **no win rate target of 90%+** that is both real and survivable. If a backtest shows it, you have overfit and it will fail live.
- The realistic, professional goal is a **steady positive expectancy with controlled drawdowns**, not a perfect strategy.

---

## 8. Cost control (often the difference between win and loss)

- Use a **zero/low-brokerage broker** — brokerage of ₹100/trade can turn a winning condor into a loser. Statutory charges (STT, GST, exchange, stamp) still apply and can't be avoided.
- **Cost-aware entry rule:** before entering, compute `profit at target − all round-trip charges`. If that isn't clearly positive, skip the trade.
- Fewer, higher-quality trades beat many marginal ones, because each trade carries fixed costs.

---

## 9. Validation protocol (do this before risking money)

1. **Backtest on real option prices** (not synthetic/model prices) across many expiries and different market regimes — trending, ranging, high-IV, low-IV.
2. Confirm **positive expectancy after all costs** over hundreds of trades.
3. **Paper trade** the exact config live for several uninterrupted sessions.
4. Only then trade small real size; scale up only after consistent results.
5. **Monitor for edge decay** — if win rate or expectancy drifts down over time, stop and re-evaluate. Edges don't last forever.

---

## 10. How iron condors blow up (avoid these)

- **Strikes too close to price** → frequent breaches, low POP.
- **Tiny profit target with huge stop** → needs an impossible win rate to break even.
- **No regime filter** → selling condors into a trending or news-driven day.
- **Oversizing** → one breached condor wipes out months of small wins.
- **Holding into expiry-day gamma** → a late move turns a winner into a large loser in minutes.
- **Selling in low IV** → thin premium, poor reward for the risk taken.
- **Revenge trading / averaging down** → turning a controlled loss into a catastrophic one.

---

### Final word
The configuration above is the closest honest thing to a "max success" setup: it aligns probability of profit with breakeven win rate, sells premium only when it's rich, avoids the regimes that destroy condors, and controls costs and size. But the configuration is only a hypothesis until **your own backtest on real data proves positive expectancy after costs.** Build it, test it honestly, keep it if it works, and replace it if it doesn't. That discipline — not any single setting — is what separates traders who last from those who don't.
