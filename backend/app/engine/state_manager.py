# backend/app/engine/state_manager.py

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
# STATE MODEL (PRODUCTION-GRADE)
# ─────────────────────────────────────────────────────────
@dataclass
class RuntimeState:
    """Complete runtime state with validation."""
    # Market data
    spot_price: float | None = None
    orb_high: float | None = None
    orb_low: float | None = None
    
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
    
    # Execution safety
    last_order_failed: bool = False
    circuit_breaker_open: bool = False
    
    # Metadata
    trade_date: str = ""
    version: str = "5.1"
    last_updated: str = ""
    
    def __post_init__(self):
        if self.positions is None:
            self.positions = {}
        if not self.trade_date:
            self.trade_date = datetime.now().date().isoformat()
        if not self.last_updated:
            self.last_updated = datetime.now().isoformat()
    
    def validate(self) -> bool:
        """Validate state integrity."""
        try:
            # Check P&L consistency
            total_pnl = self.realized_pnl + self.unrealized_pnl
            if abs(total_pnl - self.live_pnl) > 0.01:  # 1 paisa tolerance
                logger.error("P&L inconsistency detected", extra={
                    "realized": self.realized_pnl,
                    "unrealized": self.unrealized_pnl,
                    "live": self.live_pnl
                })
                return False
            
            # Check trade count consistency
            if self.active_trade and self.trade_count < 1:
                logger.error("Trade count inconsistency")
                return False
            
            return True
            
        except Exception as exc:
            logger.error(f"State validation failed: {exc}")
            return False

