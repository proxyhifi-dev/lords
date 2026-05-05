from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import redis.asyncio as redis
except Exception:
    redis = None

from backend.app.core.config_loader import get_settings
from backend.app.utils.logger import get_logger

settings = get_settings()
logger = get_logger("state_manager")


def _safe_int(value: Any, default: int = 1) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


@dataclass
class RuntimeState:
    spot_price: float | None = None

    signal: str | None = None
    signal_meta: Dict[str, Any] | None = None

    active_trade: Dict[str, Any] | None = None
    positions: Dict[str, float] = field(default_factory=dict)

    daily_pnl: float = 0.0
    live_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    peak_equity: float = 0.0

    trade_count: int = 0
    trading_enabled: bool = True
    trading_mode: str = "PAPER"
    bot_running: bool = False

    cooldown_active: bool = False
    cooldown_until: str | None = None
    last_risk_breach: str | None = None
    consecutive_losses: int = 0
    last_iron_condor_month: int | None = None

    last_trade_date: str | None = None
    last_ic_trade_date: str | None = None
    iron_condor_trade_date: str | None = None

    last_order_failed: bool = False
    circuit_breaker_open: bool = False

    trade_date: str = ""
    version: int = 1
    last_updated: str = ""

    def __post_init__(self):
        if not self.trade_date:
            self.trade_date = datetime.now().date().isoformat()
        if not self.last_updated:
            self.last_updated = datetime.now().isoformat()
        self.version = _safe_int(self.version, default=1)

    def validate(self) -> bool:
        try:
            if settings.is_live:
                total = self.realized_pnl + self.unrealized_pnl
                if abs(total - self.live_pnl) > 0.01:
                    logger.error(
                        "P&L inconsistency: realized=%.2f unrealized=%.2f live=%.2f",
                        self.realized_pnl,
                        self.unrealized_pnl,
                        self.live_pnl,
                    )
                    return False
            else:
                logger.debug("PAPER: skipping strict P&L validation")

            if self.active_trade and self.trade_count < 1:
                logger.error("Trade count inconsistency")
                return False

            if self.trade_count < 0:
                logger.error("Invalid negative trade_count=%s", self.trade_count)
                return False

            return True
        except Exception as exc:
            logger.error("State validation failed: %s", exc)
            return False


