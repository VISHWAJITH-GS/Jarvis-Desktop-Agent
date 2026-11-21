"""
core/tasks/store.py — SQLite-backed atomic store for durable background tasks.

Design reference: Mark LVII (CC BY-NC 4.0).
Provides crash-resilient persistence for task definitions, execution rounds,
conversation checkpoints, steering instructions, and outcome results.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from core.runtime_env import BASE_DIR

_DEFAULT_DB_PATH = BASE_DIR / "memory" / "tasks.db"


@dataclass
class TaskRecord:
    id: str
    title: str
    description: str
    state: str = "queued"  # queued, scheduled, running, waiting_quota, needs_confirmation, done, failed, cancelled
    checkpoint: dict = field(default_factory=dict)
    attempts: int = 0
    max_attempts: int = 3
    round_count: int = 0
    round_budget: int = 10
    time_budget_sec: float = 300.0
    elapsed_sec: float = 0.0
    result: Optional[str] = None
    error: Optional[str] = None
    allowlist: list[str] = field(default_factory=list)
    workspace_dir: str = ""
    steer_notes: list[str] = field(default_factory=list)
    pending_confirmation: Optional[dict] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "state": self.state,
            "checkpoint": self.checkpoint,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "round_count": self.round_count,
            "round_budget": self.round_budget,
            "time_budget_sec": self.time_budget_sec,
            "elapsed_sec": self.elapsed_sec,
            "result": self.result,
            "error": self.error,
            "allowlist": self.allowlist,
            "workspace_dir": self.workspace_dir,
            "steer_notes": self.steer_notes,
            "pending_confirmation": self.pending_confirmation,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TaskStore:
    """Thread-safe, atomic SQLite store for background autonomy tasks."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or _DEFAULT_DB_PATH
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
                CREATE TABLE IF NOT EXISTS background_tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    state TEXT NOT NULL,
                    checkpoint TEXT,
                    attempts INTEGER DEFAULT 0,
                    max_attempts INTEGER DEFAULT 3,
                    round_count INTEGER DEFAULT 0,
                    round_budget INTEGER DEFAULT 10,
                    time_budget_sec REAL DEFAULT 300.0,
                    elapsed_sec REAL DEFAULT 0.0,
                    result TEXT,
                    error TEXT,
                    allowlist TEXT,
                    workspace_dir TEXT,
                    steer_notes TEXT,
                    pending_confirmation TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state ON background_tasks(state);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_updated ON background_tasks(updated_at);")

    def _row_to_record(self, row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            state=row["state"],
            checkpoint=json.loads(row["checkpoint"]) if row["checkpoint"] else {},
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            round_count=row["round_count"],
            round_budget=row["round_budget"],
            time_budget_sec=row["time_budget_sec"],
            elapsed_sec=row["elapsed_sec"],
            result=row["result"],
            error=row["error"],
            allowlist=json.loads(row["allowlist"]) if row["allowlist"] else [],
            workspace_dir=row["workspace_dir"] or "",
            steer_notes=json.loads(row["steer_notes"]) if row["steer_notes"] else [],
            pending_confirmation=json.loads(row["pending_confirmation"]) if row["pending_confirmation"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def save_task(self, task: TaskRecord) -> None:
        """Create or update a task record."""
        now_str = datetime.now().isoformat()
        task.updated_at = now_str
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO background_tasks (
                    id, title, description, state, checkpoint, attempts, max_attempts,
                    round_count, round_budget, time_budget_sec, elapsed_sec, result,
                    error, allowlist, workspace_dir, steer_notes, pending_confirmation,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    description=excluded.description,
                    state=excluded.state,
                    checkpoint=excluded.checkpoint,
                    attempts=excluded.attempts,
                    max_attempts=excluded.max_attempts,
                    round_count=excluded.round_count,
                    round_budget=excluded.round_budget,
                    time_budget_sec=excluded.time_budget_sec,
                    elapsed_sec=excluded.elapsed_sec,
                    result=excluded.result,
                    error=excluded.error,
                    allowlist=excluded.allowlist,
                    workspace_dir=excluded.workspace_dir,
                    steer_notes=excluded.steer_notes,
                    pending_confirmation=excluded.pending_confirmation,
                    updated_at=excluded.updated_at;
            """,
                (
                    task.id,
                    task.title,
                    task.description,
                    task.state,
                    json.dumps(task.checkpoint),
                    task.attempts,
                    task.max_attempts,
                    task.round_count,
                    task.round_budget,
                    task.time_budget_sec,
                    task.elapsed_sec,
                    task.result,
                    task.error,
                    json.dumps(task.allowlist),
                    task.workspace_dir,
                    json.dumps(task.steer_notes),
                    json.dumps(task.pending_confirmation) if task.pending_confirmation else None,
                    task.created_at,
                    task.updated_at,
                ),
            )

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        """Fetch a single task record by ID."""
        with self._lock, self._connection() as conn:
            cur = conn.execute("SELECT * FROM background_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            return self._row_to_record(row) if row else None

    def list_tasks(self, state: Optional[str] = None, limit: int = 50) -> list[TaskRecord]:
        """List tasks, optionally filtered by state."""
        with self._lock, self._connection() as conn:
            if state:
                cur = conn.execute(
                    "SELECT * FROM background_tasks WHERE state = ? ORDER BY updated_at DESC LIMIT ?",
                    (state, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM background_tasks ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                )
            return [self._row_to_record(r) for r in cur.fetchall()]

    def atomic_checkpoint(
        self,
        task_id: str,
        state: str,
        round_count: int,
        elapsed_sec: float,
        checkpoint: dict,
        result: Optional[str] = None,
        error: Optional[str] = None,
        steer_notes: Optional[list[str]] = None,
        pending_confirmation: Optional[dict] = None,
    ) -> bool:
        """Atomically persist round results, state, and checkpoint."""
        now_str = datetime.now().isoformat()
        with self._lock, self._connection() as conn:
            cur = conn.execute(
                """
                UPDATE background_tasks SET
                    state = ?,
                    round_count = ?,
                    elapsed_sec = ?,
                    checkpoint = ?,
                    result = COALESCE(?, result),
                    error = ?,
                    steer_notes = COALESCE(?, steer_notes),
                    pending_confirmation = ?,
                    updated_at = ?
                WHERE id = ?;
            """,
                (
                    state,
                    round_count,
                    elapsed_sec,
                    json.dumps(checkpoint),
                    result,
                    error,
                    json.dumps(steer_notes) if steer_notes is not None else None,
                    json.dumps(pending_confirmation) if pending_confirmation is not None else None,
                    now_str,
                    task_id,
                ),
            )
            return cur.rowcount > 0

    def update_state(self, task_id: str, state: str, error: Optional[str] = None, result: Optional[str] = None) -> bool:
        """Update task status with optional error or result string."""
        now_str = datetime.now().isoformat()
        with self._lock, self._connection() as conn:
            cur = conn.execute(
                """
                UPDATE background_tasks SET
                    state = ?,
                    error = COALESCE(?, error),
                    result = COALESCE(?, result),
                    updated_at = ?
                WHERE id = ?;
            """,
                (state, error, result, now_str, task_id),
            )
            return cur.rowcount > 0

    def add_steer_note(self, task_id: str, note: str) -> bool:
        """Append an steering instruction to be delivered next round."""
        with self._lock, self._connection() as conn:
            cur = conn.execute("SELECT steer_notes FROM background_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                return False
            notes = json.loads(row["steer_notes"]) if row["steer_notes"] else []
            notes.append(note)
            now_str = datetime.now().isoformat()
            conn.execute(
                "UPDATE background_tasks SET steer_notes = ?, updated_at = ? WHERE id = ?",
                (json.dumps(notes), now_str, task_id),
            )
            return True

    def pop_steer_notes(self, task_id: str) -> list[str]:
        """Retrieve and clear queued steering notes atomically."""
        with self._lock, self._connection() as conn:
            cur = conn.execute("SELECT steer_notes FROM background_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row or not row["steer_notes"]:
                return []
            notes = json.loads(row["steer_notes"])
            now_str = datetime.now().isoformat()
            conn.execute(
                "UPDATE background_tasks SET steer_notes = '[]', updated_at = ? WHERE id = ?",
                (now_str, task_id),
            )
            return notes

    def record_attempt(self, task_id: str) -> int:
        """Increment attempt count upon worker crash/resume."""
        now_str = datetime.now().isoformat()
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE background_tasks SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (now_str, task_id),
            )
            cur = conn.execute("SELECT attempts FROM background_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            return int(row["attempts"]) if row else 1

    def delete_task(self, task_id: str) -> bool:
        """Remove a task record entirely."""
        with self._lock, self._connection() as conn:
            cur = conn.execute("DELETE FROM background_tasks WHERE id = ?", (task_id,))
            return cur.rowcount > 0
