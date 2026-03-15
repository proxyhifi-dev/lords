from __future__ import annotations

import logging
from pathlib import Path

from config import settings


def configure_logging() -> None:
    Path(settings.logs_dir).mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(), logging.FileHandler(settings.trading_log_file)]
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        handlers=handlers,
    )
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
