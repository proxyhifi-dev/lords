from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from models import DashboardState
from option_chain_service import OptionChainService
from samco_client import SamcoClient
from strategy_engine import StrategyEngine

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("app")

state = DashboardState()
samco_client = SamcoClient()
option_service = OptionChainService(samco_client)
strategy_engine = StrategyEngine()


async def polling_loop() -> None:
    while True:
        try:
            snapshot = await option_service.fetch_snapshot()
            signal = strategy_engine.generate_signal(snapshot)
            state.snapshot = snapshot
            state.signal = signal
            state.last_error = None
        except Exception as exc:  # noqa: BLE001 - background resilience
            state.last_error = str(exc)
            logger.exception("polling_error")
        await asyncio.sleep(settings.poll_interval_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(polling_loop())
    try:
        yield
    finally:
        task.cancel()
        await samco_client.close()


app = FastAPI(title="SAMCO NIFTY Option Bot", lifespan=lifespan)
frontend_dir = Path(__file__).resolve().parents[1] / "frontend"
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(frontend_dir / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "error": state.last_error or ""}


@app.get("/index-price")
async def index_price() -> dict[str, float | None]:
    return {
        "price": state.snapshot.spot_price if state.snapshot else None,
        "atm_strike": state.snapshot.atm_strike if state.snapshot else None,
    }


@app.get("/option-chain")
async def option_chain() -> dict:
    if not state.snapshot:
        return {"snapshot": None, "error": state.last_error}
    return state.snapshot.model_dump(mode="json")


@app.get("/signal")
async def signal() -> dict:
    if not state.signal:
        return {"signal": "NEUTRAL", "reason": state.last_error or "No data"}
    return state.signal.model_dump(mode="json")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