class StateManager:
    """
    Production-grade state management with PostgreSQL persistence,
    Redis caching, and crash recovery.
    """
    
    def __init__(self):
        self._db_path = Path(settings.state_file).with_suffix('.db')
        self._backup_path = self._db_path.with_suffix('.db.backup')
        self._redis: Optional[redis.Redis] = None
        self._lock = asyncio.Lock()
        self._cache: Dict[str, Any] = {}
        self._journal: list = []
        
        # Initialize storage
        self._init_database()
        self._init_redis()
        
        # Load state
        self._state = self._load_state()
    
    def _init_database(self):
        """Initialize SQLite database with schema."""
        try:
            with sqlite3.connect(self._db_path) as conn:
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
        """Initialize Redis connection for caching."""
        try:
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
            self._redis = redis.from_url(redis_url, decode_responses=True)
        except Exception as exc:
            logger.warning(f"Redis initialization failed, using memory cache: {exc}")
            self._redis = None
    
    async def snapshot(self) -> RuntimeState:
        """Get current state snapshot with validation."""
        async with self._lock:
            # Try cache first
            if self._redis:
                try:
                    cached = await self._redis.get("state")
                    if cached:
                        state_dict = json.loads(cached)
                        state = RuntimeState(**state_dict)
                        if state.validate():
                            return state
                except Exception as exc:
                    logger.warning(f"Cache read failed: {exc}")
            
            # Fallback to database
            state = self._load_state()
            if not state.validate():
                logger.error("State validation failed, attempting recovery")
                state = await self._recover_state()
            
            # Update cache
            if self._redis:
                try:
                    await self._redis.set("state", json.dumps(asdict(state)), ex=30)
                except Exception as exc:
                    logger.warning(f"Cache write failed: {exc}")
            
            return state
    
    async def load(self) -> None:
        """Load state from database (for backward compatibility)."""
        async with self._lock:
            self._state = self._load_state()
            logger.info("State loaded from database")
    
    def _load_state(self) -> RuntimeState:
        """Load state from database."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.execute("SELECT value FROM state WHERE key = 'runtime'")
                row = cursor.fetchone()
                
                if row:
                    state_dict = json.loads(row[0])
                    return RuntimeState(**state_dict)
                else:
                    # Initialize default state
                    default_state = RuntimeState()
                    self._save_state(default_state)
                    return default_state
                    
        except Exception as exc:
            logger.error(f"State load failed: {exc}")
            return RuntimeState()  # Fallback
    
    async def update(self, **kwargs) -> None:
        """Update state with journaling."""
        async with self._lock:
            try:
                # Journal the change
                await self._journal_event("STATE_UPDATE", {
                    "old_state": asdict(self._state),
                    "updates": kwargs,
                    "timestamp": datetime.now().isoformat()
                })
                
                # Apply updates
                for key, value in kwargs.items():
                    if hasattr(self._state, key):
                        setattr(self._state, key, value)
                    else:
                        logger.warning("Unknown state key", extra={"key": key})
                
                self._state.last_updated = datetime.now().isoformat()
                
                # Validate
                if not self._state.validate():
                    logger.error("State update validation failed")
                    await self._journal_event("VALIDATION_FAILED", {"updates": kwargs})
                    return
                
                # Save to database
                self._save_state(self._state)
                
                # Update cache
                if self._redis:
                    try:
                        await self._redis.set("state", json.dumps(asdict(self._state)), ex=30)
                    except Exception as exc:
                        logger.warning(f"Cache update failed: {exc}")
                
                # Clear cache
                self._cache.clear()
                
            except Exception as exc:
                logger.error(f"State update failed: {exc}", exc_info=True)
                await self._journal_event("UPDATE_FAILED", {"error": str(exc), "updates": kwargs})
    
    def _save_state(self, state: RuntimeState) -> None:
        """Save state to database with backup."""
        try:
            state_json = json.dumps(asdict(state), default=str)
            
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                    ("runtime", state_json)
                )
                conn.commit()
            
            # Create backup
            if self._db_path.exists():
                import shutil
                shutil.copy2(self._db_path, self._backup_path)
                
        except Exception as exc:
            logger.error(f"State save failed: {exc}")
            raise
    
    async def _journal_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Journal state changes for audit trail."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO journal (event_type, data) VALUES (?, ?)",
                    (event_type, json.dumps(data))
                )
                conn.commit()
                
            # Keep journal size manageable
            self._cleanup_journal()
            
        except Exception as exc:
            logger.error(f"Journal write failed: {exc}")
    
    def _cleanup_journal(self) -> None:
        """Clean up old journal entries."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                # Keep last 1000 entries
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
            
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.execute("""
                    SELECT data FROM journal 
                    WHERE event_type = 'STATE_UPDATE' 
                    ORDER BY id DESC LIMIT 1
                """)
                row = cursor.fetchone()
                
                if row:
                    last_update = json.loads(row[0])
                    old_state = last_update.get("old_state", {})
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
                # Journal the reset
                await self._journal_event("DAILY_RESET", {
                    "old_state": asdict(self._state),
                    "timestamp": datetime.now().isoformat()
                })
                
                # Reset daily values
                self._state.daily_pnl = 0.0
                self._state.trade_count = 0
                self._state.trade_date = datetime.now().date().isoformat()
                self._state.cooldown_active = False
                self._state.cooldown_until = None
                self._state.last_updated = datetime.now().isoformat()
                
                # Keep persistent values
                # trading_enabled, peak_equity, etc. persist across days
                
                # Save
                self._save_state(self._state)
                
                logger.info("Daily reset completed", extra={
                    "new_date": self._state.trade_date,
                    "peak_equity": self._state.peak_equity
                })
                
            except Exception as exc:
                logger.error(f"Daily reset failed: {exc}", exc_info=True)
    
    async def get_journal(self, limit: int = 100) -> list:
        """Get recent journal entries for audit."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.execute("""
                    SELECT timestamp, event_type, data 
                    FROM journal 
                    ORDER BY id DESC LIMIT ?
                """, (limit,))
                
                return [{
                    "timestamp": row[0],
                    "event_type": row[1],
                    "data": json.loads(row[2])
                } for row in cursor.fetchall()]
                
        except Exception as exc:
            logger.error(f"Journal read failed: {exc}")
            return []
    
    @property
    def lock(self):
        """Access to the state lock for external synchronization."""
        return self._lock
    
    @property
    def equity(self) -> float:
        """Calculate current equity."""
        return settings.capital + self._state.realized_pnl + self._state.unrealized_pnl

# Global instance
state_manager = StateManager()