# Lords Bot

Plug-and-play FastAPI options dashboard with Samco market data, option scanner, manual trade approval, paper/real mode, and auto stop-loss/target management.

## Quick start

```bash
pip install -r requirements.txt
cd backend
uvicorn main:app --reload
```

Open: `http://127.0.0.1:8000`

## Environment

Create `.env` in project root:

```env
SYMBOL=NIFTY
EXPIRY=2026-03-26

SAMCO_BASE_URL=https://api.stocknote.com

# Preferred: auto-login flow (StockNoteBridge style)
SAMCO_USER_ID=
SAMCO_PASSWORD=
SAMCO_YOB=

# Optional fallback: provide active session token directly
SAMCO_SESSION_TOKEN=
# Backward-compatible alias
SAMCO_ACCESS_TOKEN=

# Optional legacy header (only if your account setup needs it)
SAMCO_API_KEY=

TRADING_MODE=PAPER
ENABLE_REAL_TRADING=false

SCHEDULER_INTERVAL=15

MAX_TRADES_PER_DAY=5
MAX_DAILY_LOSS=5000
```

## Key endpoints

- `GET /dashboard`
- `POST /trade/approve`
- `POST /trade/close`
- `POST /trade/reset`
- `GET /trading-mode`
- `POST /trading-mode`
