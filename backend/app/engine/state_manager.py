# backend/app/engine/state_manager.py
#
# Changes vs v52:
#   1. Added 4 fields to RuntimeState: orb_open, orb_close, today_open, prev_day_close
#      Without these, scheduler's `state.update(orb_open=...)` was silently dropped
#      on the floor, breaking trend score persistence across restarts.
#   2. Added _safe_int() helper. Coerces version field to int even if DB has "5.1"
#      from an old version. This was the cause of 440+ State save errors on Apr 29.
#   3. Used _safe_int in: __post_init__, _load_state, update(), update_cas(), _save_state
#   4. _save_state no longer re-raises when backup fails (backup is best-effort)
#   5. Logger now includes the offending key when it warns about unknown keys
#      (so silent drops show up in logs)

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
import redis.asyncio as redis
from contextlib import asynccontextmanager

from backend.app.core.config_loader import get_settings
from backend.app.utils.logger import get_logger

settings = get_settings()
logger = get_logger("state_manager")


# ─────────────────────────────────────────────────────────
# UTIL: defensive int coercion
# ─────────────────────────────────────────────────────────
def _safe_int(value: Any, default: int = 1) -> int:
    """
    Coerce anything to int. Handles 'int-like' strings ('5'), float-strings ('5.1'),
    None, and garbage. Floats truncated. Falls back to default on failure.

    Reason: version field has historically been stored as '5.1' (str), which crashes
    `int('5.1')`. This helper makes version handling bulletproof.
    """
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


# ─────────────────────────────────────────────────────────
# STATE MODEL
# ─────────────────────────────────────────────────────────
@dataclass
class RuntimeState:
    """Complete runtime state with validation."""
    # Market data
    spot_price: float | None = None
    orb_high: float | None = None
    orb_low: float | None = None

    # ✅ NEW — trend variables persisted for restart safety
    orb_open: float | None = None
    orb_close: float | None = None
    today_open: float | None = None
    prev_day_close: float | None = None

    # Signals
    signal: str | None = None
    signal_meta: Dict[str, Any] | None = None

    # Positions
    active_trade: Dict[str, Any] | None = None
    positions: Dict[str, float] = None  # symbol -> notional

    # P&L
    daily_pnl: float = 0.0
    live_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    peak_equity: float = 0.0

    # Trading controls
    trade_count: int = 0
    trading_enabled: bool = True
    trading_mode: str = "PAPER"
    bot_running: bool = False

    # Risk management
    cooldown_active: bool = False
    cooldown_until: str | None = None
    last_risk_breach: str | None = None
    consecutive_losses: int = 0

    # Execution safety
    last_order_failed: bool = False
    circuit_breaker_open: bool = False

    # Metadata
    trade_date: str = ""
    version: int = 1
    last_updated: str = ""

    def __post_init__(self):
        if self.positions is None:
            self.positions = {}
        if not self.trade_date:
            self.trade_date = datetime.now().date().isoformat()
        if not self.last_updated:
            self.last_updated = datetime.now().isoformat()
        # Defensive: normalize version on construction.
        # If DB ever holds '5.1' or 5.1, this normalizes it before any update().
        self.version = _safe_int(self.version, default=1)

    def validate(self) -> bool:
        """Validate state integrity."""
        try:
            # Check P&L consistency (LIVE mode only — paper positions are simulated)
            if settings.is_live:
                total_pnl = self.realized_pnl + self.unrealized_pnl
                if abs(total_pnl - self.live_pnl) > 0.01:
                    logger.error("P&L inconsistency detected", extra={
                        "realized": self.realized_pnl,
                        "unrealized": self.unrealized_pnl,
                        "live": self.live_pnl,
                    })
                    return False
            else:
                logger.debug("PAPER MODE: skipping strict P&L validation")

            if self.active_trade and self.trade_count < 1:
                logger.error("Trade count inconsistency")
                return False

            return True

        except Exception as exc:
            logger.error(f"State validation failed: {exc}")
            return False


