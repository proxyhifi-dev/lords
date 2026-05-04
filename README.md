# Lords Bot (Iron Condor)

Automated NIFTY options bot using an Iron Condor strategy with paper/live modes.

## Safety Defaults
- Default is `MODE=paper`.
- Live mode is fail-closed and requires explicit SAMCO + risk env vars.
- Do not run live until tests pass.

## Quick Start
1. Copy `.env.example` to `.env` and edit.
2. Install deps: `pip install -r requirements.txt`
3. Run API: `uvicorn backend.app.main:app --host 127.0.0.1 --port 8000`
4. Run tests: `pytest -q`

## Paper Mode
```bash
MODE=paper uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

## Live Mode
```bash
MODE=live uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Required live vars: `SAMCO_USER_ID`, `SAMCO_PASSWORD` (or token workflow), `SAMCO_YOB`, `CAPITAL`, `ORDER_QTY`, `MAX_DAILY_LOSS`, `MAX_TRADES`.

## Backtest
```bash
python backtest_runner.py --help
```

## Disclaimer
Live trading carries real risk. Validate in paper mode first.
