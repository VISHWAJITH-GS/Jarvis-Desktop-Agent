"""
core/routines/migration.py — Seamless migration of legacy reminders into routines.

Scans legacy reminders.db and reminders_db.json across config and fixtures,
converting each reminder into an exact-time RoutineRecord without loss.
"""
from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
import sqlite3
from typing import Optional

from core.runtime_env import BASE_DIR
from core.routines.store import RoutineRecord, RoutineStore

logger = logging.getLogger(__name__)


def migrate_legacy_reminders(store: RoutineStore, custom_json_path: Optional[Path] = None, custom_sqlite_path: Optional[Path] = None) -> int:
    """
    Migrate existing reminders into the unified RoutineStore.
    Idempotent: skips reminders already present in the store.
    """
    migrated_count = 0

    if custom_json_path or custom_sqlite_path:
        candidate_json_paths = [custom_json_path] if custom_json_path else []
        candidate_sqlite_paths = [custom_sqlite_path] if custom_sqlite_path else []
    else:
        candidate_json_paths = [
            BASE_DIR / "memory" / "reminders" / "reminders_db.json",
            BASE_DIR / "tests" / "fixtures" / "reminders_db.json",
        ]
        candidate_sqlite_paths = [
            BASE_DIR / "memory" / "reminders" / "reminders.db",
        ]
        try:
            from memory.config_manager import get_user_data_dir
            candidate_json_paths.append(get_user_data_dir() / "reminders" / "reminders_db.json")
            candidate_sqlite_paths.append(get_user_data_dir() / "reminders" / "reminders.db")
        except Exception:
            pass

    # 1. Migrate JSON sources
    for jp in candidate_json_paths:
        if not jp or not jp.exists():
            continue
        try:
            content = jp.read_text(encoding="utf-8").strip()
            if not content:
                continue
            data = json.loads(content)
            items = data if isinstance(data, list) else [data]
            for item in items:
                rid = item.get("id")
                if not rid or store.get_routine(rid):
                    continue

                msg = item.get("message") or item.get("text") or "Reminder"
                due = item.get("due_time") or item.get("target_time") or ""
                rec = item.get("recurrence", "none").lower()
                status = item.get("status", "active")
                sched_type = "once" if rec in ("none", "", "no", "null") else rec

                routine = RoutineRecord(
                    id=str(rid),
                    title=item.get("task_name") or f"Reminder: {msg[:30]}",
                    schedule_type=sched_type,
                    schedule_config={"due_time": due, "recurrence": rec},
                    action_type="speak",
                    action_config={"message": msg},
                    is_exact_time=True,  # Exact-time reminders exempt from quiet hours
                    state=status if status in ("active", "completed", "cancelled") else "active",
                    next_run_at=due if status != "completed" else None,
                    created_at=item.get("created_at") or datetime.now().isoformat(),
                    completed_at=item.get("completed_at"),
                )
                store.save_routine(routine)
                migrated_count += 1
        except Exception as e:
            logger.warning("[Routines] Could not migrate reminders from %s: %s", jp, e)

    # 2. Migrate legacy SQLite sources
    for sp in candidate_sqlite_paths:
        if not sp or not sp.exists():
            continue
        try:
            conn = sqlite3.connect(str(sp), timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                cur = conn.execute("SELECT * FROM reminders")
                rows = cur.fetchall()
                for row in rows:
                    rid = row["id"]
                    if not rid or store.get_routine(rid):
                        continue

                    msg = row["message"]
                    due = row["due_time"]
                    rec = (row["recurrence"] or "none").lower()
                    status = row["status"] or "active"
                    sched_type = "once" if rec in ("none", "", "no") else rec

                    routine = RoutineRecord(
                        id=str(rid),
                        title=row["task_name"] or f"Reminder: {msg[:30]}",
                        schedule_type=sched_type,
                        schedule_config={"due_time": due, "recurrence": rec},
                        action_type="speak",
                        action_config={"message": msg},
                        is_exact_time=True,
                        state=status if status in ("active", "completed", "cancelled") else "active",
                        next_run_at=due if status != "completed" else None,
                        created_at=row["created_at"] or datetime.now().isoformat(),
                        completed_at=row["completed_at"],
                    )
                    store.save_routine(routine)
                    migrated_count += 1
            finally:
                conn.close()
        except Exception as e:
            logger.warning("[Routines] Could not migrate reminders from SQLite %s: %s", sp, e)

    if migrated_count > 0:
        logger.info("[Routines] Successfully migrated %d legacy reminders to RoutineStore.", migrated_count)
    return migrated_count
