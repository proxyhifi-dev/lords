# Lords Bot — Automated NIFTY Options ORB Trading System

Production-grade, event-driven trading engine built around the official SAMCO Python SDK (`stocknotebridge`).

## Architecture

- `Market Engine` polls NIFTY quote from SAMCO `get_quote()`.
- `EventBus` (`asyncio.Queue`) is the only communication channel.
- `Tick Engine` emits normalized ticks.
- `ORB Strategy` computes ORB (09:15–09:30), freezes range, emits breakout signal.
- `Risk Manager` enforces max trades / max daily loss.
- `Execution Engine` selects option + places order via `place_order()`.
- `Trading Engine` manages state, reconciliation (`get_order_book`, `get_positions`) every 30s.
- `Circuit Breaker` protects trading during broker instability.
- `FastAPI` exposes `/api/dashboard`.

## Folder Structure

```text
lords-bot/
├── backend/
│   ├── app/
│   │   ├── api/dashboard_api.py
│   │   ├── broker/samco_client.py
│   │   ├── core/
│   │   │   ├── circuit_breaker.py
│   │   │   ├── config_loader.py
│   │   │   └── event_bus.py
│   │   ├── engine/
│   │   │   ├── state_manager.py
│   │   │   └── trading_engine.py
│   │   ├── execution/order_executor.py
│   │   ├── market/
│   │   │   ├── market_engine.py
│   │   │   └── tick_engine.py
│   │   ├── options/option_selector.py
│   │   ├── risk/risk_manager.py
│   │   ├── scheduler/market_scheduler.py
│   │   ├── storage/trade_store.py
│   │   └── strategy/orb_strategy.py
│   ├── config.py
│   ├── main.py
│   └── storage/
├── main.py
├── requirements.txt
└── .env
```

## Environment Variables

Configure all values in `.env`:

```env
SAMCO_USER_ID=
SAMCO_PASSWORD=
SAMCO_YOB=

NIFTY_SYMBOL=NIFTY 50
NIFTY_EXCHANGE=NSE
POLL_SECONDS=1
ORDER_QTY=50

MAX_DAILY_LOSS=3000
MAX_TRADES=2
RISK_PERCENT=1
STOP_LOSS_PCT=0.2
TARGET_PCT=0.4

ORB_START=09:15
ORB_END=09:30
SQUARE_OFF=15:15

RECONNECT_MAX_ATTEMPTS=5
RECONNECT_BASE_DELAY=1
CIRCUIT_FAILURE_THRESHOLD=3
CIRCUIT_COOLDOWN_SECONDS=30

TRADES_FILE=backend/storage/trades.json
STATE_FILE=backend/storage/runtime_state.json
LOG_FILE=backend/logs/bot.log
```

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

## Dashboard

- `GET /api/dashboard`
- returns:
  - `spot_price`
  - `orb_high`
  - `orb_low`
  - `signal`
  - `active_trade`
  - `daily_pnl`

## Official References

- https://docs-tradeapi.samco.in
- https://github.com/samco-sdk/Python-SDK
