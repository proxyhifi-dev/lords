"""
Enhanced logger with parallel JSONL event stream
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.app.core.config_loader import get_settings

_configured = False
_event_lock = threading.Lock()
IST = ZoneInfo("Asia/Kolkata")


def configure_logging() -> None:
    global _configured
    if _configured:
        return

    _configured = True
    settings = get_settings()
    Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s  %(levelname)-8s  %(name)-26s  %(message)s"
    stream = sys.stdout

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if not root.handlers:
        stream_handler = logging.StreamHandler(stream)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(
            logging.Formatter(fmt=fmt, datefmt="%Y-%m-%d %H:%M:%S")
        )

        file_handler = logging.FileHandler(settings.log_file, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(
            logging.Formatter(fmt=fmt, datefmt="%Y-%m-%d %H:%M:%S")
        )

        root.addHandler(stream_handler)
        root.addHandler(file_handler)

    logging.getLogger("market_scheduler").setLevel(logging.INFO)
    logging.getLogger("risk_manager").setLevel(logging.INFO)
    logging.getLogger("trading_engine").setLevel(logging.INFO)
    logging.getLogger("startup_manager").setLevel(logging.INFO)
    logging.getLogger("samco_client").setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def log_event(event_type: str, **fields) -> None:
    """
    Structured event logger (JSONL).
    Use this for: ENTRY, EXIT, T1_BOOKED, STOPLOSS, SIGNAL_GENERATED, etc.
    """
    settings = get_settings()
    event_path = Path(settings.log_file).parent / "events.jsonl"

    rec = {
        "ts": datetime.now(IST).isoformat(),
        "type": event_type,
        **fields,
    }

    with _event_lock:
        with open(event_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")