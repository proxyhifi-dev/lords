# Lords Bot (Production-Grade Samco Trading Bot)

## Configure credentials
Create `.env` in repo root:

```env
SAMCO_USER_ID=your_user
SAMCO_PASSWORD=your_password
SAMCO_YOB=1990
SAMCO_SESSION_TOKEN=
```

## Run bot
```bash
pip install -r requirements.txt
python backend/main.py
```

## Run tests
```bash
PYTHONPATH=backend pytest tests/test_market_feed.py tests/test_strategy.py tests/test_execution.py tests/test_full_bot.py
```

## Architecture
- `backend/broker/samco_broker.py`: Samco API adapter with retry, validation and global rate limiting.
- `backend/engine/market_feed_engine.py`: robust tick producer.
- `backend/engine/strategy_engine.py`: signal emitter.
- `backend/engine/execution_engine.py`: order placement, duplicate guard, state persistence.
- `backend/risk/risk_manager.py`: risk gates.
- `backend/services/state_manager.py`: disk state recovery using `runtime_state.json`.
