# Lords FYERS V3 Trading Bot

Production-ready Python architecture for a modular FYERS V3 bot with:

- OAuth2 authcode + refresh token support
- Auto token refresh
- Async API client via `httpx`
- Rate limit safety (10/sec, 200/min)
- Retry strategy for transient failures
- Structured logging (console + rotating file)
- Pydantic request validation for order payloads
- Account and order service layers
- WebSocket service scaffold
- Optional FastAPI wrapper

## Project Layout

```text
lords_bot/
├── app/
│   ├── api.py
│   ├── auth.py
│   ├── account_service.py
│   ├── config.py
│   ├── fyers_client.py
│   ├── order_service.py
│   ├── rate_limiter.py
│   ├── schemas.py
│   ├── utils.py
│   └── websocket_service.py
├── .env.example
├── main.py
└── requirements.txt
```

## Setup

1. Install dependencies:

   ```bash
   pip install -r lords_bot/requirements.txt
   ```

2. Create env file:

   ```bash
   cp lords_bot/.env.example lords_bot/.env
   ```

3. Fill in credentials in `lords_bot/.env`.

4. Run CLI bootstrap:

   ```bash
   cd lords_bot
   python main.py
   ```

5. (Optional) Run FastAPI wrapper:

   ```bash
   cd lords_bot
   uvicorn app.api:app --reload
   ```

## Notes

- Keep secrets only in `.env` (never in source control).
- Tick size and lot-size checks are included as placeholders and should be connected to instrument master data before live deployment.
- Validate all order/risk policies with paper trading before using real capital.
