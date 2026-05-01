"""
Enhanced logger with parallel JSONL event stream
"""
from __future__ import annotations
import logging
import sys
import json
import threading
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from backend.app.core.config_loader import get_settings

_configured = False
_event_lock = threading.Lock()
IST = ZoneInfo("Asia/Kolkata")

def configure_logging() -> None:
    global _configured
    if _configured: return
    _configured = True
    settings = get_settings()
    Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
    
    # Reduce tick spam: set market_scheduler to WARNING
    fmt = "%(asctime)s  %(levelname)-8s  %(name)-26s  %(message)s"
    logging.basicConfig(
        level=logging.INFO, format=fmt, datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(settings.log_file, encoding="utf-8"),
        ],
    )
    # Suppress tick noise
    logging.getLogger("market_scheduler").setLevel(logging.WARNING)

def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)

def log_event(event_type: str, **fields):
    """
    Structured event logger (JSONL). 
    Use this for: ENTRY, EXIT, T1_BOOKED, STOPLOSS, SIGNAL_GENERATED, etc.
    Parseable with: pd.read_json('events.jsonl', lines=True)
    """
    settings = get_settings()
    event_path = Path(settings.log_file).parent / "events.jsonl"
    rec = {
        "ts": datetime.now(IST).isoformat(),
        "type": event_type,
        **fields
    }
    with _event_lock:
        with open(event_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
