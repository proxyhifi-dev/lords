from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def write_json_file(path: Path, data: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(data, indent=2))


def is_market_hours() -> bool:
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:
        return False
    current = now_ist.time()
    return time(9, 15) <= current <= time(15, 30)


async def with_retries(coro_factory: Callable[[], Awaitable[Any]], retries: int = 3, delay: float = 0.3) -> Any:
    last_error = None
    for attempt in range(retries):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries - 1:
                await asyncio.sleep(delay * (attempt + 1))
    raise RuntimeError(f'All retries failed: {last_error}') from last_error


def configure_logging(log_file: Path) -> None:
    ensure_parent(log_file)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s - %(message)s',
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
    )
