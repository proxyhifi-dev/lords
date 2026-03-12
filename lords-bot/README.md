# Lords Bot

Lords Bot is a production-style real-time options analysis and paper-trading platform for **NIFTY** and **BANKNIFTY**. It runs an asynchronous data pipeline every 3 seconds, computes market analytics, generates strategy signals, and simulates trades.

## Features

- FastAPI backend with async endpoints
- Samco API client with retries, timeout, and fallback mock data
- Option chain ingestion + pandas processing
- Analytics: PCR, support/resistance, ATM strike, OI/volume intelligence
- Strategy engine with confidence-based BUY CALL / BUY PUT / NO TRADE
- Paper trading engine with trade lifecycle and PnL tracking
- Real-time dashboard (HTML/CSS/Vanilla JS + Chart.js)

## Project Structure

```
lords-bot/
  backend/
  frontend/
  data/
  README.md
```

## Setup

```bash
cd lords-bot/backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional environment variables:

- `SAMCO_BASE_URL`
- `SAMCO_ACCESS_TOKEN`
- `DEFAULT_SYMBOL` (`NIFTY` or `BANKNIFTY`)
- `DEFAULT_EXPIRY`

## Run

```bash
cd lords-bot/backend
uvicorn main:app --reload
```

Open dashboard at: `http://127.0.0.1:8000/`

## API Endpoints

- `GET /option-chain`
- `GET /analysis`
- `GET /signals`
- `GET /support-resistance`
- `GET /paper-trades`
- `GET /paper-pnl`

## Notes

- Cache TTL is 3 seconds by default.
- Data snapshots are persisted in `lords-bot/data/cache.json`.
- Paper trades are persisted in `lords-bot/data/trades.json`.
- If Samco API is unavailable, simulator mode auto-generates realistic option-chain data.