class StateManager:
    def __init__(self):
        self._db_path = Path(settings.state_file).with_suffix(".db")
        self._backup_path = self._db_path.with_suffix(".db.backup")
        self._redis: Optional[Any] = None
        self._lock = asyncio.Lock()
        self._cache: Dict[str, Any] = {}

        self._init_database()
        self._init_redis()
        self._state = self._load_state()

    def _init_database(self):
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS state (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS journal (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        event_type TEXT,
                        data TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS idempotency_keys (
                        key TEXT PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS uncertain_orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id TEXT,
                        state TEXT,
                        filled_qty INTEGER,
                        avg_price REAL,
                        reason TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.error("DB init failed: %s", exc)
            raise

    def _init_redis(self):
        if not getattr(settings, "use_redis", False) or redis is None:
            logger.info("Redis disabled (USE_REDIS=%s)", getattr(settings, "use_redis", False))
            return

        try:
            self._redis = redis.from_url(
                "redis://localhost:6379",
                decode_responses=True,
            )
            logger.info("Redis initialized")
        except Exception as exc:
            logger.warning("Redis init failed: %s", exc)
            self._redis = None

    async def snapshot(self) -> RuntimeState:
        async with self._lock:
            if self._redis:
                try:
                    cached = await self._redis.get("state")
                    if cached:
                        data = json.loads(cached)
                        data["version"] = _safe_int(data.get("version"), default=1)
                        valid_fields = set(RuntimeState.__dataclass_fields__)
                        state = RuntimeState(**{k: v for k, v in data.items() if k in valid_fields})
                        if state.validate():
                            self._state = state
                            return state
                except Exception:
                    self._redis = None

            state = self._load_state()
            if not state.validate():
                logger.error("Validation failed — recovering")
                state = await self._recover_state()

            self._state = state

            if self._redis:
                try:
                    await self._redis.set("state", json.dumps(asdict(state), default=str), ex=30)
                except Exception:
                    self._redis = None

            return state

    async def load(self) -> None:
        async with self._lock:
            self._state = self._load_state()
            logger.info("State loaded from DB")

    def _load_state(self) -> RuntimeState:
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                row = conn.execute(
                    "SELECT value FROM state WHERE key = 'runtime'"
                ).fetchone()

                if row:
                    data = json.loads(row[0])
                    data["version"] = _safe_int(data.get("version"), default=1)
                    valid_fields = set(RuntimeState.__dataclass_fields__)
                    return RuntimeState(**{k: v for k, v in data.items() if k in valid_fields})

                default_state = RuntimeState()
                self._save_state(default_state)
                return default_state

        except Exception as exc:
            logger.error("State load failed: %s", exc)
            return RuntimeState()

    async def update(self, **kwargs) -> None:
        async with self._lock:
            try:
                await self._journal_event(
                    "STATE_UPDATE",
                    {"updates": kwargs, "ts": datetime.now().isoformat()},
                )

                for key, value in kwargs.items():
                    if hasattr(self._state, key):
                        setattr(self._state, key, value)
                    else:
                        logger.warning("Unknown state key dropped: %s", key)

                self._state.version = _safe_int(self._state.version, default=1) + 1
                self._state.last_updated = datetime.now().isoformat()

                if not self._state.validate():
                    logger.error("State update validation failed")
                    return

                self._save_state(self._state)

                if self._redis:
                    try:
                        await self._redis.set(
                            "state",
                            json.dumps(asdict(self._state), default=str),
                            ex=30,
                        )
                    except Exception:
                        self._redis = None

                self._cache.clear()

            except Exception as exc:
                logger.error("State update failed: %s", exc, exc_info=True)
                await self._journal_event("UPDATE_FAILED", {"error": str(exc)})

    async def update_cas(self, expected_version: int, **kwargs) -> bool:
        async with self._lock:
            current = _safe_int(self._state.version, default=1)
            expected = _safe_int(expected_version, default=1)

            if current != expected:
                return False

            for key, value in kwargs.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)
                else:
                    logger.warning("Unknown state key dropped (CAS): %s", key)

            self._state.version = current + 1
            self._state.last_updated = datetime.now().isoformat()

            if not self._state.validate():
                return False

            self._save_state(self._state)

            if self._redis:
                try:
                    await self._redis.set(
                        "state",
                        json.dumps(asdict(self._state), default=str),
                        ex=30,
                    )
                except Exception:
                    self._redis = None

            return True

    def _save_state(self, state: RuntimeState) -> None:
        state.version = _safe_int(state.version, default=1)
        state_json = json.dumps(asdict(state), default=str)

        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                    ("runtime", state_json),
                )
                conn.commit()
        except Exception as exc:
            logger.error("State save failed: db=%s err=%s", self._db_path, exc)
            raise

        self._safe_backup()

    def _safe_backup(self) -> None:
        try:
            if self._db_path.exists():
                import shutil

                self._backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(self._db_path), str(self._backup_path))
        except Exception as exc:
            logger.warning("State backup skipped: %s", exc)

    async def _journal_event(self, event_type: str, data: Dict[str, Any]) -> None:
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    "INSERT INTO journal (event_type, data) VALUES (?, ?)",
                    (event_type, json.dumps(data, default=str)),
                )
                conn.commit()

            self._cleanup_journal()
        except Exception as exc:
            logger.error("Journal write failed: %s", exc)

    def _cleanup_journal(self) -> None:
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    """
                    DELETE FROM journal WHERE id NOT IN (
                        SELECT id FROM journal ORDER BY id DESC LIMIT 1000
                    )
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning("Journal cleanup failed: %s", exc)

    async def _recover_state(self) -> RuntimeState:
        try:
            logger.info("Recovering state from journal")
            with sqlite3.connect(str(self._db_path)) as conn:
                row = conn.execute(
                    """
                    SELECT data FROM journal
                    WHERE event_type = 'STATE_UPDATE'
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()

                if row:
                    updates = json.loads(row[0]).get("updates", {})
                    state = RuntimeState()
                    valid_fields = set(RuntimeState.__dataclass_fields__)
                    for key, value in updates.items():
                        if key in valid_fields:
                            setattr(state, key, value)
                    return state

            logger.warning("Recovery fallback: fresh state")
            return RuntimeState()

        except Exception as exc:
            logger.error("State recovery failed: %s", exc)
            return RuntimeState()

    async def daily_reset(self) -> None:
        async with self._lock:
            try:
                await self._journal_event("DAILY_RESET", {"ts": datetime.now().isoformat()})

                self._state.daily_pnl = 0.0
                self._state.live_pnl = 0.0
                self._state.unrealized_pnl = 0.0
                self._state.trade_count = 0
                self._state.trade_date = datetime.now().date().isoformat()
                self._state.signal = None
                self._state.signal_meta = None
                self._state.cooldown_active = False
                self._state.cooldown_until = None
                self._state.last_order_failed = False
                self._state.last_updated = datetime.now().isoformat()

                self._save_state(self._state)
                logger.info("Daily reset complete date=%s", self._state.trade_date)

            except Exception as exc:
                logger.error("Daily reset failed: %s", exc, exc_info=True)

    async def get_journal(self, limit: int = 100) -> list:
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                rows = conn.execute(
                    """
                    SELECT timestamp, event_type, data
                    FROM journal ORDER BY id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

            return [
                {
                    "timestamp": row[0],
                    "event_type": row[1],
                    "data": json.loads(row[2]),
                }
                for row in rows
            ]
        except Exception as exc:
            logger.error("Journal read failed: %s", exc)
            return []

    async def has_idempotency_key(self, key: str) -> bool:
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                row = conn.execute(
                    "SELECT 1 FROM idempotency_keys WHERE key = ?",
                    (key,),
                ).fetchone()
            return row is not None
        except Exception as exc:
            logger.error("Idempotency read failed: %s", exc)
            return False

    async def add_idempotency_key(self, key: str) -> None:
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO idempotency_keys (key) VALUES (?)",
                    (key,),
                )
                conn.commit()
        except Exception as exc:
            logger.error("Idempotency write failed: %s", exc)

    async def record_uncertain_order(self, record: Dict[str, Any]) -> None:
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO uncertain_orders (order_id, state, filled_qty, avg_price, reason)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.get("order_id"),
                        record.get("state"),
                        _safe_int(record.get("filled_qty"), default=0),
                        record.get("avg_price"),
                        record.get("reason"),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.error("Uncertain order write failed: %s", exc)

    @property
    def lock(self):
        return self._lock

    @property
    def equity(self) -> float:
        return settings.capital + self._state.realized_pnl + self._state.unrealized_pnl


state_manager = StateManager()