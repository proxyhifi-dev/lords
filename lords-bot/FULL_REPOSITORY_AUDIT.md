# Full Repository Audit (2026-04-02)

## Findings

### 1) Duplicate/overlapping responsibilities
- Trading orchestration logic was split across `main.py`, `market_engine.py`, and `trading_engine.py` with tight coupling.
- Scheduler directly mutated strategy internals instead of emitting events.

### 2) Circular/brittle import risks
- Engines directly referenced each other (`trading_engine -> strategy/risk/execution`) causing strong coupling and making circular expansion likely.

### 3) Hardcoded configuration
- Option expiry was hardcoded (`OPTION_EXPIRY`) in option symbol selection.
- Several defaults were embedded in code paths without central validation.

### 4) Incorrect or weak SAMCO usage
- Mixed `get_orders()` / `get_order_book()` branching; standardized to official `get_order_book()`.
- Blocking calls were executed in event loops without isolation.

### 5) Unsafe blocking loops
- `time.sleep()` was used in runtime loops.
- Threaded startup + infinite polling loop created shutdown hazards.

### 6) Fault tolerance gaps
- No circuit breaker.
- Retry logic had fixed retries and no true exponential sequence.
- No periodic reconciliation against broker order/position state.

### 7) Restart safety gaps
- Runtime state persistence was partial and not atomic for all key fields.

## Refactor Actions Performed

- Introduced `core/event_bus.py` and migrated module communication to event-driven async flow.
- Added `core/circuit_breaker.py` with `CLOSED/OPEN/HALF_OPEN` behavior.
- Added typed env loader in `core/config_loader.py` and centralized settings via `backend/config.py`.
- Rebuilt broker client with async-safe SDK calls (`asyncio.to_thread`) + reconnect/session renewal + exponential backoff.
- Rebuilt trading engine to include:
  - state updates from events,
  - 30-second reconciliation loop (`get_order_book`, `get_positions`),
  - broker health monitor.
- Added persistent runtime state manager (`backend/storage/runtime_state.json`) and trade persistence.
- Reworked scheduler to `AsyncIOScheduler` and event publication.
- Added dashboard router in `app/api/dashboard_api.py`.
- Added root `main.py` for `uvicorn main:app --reload` compatibility.

## Result

Repository now follows modular, async-safe, event-driven architecture with broker resilience and restart-safe state handling.