class StateManager:
    """
    Production-grade state management with SQLite persistence,
    optional Redis caching, and crash recovery.
    """

    def __init__(self):
        self._db_path = Path(settings.state_file).with_suffix('.db')
        self._backup_path = self._db_path.with_suffix('.db.backup')
        self._redis: Optional[redis.Redis] = None
        self._lock = asyncio.Lock()
        self._cache: Dict[str, Any] = {}
        self._journal: list = []

        self._init_database()
        self._init_redis()
        self._state = self._load_state()

    def _init_database(self):
        """Initialize SQLite database with schema."""
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS state (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS journal (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        event_type TEXT,
                        data TEXT
                    )
                """)
                conn.commit()
        except Exception as exc:
            logger.error(f"Database initialization failed: {exc}")
            raise

    def _init_redis(self):
        """Initialize Redis connection for caching (optional)."""
        use_redis = os.getenv("USE_REDIS")
        if use_redis != "true":
            logger.info(f"🚫 Redis disabled (USE_REDIS={use_redis})")
            self._redis = None
            return
        try:
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
            self._redis = redis.from_url(redis_url, decode_responses=True)
            logger.info("✅ Redis client initialized")
        except Exception as exc:
            logger.warning(f"Redis init failed: {exc}")
            self._redis = None

    async def snapshot(self) -> RuntimeState:
        """Get current state snapshot with validation."""
        async with self._lock:
            if self._redis:
                try:
                    cached = await self._redis.get("state")
                    if cached:
                        state_dict = json.loads(cached)
                        # Defensive: normalize version even from cache
                        state_dict["version"] = _safe_int(state_dict.get("version"), default=1)
                        state = RuntimeState(**state_dict)
                        if state.validate():
                            return state
                except Exception:
                    self._redis = None

            state = self._load_state()
            if not state.validate():
                logger.error("State validation failed, attempting recovery")
                state = await self._recover_state()

            if self._redis:
                try:
                    await self._redis.set("state", json.dumps(asdict(state), default=str), ex=30)
                except Exception:
                    self._redis = None

            return state

    async def load(self) -> None:
        """Load state from database (compat shim)."""
        async with self._lock:
            self._state = self._load_state()
            logger.info("State loaded from database")

    def _load_state(self) -> RuntimeState:
        """Load state from database."""
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                cursor = conn.execute("SELECT value FROM state WHERE key = 'runtime'")
                row = cursor.fetchone()
                if row:
                    state_dict = json.loads(row[0])
                    # Defensive: normalize version BEFORE constructing dataclass
                    state_dict["version"] = _safe_int(state_dict.get("version"), default=1)
                    # Drop unknown keys silently — RuntimeState may have evolved
                    valid_keys = {f.name for f in RuntimeState.__dataclass_fields__.values()}
                    state_dict = {k: v for k, v in state_dict.items() if k in valid_keys}
                    return RuntimeState(**state_dict)
                else:
                    default_state = RuntimeState()
                    self._save_state(default_state)
                    return default_state
        except Exception as exc:
            logger.error(f"State load failed: {exc}")
            return RuntimeState()

    async def update(self, **kwargs) -> None:
        """Update state with journaling. Never crashes the caller."""
        async with self._lock:
            try:
                await self._journal_event("STATE_UPDATE", {
                    "old_state": asdict(self._state),
                    "updates": kwargs,
                    "timestamp": datetime.now().isoformat(),
                })

                # Apply updates — log unknown keys with the actual key name
                for key, value in kwargs.items():
                    if hasattr(self._state, key):
                        setattr(self._state, key, value)
                    else:
                        # Fixed: include the key in the message itself, not in extra=
                        logger.warning("Unknown state key dropped: %s", key)

                # Defensive version bump (uses _safe_int — was the int('5.1') crash)
                self._state.version = _safe_int(self._state.version, default=1) + 1
                self._state.last_updated = datetime.now().isoformat()

                if not self._state.validate():
                    logger.error("State update validation failed")
                    await self._journal_event("VALIDATION_FAILED", {"updates": kwargs})
                    return

                self._save_state(self._state)

                if self._redis:
                    try:
                        await self._redis.set("state", json.dumps(asdict(self._state), default=str), ex=30)
                    except Exception:
                        self._redis = None

                self._cache.clear()

            except Exception as exc:
                logger.error(f"State update failed: {exc}", exc_info=True)
                await self._journal_event("UPDATE_FAILED", {"error": str(exc), "updates": kwargs})
                # Note: we DO NOT re-raise — state update failure must not crash the caller's loop

    async def update_cas(self, expected_version: int, **kwargs) -> bool:
        """Compare-and-swap state update to prevent stale writes."""
        async with self._lock:
            current_version = _safe_int(self._state.version, default=1)
            expected_version = _safe_int(expected_version, default=1)
            if current_version != expected_version:
                await self._journal_event("CAS_REJECTED", {
                    "expected_version": expected_version,
                    "actual_version": current_version,
                    "updates": kwargs,
                })
                return False
            for key, value in kwargs.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)
                else:
                    logger.warning("Unknown state key dropped (CAS): %s", key)
            self._state.version = current_version + 1
            self._state.last_updated = datetime.now().isoformat()
            if not self._state.validate():
                return False
            self._save_state(self._state)
            if self._redis:
                try:
                    await self._redis.set("state", json.dumps(asdict(self._state), default=str), ex=30)
                except Exception:
                    self._redis = None
            return True

    def _save_state(self, state: RuntimeState) -> None:
        """
        Save state to DB. Backup is best-effort (never raises).

        Fixed from v52: previously the backup failure (Errno 22 on Windows
        path) would propagate up and the primary save was reported as failed
        even though the SQLite write had succeeded.
        """
        # Defensive: coerce version one more time before serialization
        state.version = _safe_int(state.version, default=1)
        state_json = json.dumps(asdict(state), default=str)

        # PRIMARY: must succeed
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                    ("runtime", state_json),
                )
                conn.commit()
        except Exception as exc:
            logger.error("State save failed (primary): db_path=%s err=%s", self._db_path, exc)
            raise  # primary save failure IS critical

        # BACKUP: best-effort, never raises
        self._safe_backup()

    def _safe_backup(self) -> None:
        """Safely create backup without ever raising."""
        try:
            if self._db_path.exists():
                import shutil
                self._backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(self._db_path), str(self._backup_path))
        except Exception as exc:
            logger.warning("State backup skipped: %s", exc)

    async def _journal_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Journal state changes for audit trail."""
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    "INSERT INTO journal (event_type, data) VALUES (?, ?)",
                    (event_type, json.dumps(data, default=str)),
                )
                conn.commit()
            self._cleanup_journal()
        except Exception as exc:
            logger.error(f"Journal write failed: {exc}")

    def _cleanup_journal(self) -> None:
        """Clean up old journal entries (keep last 1000)."""
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute("""
                    DELETE FROM journal WHERE id NOT IN (
                        SELECT id FROM journal ORDER BY id DESC LIMIT 1000
                    )
                """)
                conn.commit()
        except Exception as exc:
            logger.warning(f"Journal cleanup failed: {exc}")

    async def _recover_state(self) -> RuntimeState:
        """Attempt state recovery from journal."""
        try:
            logger.info("Attempting state recovery from journal")
            with sqlite3.connect(str(self._db_path)) as conn:
                cursor = conn.execute("""
                    SELECT data FROM journal 
                    WHERE event_type = 'STATE_UPDATE' 
                    ORDER BY id DESC LIMIT 1
                """)
                row = cursor.fetchone()
                if row:
                    last_update = json.loads(row[0])
                    old_state = last_update.get("old_state", {})
                    old_state["version"] = _safe_int(old_state.get("version"), default=1)
                    valid_keys = {f.name for f in RuntimeState.__dataclass_fields__.values()}
                    old_state = {k: v for k, v in old_state.items() if k in valid_keys}
                    return RuntimeState(**old_state)
            logger.warning("Recovery failed, using default state")
            return RuntimeState()
        except Exception as exc:
            logger.error(f"State recovery failed: {exc}")
            return RuntimeState()

    async def daily_reset(self) -> None:
        """Perform daily reset with full audit trail."""
        async with self._lock:
            try:
                await self._journal_event("DAILY_RESET", {
                    "old_state": asdict(self._state),
                    "timestamp": datetime.now().isoformat(),
                })
                self._state.daily_pnl = 0.0
                self._state.trade_count = 0
                self._state.trade_date = datetime.now().date().isoformat()
                self._state.cooldown_active = False
                self._state.cooldown_until = None
                self._state.last_updated = datetime.now().isoformat()
                self._save_state(self._state)
                logger.info("Daily reset completed", extra={
                    "new_date": self._state.trade_date,
                    "peak_equity": self._state.peak_equity,
                })
            except Exception as exc:
                logger.error(f"Daily reset failed: {exc}", exc_info=True)

    async def get_journal(self, limit: int = 100) -> list:
        """Get recent journal entries for audit."""
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                cursor = conn.execute("""
                    SELECT timestamp, event_type, data 
                    FROM journal 
                    ORDER BY id DESC LIMIT ?
                """, (limit,))
                return [{
                    "timestamp": row[0],
                    "event_type": row[1],
                    "data": json.loads(row[2]),
                } for row in cursor.fetchall()]
        except Exception as exc:
            logger.error(f"Journal read failed: {exc}")
            return []

    @property
    def lock(self):
        return self._lock

    @property
    def equity(self) -> float:
        return settings.capital + self._state.realized_pnl + self._state.unrealized_pnl


# Global instance
state_manager = StateManager()