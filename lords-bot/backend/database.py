from __future__ import annotations

import sqlite3
from pathlib import Path
from contextlib import contextmanager

from config import settings
from utils import ensure_parent


class Database:
    def __init__(self) -> None:
        ensure_parent(Path(settings.db_file))
        self._init_schema()

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(settings.db_file)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connection() as conn:
            conn.execute(
                '''CREATE TABLE IF NOT EXISTS paper_trades (
                    trade_id INTEGER PRIMARY KEY,
                    position_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strike REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT,
                    pnl REAL NOT NULL,
                    target_price REAL NOT NULL,
                    stop_loss_price REAL NOT NULL
                )'''
            )
            conn.execute(
                'CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, signal TEXT, confidence REAL, reason TEXT, generated_at TEXT)'
            )
            conn.execute(
                'CREATE TABLE IF NOT EXISTS analysis_history (id INTEGER PRIMARY KEY AUTOINCREMENT, pcr REAL, trend TEXT, support REAL, resistance REAL, atm_strike REAL, generated_at TEXT)'
            )


database = Database()
