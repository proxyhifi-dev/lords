# Lords Bot — ORB NIFTY Options Trading Bot

Production-oriented, event-driven ORB options trading system using **official Samco Trade API** via the **Samco Python SDK** (`snapi_py_client`).

## 1) Architecture

```text
Market Data Engine
  ↓
Tick Engine
  ↓
Strategy Engine (ORB)
  ↓
Risk Manager
  ↓
Execution Engine
  ↓
State Manager + Trade Store
  ↓
FastAPI Dashboard
```

## 2) Folder Layout

```text
lords-bot/
  backend/
    app/
      broker/samco_client.py
      market/market_engine.py
      market/tick_engine.py
      strategy/orb_strategy.py
      options/option_selector.py
      execution/order_executor.py
      risk/risk_manager.py
      engine/state_manager.py
      engine/trading_engine.py
      scheduler/market_scheduler.py
      storage/trade_store.py
      utils/logger.py
    storage/trades.json
    logs/bot.log
    main.py
    config.py
  frontend/
  requirements.txt
  .env
```

## 3) Module Responsibilities

- `samco_client.py`:
  - `login`
  - `get_quote`
  - `get_option_chain`
  - `place_order`
  - `get_positions`
  - `get_orders`
  - includes 3x retry failsafe for API calls.
- `market_engine.py`: polls NIFTY quote every second.
- `tick_engine.py`: emits tick only on price change.
- `orb_strategy.py`: ORB capture (9:15–9:30), 0.1% breakout threshold, 5-tick confirmation.
- `option_selector.py`: ATM strike as `round(spot/50)*50`, outputs symbols like `NIFTY24APR22500CE`.
- `order_executor.py`: order placement using Samco constants.
- `risk_manager.py`: max trades/day, stop-loss, target, max daily loss control.
- `state_manager.py`: thread-safe runtime state for spot, signal, active trade and PnL.
- `trading_engine.py`: orchestration, state transitions, PnL tracking, storage writes.
- `trade_store.py`: JSON persistence in `backend/storage/trades.json` and restart recovery.
- `market_scheduler.py`: APScheduler jobs for start/ORB freeze/square-off.
- `main.py`: FastAPI app + `/api/dashboard` endpoint.

## 4) Samco API Usage (SDK Methods)

Implemented through the official SDK bridge class:

```python
from snapi_py_client.snapi_bridge import StocknoteAPIPythonBridge
```

Used methods:
- `login(body={...})`
- `set_session_token(...)`
- `get_quote(symbol_name="NIFTY 50", exchange="NSE")`
- `get_option_chain(...)`
- `place_order(body={...})`
- `get_positions()`
- `get_order_book()`

## 5) ORB Strategy Logic

- ORB window: `09:15–09:30`.
- Track `orb_high` and `orb_low` from incoming ticks.
- After 9:30, freeze ORB and evaluate breakouts:
  - **BUY CE** if price > orb_high and distance ≥ `0.1%` and volume expansion, with 5-tick confirmation above range.
  - **BUY PE** if price < orb_low and distance ≥ `0.1%`, with 5-tick confirmation below range.

## 6) Risk Rules

Configured in `.env`:
- `MAX_TRADES_PER_DAY` (default 2)
- `STOP_LOSS_PCT` (default 0.20)
- `TARGET_PCT` (default 0.40)
- `MAX_DAILY_LOSS` (default 3000)

## 7) Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env`:

```env
SAMCO_USER_ID=YOUR_USER_ID
SAMCO_PASSWORD=YOUR_PASSWORD
SAMCO_YOB=YYYY
NIFTY_SYMBOL=NIFTY 50
NIFTY_EXCHANGE=NSE
ORDER_QTY=50
OPTION_EXPIRY=24APR
MAX_TRADES_PER_DAY=2
STOP_LOSS_PCT=0.20
TARGET_PCT=0.40
MAX_DAILY_LOSS=3000
ORB_START=09:15
ORB_END=09:30
SQUARE_OFF=15:15
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8000
```

## 8) Run

```bash
python backend/main.py
```

## 9) Dashboard API

### `GET /api/dashboard`

Returns:

```json
{
  "spot": 22450.0,
  "orb_high": 22472.25,
  "orb_low": 22390.0,
  "signal": "BUY_CE",
  "active_trade": {
    "symbol": "NIFTY24APR22500CE",
    "entry_price": 22450.0,
    "exit_price": null,
    "pnl": 35.0,
    "timestamp": "2026-04-02T09:31:00.000000",
    "state": "ENTERED"
  },
  "pnl": 35.0
}
```

## 10) Operations Notes

- Logs: `backend/logs/bot.log`
- Trade persistence: `backend/storage/trades.json`
- On restart, active trade is reloaded from storage.
- API failures are retried up to 3 times.

## 11) References

- Samco Trade API docs: https://docs-tradeapi.samco.in
- Samco Python SDK: https://github.com/samco-sdk/Python-SDK
