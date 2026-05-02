import logging
import os
from pathlib import Path

# Create logs directory if it doesn't exist
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

def setup_file_logging(logger_name: str):
    """Setup file-based logging for permanent audit trail"""

    logger = logging.getLogger(logger_name)

    # File handler - logs to file permanently
    file_handler = logging.FileHandler(f'logs/{logger_name}_audit.log')
    file_handler.setLevel(logging.DEBUG)

    # Format: timestamp | logger | level | message
    formatter = logging.Formatter(
        '%(asctime)s | %(name)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)

    return logger