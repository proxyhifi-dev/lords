# Lords Bot - Production ORB Platform

This codebase has been refactored into a modular ORB (Opening Range Breakout) trading platform for NIFTY options.

## Architecture

- `backend/brokers`: Samco Stocknote integration wrapper with login/session/retry handling.
- `backend/services`: market data, option chain, trade logging, performance metrics.
- `backend/engine`: scheduler, candle builder, order manager, state manager, backtester.
- `backend/strategies`: ORB signal engine with RSI and OI bias confirmation.
- `backend/risk`: global risk manager + circuit breaker + trade lock checks.
- `backend/api`: dashboard and trading control endpoints.
- `frontend`: web dashboard with paper/real mode switch and bot controls.

## Core Safety Features

- Pre-trade risk checks (risk per trade, max trades/day, daily loss limit).
- Circuit breaker statuses (`DAILY_LOSS_LIMIT`, `BROKER_DISCONNECTED`, `API_UNSTABLE`).
- Single active trade lock.
- Persistent state recovery via `backend/data/state.json`.
- Order placement + order status verification flow.

## Run

```bash
cd lords-bot
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```

Open `http://localhost:8000`.
