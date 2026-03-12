from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from config import settings, yaml_config
from utils import read_json_file, write_json_file


class TTLCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time() + self.ttl, value)

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self._store:
            return default
        expires_at, value = self._store[key]
        if time.time() > expires_at:
            self._store.pop(key, None)
            return default
        return value


cache = TTLCache(ttl_seconds=yaml_config.cache_expiry_seconds)
cache_file = Path(settings.data_cache_file)


def persist_cache_snapshot(snapshot: dict) -> None:
    write_json_file(cache_file, snapshot)


def load_cached_snapshot() -> dict:
    return read_json_file(cache_file, default={})
