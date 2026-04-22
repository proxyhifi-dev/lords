# Lords Bot v3.1 — Fix Notes

## Critical Fixes

### 1. `.env` Not Loading (config_loader.py)
**Problem:** `env_file = ".env"` resolves relative to current working directory.
If you run the bot from inside `backend/` or any directory other than project root,
pydantic-settings can't find `.env` and uses hardcoded defaults — including
`min_option_volume = 500` instead of the `0` in your `.env`.

**Fix:** `_ENV_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".env"`
Now `.env` is always found regardless of which directory you run from.

### 2. All Trades Blocked by "Low volume 0" (trading_engine.py)
**Problem:** Two issues combined:
  - The `.env` loading bug above caused `min_option_volume` to stay at 500 (default)
    instead of 0 (your `.env` value), so every trade was blocked.
  - Even when volume check was active, the volume parsing didn't cover all SAMCO
    response nesting variants (`quoteDetails.tradedVolume`).

**Fix:**
  - Volume check is now completely skipped when `MIN_OPTION_VOLUME=0` in `.env`.
  - Added `_parse_volume()` helper that checks all known SAMCO field paths:
    root keys, `quoteDetails`, and `data` nesting.

### 3. ORDER_QTY in .env (.env)
**Problem:** `.env` had `ORDER_QTY=50` (old lot size) but NIFTY lot size is 65.

**Fix:** Updated to `ORDER_QTY=65`.

### 4. Option Chain Expiry Still Thursday (option_store.py)
**Problem:** `OptionChainCollector._resolve_expiry_date()` hardcoded Thursday expiry.
NSE moved NIFTY weekly expiry to Tuesday effective Sep 2 2025.

**Fix:** Now delegates to `samco_client.get_weekly_expiry()` which has the
correct Tuesday/Thursday date logic.

### 5. Wrong Config Import in market_engine.py
**Problem:** `from backend.config import settings` (old path) instead of
`from backend.app.core.config_loader import get_settings`.

**Fix:** Updated import path.

### 6. Dashboard API Missing Fields (dashboard_api.py)
**Problem:** The `/api/dashboard` route on the APIRouter (used when wired via
`build_dashboard_router`) was missing `bot_running`, `trading_enabled`,
`trade_count` fields.

**Fix:** Added all state fields matching the main `/api/dashboard` endpoint in `main.py`.

## No Changes Needed
- `samco_client.py` — login, option chain, parse_ltp all correct
- `market_scheduler.py` — ORB logic, candle builder, trend score all correct
- `risk_manager.py` — all checks correct
- `state_manager.py` — daily reset, persistence all correct
- `trade_store.py` — CSV logging correct
- `backtest_runner.py` — DTE-calibrated IV, Tuesday expiry all correct

## How to Run

```bash
# From project root (lords-main/)
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

# Backtest
python backtest_runner.py
python backtest_runner.py --file data/nifty_1min_20260413.csv
```

## Verifying the Fix
After startup, logs should show:
```
ENTRY NIFTY26APR24350PE qty=65 ltp=₹XX.XX SL=₹XX.XX T1=₹XX.XX T2=₹XX.XX mode=PAPER
```
instead of:
```
Low volume 0 — skip NIFTY26APR24350PE
```
