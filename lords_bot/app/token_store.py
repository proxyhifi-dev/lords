import os
import json
import time
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("lords_bot.auth")

class TokenStore:
    """
    Handles storing and loading FYERS tokens with enhanced security and reliability.
    """
    FILE_PATH = Path("lords_bot/token.json")

    @classmethod
    def save(cls, data: dict[str, Any]) -> None:
        """Saves token data securely and atomically."""
        data["timestamp"] = int(time.time())
        cls.FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        
        temp_path = cls.FILE_PATH.with_suffix(".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            
            # Atomic swap
            os.replace(temp_path, cls.FILE_PATH)
            
            # Set restrictive permissions (Read/Write for owner only)
            if os.name != 'nt': # Linux/macOS
                os.chmod(cls.FILE_PATH, 0o600)
                
            logger.info("Token saved successfully.")
        except Exception as e:
            logger.error(f"Failed to save token: {e}")
            if temp_path.exists():
                temp_path.unlink()

    @classmethod
    def load(cls) -> dict[str, Any] | None:
        if not cls.FILE_PATH.exists():
            return None
        try:
            with open(cls.FILE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error("Token file is corrupted.")
            return None
        except Exception as e:
            logger.error(f"Unexpected error loading token: {e}")
            return None

    @staticmethod
    def is_expired(data: dict[str, Any], expiry_seconds: int = 21600) -> bool:
        """
        FYERS tokens typically last for a single trading day.
        21600 seconds = 6 hours.
        """
        ts = data.get("timestamp")
        if not ts:
            return True
        return (int(time.time()) - ts) > expiry_seconds