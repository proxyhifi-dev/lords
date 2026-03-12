# lords-bot

Refactored FastAPI NIFTY options bot with layered architecture, in-memory caching, scheduler strategy loop, dashboard endpoint, and paper/real trading safety controls.

## Run

```bash
cd lords-bot
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

## Key API

- `GET /dashboard` (single UI payload)
- `GET /option-chain`
- `GET /analysis`
- `GET /signals`
- `GET /profile`
- `GET /funds`
- `GET /trading-mode`
- `POST /trading-mode`

## Caching / Rate Limit Protection

- Option chain TTL: 5 sec
- Profile TTL: 60 sec
- Funds TTL: 60 sec

## Trading Safety

- `MAX_TRADES_PER_DAY=5`
- `MAX_DAILY_LOSS=5000`
- Real trading is blocked unless `ENABLE_REAL_TRADING=true`.
