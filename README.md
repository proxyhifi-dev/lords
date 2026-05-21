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

## Files changed in this session

| File | What was fixed |
|------|---------------|
| `backend/app/engine/trading_engine.py` | Health loop adaptive interval; emergency exit fresh quote fetch; slippage-adjusted premium gate; ₹0 exit price fallback; EOD exit through model_fallback; `CLOSING` guard; delta strikes; journal events |
| `backend/app/scheduler/market_scheduler.py` | IV staleness gate (90s); India VIX cascade; ATM implied vol fallback |
| `backend/app/broker/samco_client.py` | Auth cascade break; 200ms rate limiter; `get_india_vix()` method |
| `backend/app/engine/state_manager.py` | `peak_equity` high-water mark; overnight rollover; backup rate limit; idempotency TTL |
| `backend/app/strategy/iron_condor_strategy.py` | BSM delta strikes; Newton-Raphson implied vol; DTE-aware credit scaling |
| `backend/app/engine/execution_manager.py` | Paper mode returns `avg_price=None` not `0.0` |
| `backend/app/core/startup_manager.py` | Validates `SAMCO_USER_ID`, `SAMCO_PASSWORD`, `SAMCO_YOB` at startup |
| `backend/app/core/config_loader.py` | Added `ic_slippage_per_leg` |
| `.env` | Updated distances, stop-loss multiples, entry window, added `IC_SLIPPAGE_PER_LEG`, `SCHEDULER_STALL_HARD_SECONDS` |

---

## Disclaimer

Live options trading on NIFTY carries real financial risk. Default thresholds are calibrated for a ₹50,000 account with conservative risk parameters, but have not been validated against extended real-money NIFTY data. Always run paper mode first. Start with minimum capital. Monitor Telegram alerts actively.

The bot includes multiple fail-safes (circuit breaker, health loop, kill-switch, reconciliation) but no software system is infallible. Always know how to manually square off your NIFTY positions in the SAMCO web/app interface as a backup.
