# backend/app/core/logging_config.py
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_FORMAT = "%(asctime)s | %(name)s | %(levelname)-8s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_LEVEL = logging.INFO
AUDIT_LEVEL = logging.DEBUG
MAX_LOG_BYTES = 5_000_000
BACKUP_COUNT = 5


class SafeUnicodeStreamHandler(logging.StreamHandler):
    """Console handler that avoids Windows cp1252 Unicode crashes."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except UnicodeEncodeError:
            try:
                message = self.format(record)
                safe_message = message.encode("ascii", errors="replace").decode("ascii")
                self.stream.write(safe_message + self.terminator)
                self.flush()
            except Exception:
                self.handleError(record)


def _formatter() -> logging.Formatter:
    return logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)


def _handler_exists(logger: logging.Logger, target_path: Path) -> bool:
    target = str(target_path.resolve())
    for handler in logger.handlers:
        filename = getattr(handler, "baseFilename", None)
        if filename and str(Path(filename).resolve()) == target:
            return True
    return False


def configure_root_logging(level: int = DEFAULT_LEVEL) -> None:
    root = logging.getLogger()
    root.setLevel(level)

    has_console_handler = any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in root.handlers
    )
    if not has_console_handler:
        console_handler = SafeUnicodeStreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(_formatter())
        root.addHandler(console_handler)

    root_log_path = LOG_DIR / "lords_bot.log"
    if not _handler_exists(root, root_log_path):
        file_handler = RotatingFileHandler(
            root_log_path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(_formatter())
        root.addHandler(file_handler)

    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)


def setup_file_logging(logger_name: str) -> logging.Logger:
    """Setup file-based logging for permanent audit trail."""
    configure_root_logging()

    logger = logging.getLogger(logger_name)
    logger.setLevel(AUDIT_LEVEL)
    logger.propagate = True

    audit_path = LOG_DIR / f"{logger_name}_audit.log"
    if not _handler_exists(logger, audit_path):
        file_handler = RotatingFileHandler(
            audit_path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(AUDIT_LEVEL)
        file_handler.setFormatter(_formatter())
        logger.addHandler(file_handler)

    return logger


def get_logger(logger_name: str) -> logging.Logger:
    return setup_file_logging(logger_name)
