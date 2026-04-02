# Production Runtime Notes

- Uses event-driven scheduler with `MarketFeedEngine -> TickStream -> CandleBuilder -> StrategyEngine`.
- Graceful shutdown is implemented via FastAPI lifespan hook (`scheduler.stop`).
- Persistent state is written to `backend/data/state.json` and trade journal to `backend/data/trade_log.jsonl`.
- Set credentials in `.env` (copy from `.env.example`).
