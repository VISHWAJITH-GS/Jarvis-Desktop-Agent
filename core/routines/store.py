"""
core/routines/store.py — SQLite-backed persistence store for consolidated routines.

Supports schedules: once, daily, weekly, monthly, every N minutes, at startup, after idle.
Actions: speak, tool, task.
Tracks exact-time flags for quiet hours exemption.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from core.runtime_env import BASE_DIR

_DEFAULT_ROUTINES_DB = BASE_DIR / "memory" / "routines.db"


@dataclass
class RoutineRecord:
    id: str
    title: str
    schedule_type: str  # once, daily, weekly, monthly, every_n_minutes, at_startup, after_idle
    schedule_config: dict = field(default_factory=dict)
    action_type: str = "speak"  # speak, tool, task
    action_config: dict = field(default_factory=dict)
    is_exact_time: bool = False
    state: str = "active"  # active, paused, completed, cancelled
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "schedule_type": self.schedule_type,
            "schedule_config": self.schedule_config,
            "action_type": self.action_type,
            "action_config": self.action_config,
            "is_exact_time": self.is_exact_time,
            "state": self.state,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class RoutineStore:
    """Thread-safe SQLite store for routines and migrated reminders."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or _DEFAULT_ROUTINES_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS routines (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    schedule_type TEXT NOT NULL,
                    schedule_config TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    action_config TEXT NOT NULL,
                    is_exact_time INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'active',
                    next_run_at TEXT,
                    last_run_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_routines_state ON routines(state);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_routines_next_run ON routines(next_run_at);")

    def _row_to_record(self, row: sqlite3.Row) -> RoutineRecord:
        return RoutineRecord(
            id=row["id"],
            title=row["title"],
            schedule_type=row["schedule_type"],
            schedule_config=json.loads(row["schedule_config"]) if row["schedule_config"] else {},
            action_type=row["action_type"],
            action_config=json.loads(row["action_config"]) if row["action_config"] else {},
            is_exact_time=bool(row["is_exact_time"]),
            state=row["state"],
            next_run_at=row["next_run_at"],
            last_run_at=row["last_run_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def save_routine(self, r: RoutineRecord) -> None:
        """Create or update a routine record."""
        now_str = datetime.now().isoformat()
        r.updated_at = now_str
        with self._lock, self._connection() as conn:
            conn.execute("""
                INSERT INTO routines (
                    id, title, schedule_type, schedule_config, action_type,
                    action_config, is_exact_time, state, next_run_at, last_run_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    schedule_type=excluded.schedule_type,
                    schedule_config=excluded.schedule_config,
                    action_type=excluded.action_type,
                    action_config=excluded.action_config,
                    is_exact_time=excluded.is_exact_time,
                    state=excluded.state,
                    next_run_at=excluded.next_run_at,
                    last_run_at=excluded.last_run_at,
                    updated_at=excluded.updated_at;
            """, (
                r.id,
                r.title,
                r.schedule_type,
                json.dumps(r.schedule_config),
                r.action_type,
                json.dumps(r.action_config),
                1 if r.is_exact_time else 0,
                r.state,
                r.next_run_at,
                r.last_run_at,
                r.created_at,
                r.updated_at,
            ))

    def get_routine(self, routine_id: str) -> Optional[RoutineRecord]:
        """Fetch routine by ID."""
        with self._lock, self._connection() as conn:
            cur = conn.execute("SELECT * FROM routines WHERE id = ?", (routine_id,))
            row = cur.fetchone()
            return self._row_to_record(row) if row else None

    def list_routines(self, state: Optional[str] = None, limit: int = 100) -> list[RoutineRecord]:
        """List routines optionally filtered by state."""
        with self._lock, self._connection() as conn:
            if state:
                cur = conn.execute(
                    "SELECT * FROM routines WHERE state = ? ORDER BY next_run_at ASC LIMIT ?",
                    (state, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM routines ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                )
            return [self._row_to_record(r) for r in cur.fetchall()]

    def list_due_routines(self, before_iso: str) -> list[RoutineRecord]:
        """List active routines due to run on or before before_iso."""
        with self._lock, self._connection() as conn:
            cur = conn.execute(
                "SELECT * FROM routines WHERE state = 'active' AND next_run_at IS NOT NULL AND next_run_at <= ? ORDER BY next_run_at ASC",
                (before_iso,),
            )
            return [self._row_to_record(r) for r in cur.fetchall()]

    def update_run_status(self, routine_id: str, last_run_at: str, next_run_at: Optional[str], state: str = "active") -> bool:
        """Update last run timestamp and set the next scheduled run."""
        now_str = datetime.now().isoformat()
        with self._lock, self._connection() as conn:
            cur = conn.execute("""
                UPDATE routines SET
                    last_run_at = ?,
                    next_run_at = ?,
                    state = ?,
                    updated_at = ?
                WHERE id = ?;
            """, (last_run_at, next_run_at, state, now_str, routine_id))
            return cur.rowcount > 0

    def delete_routine(self, routine_id: str) -> bool:
        """Delete routine record."""
        with self._lock, self._connection() as conn:
            cur = conn.execute("DELETE FROM routines WHERE id = ?", (routine_id,))
            return cur.rowcount > 0
