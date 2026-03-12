# SAMCO NIFTY Option Bot

Production-ready FastAPI trading bot scaffold using SAMCO Trade API with a minimal real-time dashboard.

## Project Structure

```text
project_root/
├── backend/
│   ├── main.py
│   ├── config.py
│   ├── samco_auth.py
│   ├── samco_client.py
│   ├── option_chain_service.py
│   ├── strategy_engine.py
│   └── models.py
├── frontend/
│   ├── index.html
│   └── dashboard.js
└── requirements.txt
```

## Features

- SAMCO login (`POST /login`) with in-memory `sessionToken` reuse.
- Automatic retry with exponential backoff for API requests.
- Token expiration detection (401/403) and automatic re-login.
- Option chain polling every 5 seconds using shared async `httpx.AsyncClient`.
- ATM strike calculation: `round(spot / 50) * 50`.
- Simple signal strategy:
  - `CALL` when `CE_OI > PE_OI` at ATM.
  - `PUT` otherwise.
- JSON APIs:
  - `GET /health`
  - `GET /index-price`
  - `GET /option-chain`
  - `GET /signal`
- Minimal dashboard with Tailwind + JS polling every 5 seconds.

## Setup

1. Create and activate a Python 3.11 virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Export environment variables (or create `.env`):

```bash
export SAMCO_USER_ID="your_user"
export SAMCO_PASSWORD="your_password"
export SAMCO_YOB="YYYY"
export OPTION_EXPIRY="2026-12-31"
```

4. Run:

```bash
python backend/main.py
```

5. Open dashboard:

```text
http://localhost:8000
```

## Notes

- `OPTION_EXPIRY` must match an active NIFTY expiry date.
- Parser is tolerant to field naming variations in option-chain payloads.
