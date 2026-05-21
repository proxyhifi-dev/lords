# backend/app/engine/state_manager.py
from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import redis.asyncio as redis
except Exception:
    redis = None

from backend.app.core.config_loader import get_settings
from backend.app.utils.logger import get_logger

settings = get_settings()
logger = get_logger("state_manager")
IST = ZoneInfo("Asia/Kolkata")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return datetime.now(IST).date().isoformat()


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _safe_str(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _safe_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _safe_list_of_str(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _safe_str(item)
        if text:
            result.append(text)
    return result


def _safe_list_of_dict(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _safe_positions(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, float] = {}
    for key, raw_value in value.items():
        name = _safe_str(key)
        if not name:
            continue
        cleaned[name] = _safe_float(raw_value, 0.0)
    return cleaned


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


@dataclass
class RuntimeState:
    spot_price: float | None = None
    current_iv: float | None = None
    signal: str | None = None
    signal_meta: dict[str, Any] | None = None
    active_trade: dict[str, Any] | None = None
    positions: dict[str, float] = field(default_factory=dict)

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
    manual_intervention_required: bool = False
    emergency_flatten_verified: bool = False
    emergency_flatten_attempts: int = 0
    emergency_flatten_unclosed_symbols: list[str] = field(default_factory=list)
    emergency_flatten_last_error: str | None = None
    emergency_flatten_order_proof: list[dict[str, Any]] = field(default_factory=list)
    reconstructed_ic_status: str | None = None
    hedge_integrity_status: str | None = None
    broker_position_count: int = 0

    last_iron_condor_month: int | None = None
    last_iron_condor_date: str | None = None
    last_trade_date: str | None = None
    last_ic_trade_date: str | None = None
    iron_condor_trade_date: str | None = None

    last_order_failed: bool = False
    circuit_breaker_open: bool = False

    trade_date: str = ""
    version: int = 1
    last_updated: str = ""

    def __post_init__(self) -> None:
        self.normalize()

    def normalize(self) -> None:
        self.spot_price = None if self.spot_price in ("", None) else _safe_float(self.spot_price, 0.0)
        if self.spot_price is not None and self.spot_price <= 0:
            self.spot_price = None

        self.current_iv = None if self.current_iv in ("", None) else _safe_float(self.current_iv, 0.0)
        if self.current_iv is not None and self.current_iv <= 0:
            self.current_iv = None

        self.signal = _safe_str(self.signal)
        self.signal_meta = _safe_dict(self.signal_meta)
        self.active_trade = _safe_dict(self.active_trade)
        self.positions = _safe_positions(self.positions)

        self.daily_pnl = _safe_float(self.daily_pnl, 0.0)
        self.live_pnl = _safe_float(self.live_pnl, 0.0)
        self.unrealized_pnl = _safe_float(self.unrealized_pnl, 0.0)
        self.realized_pnl = _safe_float(self.realized_pnl, 0.0)
        self.peak_equity = _safe_float(self.peak_equity, 0.0)

        self.trade_count = max(_safe_int(self.trade_count, 0), 0)
        self.trading_enabled = _safe_bool(self.trading_enabled, True)
        self.trading_mode = (_safe_str(self.trading_mode, "PAPER") or "PAPER").upper()
        self.bot_running = _safe_bool(self.bot_running, False)

        self.cooldown_active = _safe_bool(self.cooldown_active, False)
        self.cooldown_until = _safe_str(self.cooldown_until)
        self.last_risk_breach = _safe_str(self.last_risk_breach)
        self.consecutive_losses = max(_safe_int(self.consecutive_losses, 0), 0)
        self.manual_intervention_required = _safe_bool(self.manual_intervention_required, False)
        self.emergency_flatten_verified = _safe_bool(self.emergency_flatten_verified, False)
        self.emergency_flatten_attempts = max(_safe_int(self.emergency_flatten_attempts, 0), 0)
        self.emergency_flatten_unclosed_symbols = _safe_list_of_str(
            self.emergency_flatten_unclosed_symbols
        )
        self.emergency_flatten_last_error = _safe_str(self.emergency_flatten_last_error)
        self.emergency_flatten_order_proof = _safe_list_of_dict(
            self.emergency_flatten_order_proof
        )
        self.reconstructed_ic_status = _safe_str(self.reconstructed_ic_status)
        self.hedge_integrity_status = _safe_str(self.hedge_integrity_status)
        self.broker_position_count = max(_safe_int(self.broker_position_count, 0), 0)

        month_value = _safe_int(self.last_iron_condor_month, 0)
        self.last_iron_condor_month = month_value if 1 <= month_value <= 12 else None

        self.last_iron_condor_date = _safe_str(self.last_iron_condor_date)
        self.last_trade_date = _safe_str(self.last_trade_date)
        self.last_ic_trade_date = _safe_str(self.last_ic_trade_date)
        self.iron_condor_trade_date = _safe_str(self.iron_condor_trade_date)

        self.last_order_failed = _safe_bool(self.last_order_failed, False)
        self.circuit_breaker_open = _safe_bool(self.circuit_breaker_open, False)

        self.trade_date = _safe_str(self.trade_date, _today_iso()) or _today_iso()
        self.version = max(_safe_int(self.version, 1), 1)
        self.last_updated = _safe_str(self.last_updated, _now_iso()) or _now_iso()

        if self.peak_equity <= 0:
            self.peak_equity = float(getattr(settings, "capital", 0.0) or 0.0)

    def validate(self) -> bool:
        try:
            if self.trade_count < 0:
                logger.error("State invalid: negative trade_count=%s", self.trade_count)
                return False

            if self.consecutive_losses < 0:
                logger.error(
                    "State invalid: negative consecutive_losses=%s",
                    self.consecutive_losses,
                )
                return False

            if self.active_trade and self.trade_count < 1:
                logger.error(
                    "State invalid: active_trade exists while trade_count=%s",
                    self.trade_count,
                )
                return False

            if settings.is_live:
                # Use a wide tolerance: live_pnl, realized_pnl, and unrealized_pnl are
                # updated in separate async calls so a transient mismatch during normal
                # operation must NOT trigger state recovery.
                expected_live = round(self.realized_pnl + self.unrealized_pnl, 2)
                actual_live = round(self.live_pnl, 2)
                if abs(expected_live - actual_live) > 50.0:
                    logger.warning(
                        "P&L drift detected (non-fatal): realized=%.2f unrealized=%.2f live=%.2f diff=%.2f",
                        self.realized_pnl,
                        self.unrealized_pnl,
                        self.live_pnl,
                        abs(expected_live - actual_live),
                    )

            return True
        except Exception as exc:
            logger.error("State validation failed: %s", exc)
            return False

    def reset_intraday_fields(self) -> None:
        self.daily_pnl = 0.0
        self.live_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self.trade_count = 0
        self.signal = None
        self.signal_meta = None
        self.cooldown_active = False
        self.cooldown_until = None
        self.last_order_failed = False
        self.manual_intervention_required = False
        self.emergency_flatten_verified = False
        self.emergency_flatten_attempts = 0
        self.emergency_flatten_unclosed_symbols = []
        self.emergency_flatten_last_error = None
        self.emergency_flatten_order_proof = []
        self.reconstructed_ic_status = None
        self.hedge_integrity_status = None
        self.broker_position_count = 0
        self.consecutive_losses = 0  # reset streak each day so prior days don't block new trading
        self.trade_date = _today_iso()
        self.last_updated = _now_iso()
        self.normalize()


class StateManager:
    def __init__(self) -> None:
        self._db_path = Path(settings.state_file).with_suffix(".db")
        self._backup_path = self._db_path.with_suffix(".db.backup")
        self._redis: Any | None = None
        self._lock = asyncio.Lock()
        self._state = RuntimeState()

        self._init_database()
        self._init_redis()
        self._state = self._load_state()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_database(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS state (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS journal (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        event_type TEXT NOT NULL,
                        data TEXT NOT NULL
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

            logger.info("State DB initialized path=%s", self._db_path)
        except Exception as exc:
            logger.error("DB init failed: %s", exc)
            raise

    def _init_redis(self) -> None:
        if not getattr(settings, "use_redis", False) or redis is None:
            logger.info("Redis disabled (USE_REDIS=%s)", getattr(settings, "use_redis", False))
            return

        try:
            redis_url = str(getattr(settings, "redis_url", "redis://localhost:6379") or "redis://localhost:6379")
            self._redis = redis.from_url(redis_url, decode_responses=True)
            logger.info("Redis initialized url=%s", redis_url)
        except Exception as exc:
            logger.warning("Redis init failed: %s", exc)
            self._redis = None

    def _serialize_state(self, state: RuntimeState) -> str:
        state.normalize()
        return _json_dumps(asdict(state))

    def _deserialize_state(self, raw: str) -> RuntimeState:
        payload = json.loads(raw)
        valid_fields = set(RuntimeState.__dataclass_fields__)
        filtered = {key: value for key, value in payload.items() if key in valid_fields}
        return RuntimeState(**filtered)

    def _save_state(self, state: RuntimeState) -> None:
        state.normalize()
        state_json = self._serialize_state(state)

        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO state (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    ("runtime", state_json),
                )
                # Write full-state checkpoint so crash recovery restores complete state.
                conn.execute(
                    "INSERT INTO journal (event_type, data) VALUES (?, ?)",
                    ("FULL_STATE", state_json),
                )
            self._safe_backup()
        except Exception as exc:
            logger.error("State save failed: db=%s err=%s", self._db_path, exc)
            raise

    def _load_state(self) -> RuntimeState:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM state WHERE key = ?",
                    ("runtime",),
                ).fetchone()

            if row and row["value"]:
                state = self._deserialize_state(row["value"])
                return self._apply_day_rollover_if_needed(state, persist=True)

            default_state = RuntimeState()
            self._save_state(default_state)
            return default_state
        except Exception as exc:
            logger.error("State load failed: %s", exc)
            return RuntimeState()

    async def snapshot(self) -> RuntimeState:
        async with self._lock:
            if self._redis:
                try:
                    cached = await self._redis.get("state")
                    if cached:
                        state = self._deserialize_state(cached)
                        state = self._apply_day_rollover_if_needed(state, persist=True)
                        if state.validate():
                            self._state = state
                            return state
                except Exception as exc:
                    logger.warning("Redis snapshot read failed, disabling Redis: %s", exc)
                    self._redis = None

            state = self._load_state()
            if not state.validate():
                logger.error("State validation failed — attempting recovery")
                state = await self._recover_state()

            self._state = state
            await self._refresh_redis_cache()
            return state

    async def load(self) -> None:
        async with self._lock:
            self._state = self._load_state()
            await self._refresh_redis_cache()
            logger.info("State loaded from DB")

    async def update(self, **kwargs: Any) -> None:
        async with self._lock:
            try:
                await self._journal_event(
                    "STATE_UPDATE",
                    {"updates": kwargs, "ts": _now_iso()},
                )

                for key, value in kwargs.items():
                    if hasattr(self._state, key):
                        setattr(self._state, key, value)
                    else:
                        logger.warning("Unknown state key dropped: %s", key)

                self._state.version = max(_safe_int(self._state.version, 1) + 1, 1)
                self._state.last_updated = _now_iso()
                self._state.normalize()
                self._state = self._apply_day_rollover_if_needed(self._state, persist=False)

                if not self._state.validate():
                    logger.error("State update validation failed")
                    return

                self._save_state(self._state)
                await self._refresh_redis_cache()

            except Exception as exc:
                logger.error("State update failed: %s", exc, exc_info=True)
                await self._journal_event(
                    "UPDATE_FAILED",
                    {"error": str(exc), "ts": _now_iso()},
                )

    async def update_cas(self, expected_version: int, **kwargs: Any) -> bool:
        async with self._lock:
            current_version = _safe_int(self._state.version, 1)
            if current_version != _safe_int(expected_version, 1):
                return False

            for key, value in kwargs.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)
                else:
                    logger.warning("Unknown state key dropped (CAS): %s", key)

            self._state.version = current_version + 1
            self._state.last_updated = _now_iso()
            self._state.normalize()
            self._state = self._apply_day_rollover_if_needed(self._state, persist=False)

            if not self._state.validate():
                return False

            self._save_state(self._state)
            await self._refresh_redis_cache()
            return True

    async def _refresh_redis_cache(self) -> None:
        if not self._redis:
            return

        try:
            await self._redis.set(
                "state",
                self._serialize_state(self._state),
                ex=30,
            )
        except Exception as exc:
            logger.warning("Redis cache write failed, disabling Redis: %s", exc)
            self._redis = None

    async def _journal_event(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO journal (event_type, data) VALUES (?, ?)",
                    (event_type, _json_dumps(data)),
                )
            self._cleanup_journal()
        except Exception as exc:
            logger.error("Journal write failed: %s", exc)

    def _cleanup_journal(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    DELETE FROM journal
                    WHERE id NOT IN (
                        SELECT id FROM journal ORDER BY id DESC LIMIT 1000
                    )
                    """
                )
        except Exception as exc:
            logger.warning("Journal cleanup failed: %s", exc)

    async def _recover_state(self) -> RuntimeState:
        try:
            logger.info("Recovering state from journal")

            with self._connect() as conn:
                # Prefer FULL_STATE checkpoints — these contain the complete serialised state.
                row = conn.execute(
                    """
                    SELECT data FROM journal
                    WHERE event_type = 'FULL_STATE'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()

            if row and row["data"]:
                state = self._deserialize_state(row["data"])
                state = self._apply_day_rollover_if_needed(state, persist=True)
                if state.validate():
                    logger.info("Recovered full state from FULL_STATE journal entry")
                    return state

            # Legacy fallback: reconstruct from STATE_UPDATE deltas (incomplete but better than nothing)
            logger.warning("No FULL_STATE checkpoint found — attempting delta reconstruction")
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT data FROM journal
                    WHERE event_type = 'STATE_UPDATE'
                    ORDER BY id ASC
                    """
                ).fetchall()

            if rows:
                valid_fields = set(RuntimeState.__dataclass_fields__)
                merged: dict = {}
                for r in rows:
                    try:
                        payload = json.loads(r["data"])
                        updates = payload.get("updates", {})
                        for key, value in updates.items():
                            if key in valid_fields:
                                merged[key] = value
                    except Exception:
                        continue

                state = RuntimeState(**{k: v for k, v in merged.items()})
                state.normalize()
                state = self._apply_day_rollover_if_needed(state, persist=True)
                if state.validate():
                    logger.info("Recovered state from %d delta journal entries", len(rows))
                    return state

            logger.warning("Recovery fallback: fresh state")
            return RuntimeState()

        except Exception as exc:
            logger.error("State recovery failed: %s", exc)
            return RuntimeState()

    def _apply_day_rollover_if_needed(self, state: RuntimeState, *, persist: bool) -> RuntimeState:
        today = _today_iso()

        if state.trade_date == today:
            return state

        if state.active_trade:
            logger.warning(
                "Trade date rollover detected with active_trade present | old=%s new=%s",
                state.trade_date,
                today,
            )
            state.trade_date = today
            state.last_updated = _now_iso()
            state.normalize()
            if persist:
                self._save_state(state)
            return state

        logger.info(
            "Applying automatic intraday rollover | old=%s new=%s",
            state.trade_date,
            today,
        )
        state.reset_intraday_fields()

        if persist:
            self._save_state(state)

        return state

    async def daily_reset(self) -> None:
        async with self._lock:
            try:
                await self._journal_event("DAILY_RESET", {"ts": _now_iso()})
                self._state.reset_intraday_fields()
                self._save_state(self._state)
                await self._refresh_redis_cache()
                logger.info("Daily reset complete date=%s", self._state.trade_date)
            except Exception as exc:
                logger.error("Daily reset failed: %s", exc, exc_info=True)

    async def get_journal(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT timestamp, event_type, data
                    FROM journal
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (max(_safe_int(limit, 100), 1),),
                ).fetchall()

            output: list[dict[str, Any]] = []
            for row in rows:
                try:
                    data = json.loads(row["data"])
                except Exception:
                    data = {"raw": row["data"]}

                output.append(
                    {
                        "timestamp": row["timestamp"],
                        "event_type": row["event_type"],
                        "data": data,
                    }
                )

            return output

        except Exception as exc:
            logger.error("Journal read failed: %s", exc)
            return []

    async def has_idempotency_key(self, key: str) -> bool:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM idempotency_keys WHERE key = ?",
                    (str(key),),
                ).fetchone()
            return row is not None
        except Exception as exc:
            logger.error("Idempotency read failed: %s", exc)
            return False

    async def add_idempotency_key(self, key: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO idempotency_keys (key) VALUES (?)",
                    (str(key),),
                )
        except Exception as exc:
            logger.error("Idempotency write failed: %s", exc)

    async def record_uncertain_order(self, record: dict[str, Any]) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO uncertain_orders (
                        order_id,
                        state,
                        filled_qty,
                        avg_price,
                        reason
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        _safe_str(record.get("order_id")),
                        _safe_str(record.get("state")),
                        _safe_int(record.get("filled_qty"), 0),
                        _safe_float(record.get("avg_price"), 0.0),
                        _safe_str(record.get("reason")),
                    ),
                )
        except Exception as exc:
            logger.error("Uncertain order write failed: %s", exc)

    def _safe_backup(self) -> None:
        try:
            if self._db_path.exists():
                self._backup_path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(str(self._db_path)) as src:
                    with sqlite3.connect(str(self._backup_path)) as dst:
                        src.backup(dst)
        except Exception as exc:
            logger.warning("State backup skipped: %s", exc)

    async def close(self) -> None:
        if not self._redis:
            return

        try:
            await self._redis.aclose()
        except Exception as exc:
            logger.warning("Redis close failed: %s", exc)
        finally:
            self._redis = None

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def equity(self) -> float:
        capital = float(getattr(settings, "capital", 0.0) or 0.0)
        return capital + self._state.realized_pnl + self._state.unrealized_pnl


_state_manager_instance: StateManager | None = None


def get_state_manager() -> StateManager:
    """Return the process-wide StateManager, creating it on first call."""
    global _state_manager_instance
    if _state_manager_instance is None:
        _state_manager_instance = StateManager()
    return _state_manager_instance


# Back-compat alias — existing code that imports `state_manager` directly still works.
# New code should prefer get_state_manager().
state_manager: StateManager = None  # type: ignore[assignment]


def _init_state_manager_alias() -> None:
    """Called once at startup to set the module-level alias after the DB is ready."""
    global state_manager
    state_manager = get_state_manager()


# Initialise eagerly so that `from ... import state_manager` keeps working.
state_manager = get_state_manager()
