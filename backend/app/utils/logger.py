from __future__ import annotations
import logging, sys
from pathlib import Path
from backend.app.core.config_loader import get_settings

_configured = False

def configure_logging() -> None:
    global _configured
    if _configured: return
    _configured = True
    settings = get_settings()
    Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s  %(levelname)-8s  %(name)-26s  %(message)s"
    logging.basicConfig(
        level=logging.INFO, format=fmt, datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(settings.log_file, encoding="utf-8"),
        ],
    )

def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
