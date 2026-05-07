# Lords Bot — NIFTY Iron Condor

Automated NIFTY weekly Iron Condor bot with paper / live modes, real-time risk gates, and Telegram alerts on critical events.

## What it does

- **Iron Condor entry** during a configurable window (default 09:30–13:30 IST), one trade per day.
- **Strike construction**: ATM-anchored short-call/short-put + protective wings, percentage- or rupee-based.
- **Live monitoring**: real broker quotes for the IC every 2s, with model fallback if quotes degrade.
- **Multi-layer exit**: target profit, stop-loss multiple, extreme-loss stop, EOD square-off, expiry-day safety.
- **Economics filter**: rejects entries where credit can't cover round-trip charges + buffer + reward/risk floor.
- **Hard fail-safe**: any uncertain order or fatal exception flips trading off and emergency-flattens any open trade.
- **State durability**: SQLite + journal + idempotency keys; survives crashes mid-trade.
- **Telegram alerts**: critical events (`SELL_FAILED`, `EXECUTION_UNCERTAIN`, hard-fail, partial-fill-unrecovered) push to your phone.

## Quick start (paper)

```bash
# 1. Install
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# edit .env — see "Configuration" below

# 3. Run
uvicorn backend.main:app --host 0.0.0.0 --port 8000

# 4. Open dashboard
# http://localhost:8000
```

Default `MODE=paper` simulates fills against live market quotes. No real orders are placed.

## Going live — checklist

Before flipping `MODE=live`, all of these should be true:

- [ ] `.env` filled with valid `SAMCO_USER_ID`, `SAMCO_PASSWORD`, `SAMCO_YOB`, `SAMCO_ACCESS_TOKEN`
- [ ] Telegram alerts configured and tested (you got the "bot started" message)
- [ ] Ran 5+ full paper sessions across different market regimes
- [ ] P&L from paper sessions reconciled with what real fills would have produced
- [ ] Manual flatten tested via `POST /api/trade/flatten`
- [ ] Kill-switch tested via `POST /api/kill-switch`
- [ ] Reviewed trades in `data/trades.csv` for correct charge accounting
- [ ] Capital + `MAX_DAILY_LOSS` + `IC_MAX_LOSS_PER_TRADE` set conservatively
- [ ] Started with one lot for the first live week

```bash
MODE=live uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Live mode is fail-closed: any uncertain order disables trading until you re-enable via `POST /api/trading-enabled`.

## Configuration

All knobs live in `.env`. Key groups:

**Mode + capital**
- `MODE` — `paper` or `live`
- `CAPITAL`, `ORDER_QTY` — bankroll, NIFTY lots per trade
- `MAX_DAILY_LOSS`, `MAX_TRADES`, `MAX_CONSECUTIVE_LOSSES`, `MAX_DRAWDOWN_PCT` — risk limits

**IC strategy**
- `IC_ENTRY_WINDOW_START` / `_END`, `NO_ENTRY_AFTER`, `SQUARE_OFF` — timing
- `IC_SHORT_DISTANCE`, `IC_WING_WIDTH`, `IC_STRIKE_ROUNDING` — strike construction
- `IC_TARGET_PROFIT_PCT`, `IC_STOP_LOSS_MULTIPLE`, `IC_EXTREME_LOSS_MULTIPLE` — exit thresholds
- `IC_MIN_ENTRY_PREMIUM`, `IC_MIN_REWARD_RISK`, `IC_MIN_CREDIT_TO_COST_RATIO`, `IC_MIN_NET_AFTER_COST_BUFFER` — economics filter

**Cooldowns**
- `SIGNAL_COOLDOWN_SECONDS` — base time between signals
- `SIGNAL_REJECTION_COOLDOWN_SECONDS` — extended cooldown after rejection (default 300s)

**Alerts**
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — leave blank to disable

## Telegram setup (2 minutes)

1. Open Telegram, message `@BotFather` → `/newbot` → follow prompts → save the token.
2. Message your new bot once (any text).
3. Message `@userinfobot` → it replies with your numeric chat ID.
4. Put both in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=987654321
   ```
5. Restart the bot. You should get a `[INFO] LORDS bot started` message.

If both vars are blank, the notifier is silent — no errors, no setup needed for paper play.

## Architecture

```
.env / config_loader  →  Settings dataclass (singleton)
                              │
                              ▼
SamcoClient ── login/quotes/orders ── broker
                              │
                              ▼
MarketScheduler  ── ticks NIFTY every 1s ──► EventBus
                                                │
                                                ├─► RiskManager     (gates SIGNAL → RISK_APPROVED)
                                                ├─► TradingEngine   (IC entry/monitor/exit)
                                                ├─► ReconciliationEngine (sync local↔broker)
                                                ├─► RejectionWatcher (extends cooldown on rejects)
                                                └─► TelegramNotifier (critical alerts)
                              │
                              ▼
StateManager (SQLite + journal)  ──  TradeStore (CSV)
                              │
                              ▼
           FastAPI dashboard (/api/dashboard, /api/iron-condor/stats, ...)
```

## API endpoints

| Path | Purpose |
|---|---|
| `GET /api/dashboard` | Full state snapshot + recent trades |
| `GET /api/iron-condor/stats` | Live IC position with current premium and exit thresholds |
| `GET /api/analytics` | Win rate, Sharpe, drawdown, Kelly |
| `POST /api/trade/flatten` | Close active position now |
| `POST /api/kill-switch` | Disable trading + cancel open orders |
| `POST /api/reconcile` | Force broker reconciliation |
| `POST /api/emergency-flatten` | Flatten + reconcile |

## Backtest

```bash
python backtest_runner.py --file data/nifty_1min_<date>.csv --capital 50000
```

Note: backtest uses **synthetic option pricing**, not real options-chain history. Results are directional indicators, not validation.

## Tests

```bash
pytest -q
```

The execution-safety suite covers partial fills, rejections, fill timeouts, and exit verification. Engine integration coverage is partial — paper sessions are the better validation.

## Disclaimer

Live options trading on NIFTY carries real money risk. The strategy thresholds (target / SL / extreme) are reasonable defaults but have not been validated against extensive real NIFTY weekly options data. Run paper first. Start small. Watch your alerts.
