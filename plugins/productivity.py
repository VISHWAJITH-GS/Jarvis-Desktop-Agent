"""
plugins/productivity.py — Authoritative Task & Habit Management System for JARVIS.

Capabilities:
- Task storage in memory/productivity.json with atomic writes and lock
- Natural language date & time parsing with recurrence support
- Fuzzy task title matching with ambiguity detection (status="ask")
- Scope-aware task management (daily, weekly, monthly, yearly, once)
- Recurring task rollover on completion
- Full undo integration via core/undo.py
- Scheduled desktop notifications & missed reminder recovery
- Habit tracking with streak calculations and coaching nudges
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
import difflib
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from core import outcome
from core.undo import push_undo

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Metadata for Auto-Discovery (PLUGIN dict)
# ─────────────────────────────────────────────────────────────────────────────

PLUGIN = {
    "name": "productivity",
    "description": (
        "Manage tasks, to-dos, habits, and daily/weekly reviews. "
        "Actions: add_task, list_tasks, complete_task, update_task, delete_task, "
        "daily_summary, snooze, add_habit, log_habit, list_habits, habit_stats, weekly_review. "
        "Supports natural dates ('tomorrow 6pm', 'every Monday', 'end of month'), recurrence, "
        "fuzzy matching, and undo."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "The productivity action to perform. One of: "
                    "'add_task', 'list_tasks', 'complete_task', 'update_task', 'delete_task', "
                    "'daily_summary', 'snooze', 'add_habit', 'log_habit', 'list_habits', "
                    "'habit_stats', 'weekly_review'."
                ),
                "enum": [
                    "add_task",
                    "list_tasks",
                    "complete_task",
                    "update_task",
                    "delete_task",
                    "daily_summary",
                    "snooze",
                    "add_habit",
                    "log_habit",
                    "list_habits",
                    "habit_stats",
                    "weekly_review",
                ],
            },
            "title": {
                "type": "STRING",
                "description": "Title of the task or search query for fuzzy matching.",
            },
            "notes": {
                "type": "STRING",
                "description": "Additional notes, details or description for the task.",
            },
            "scope": {
                "type": "STRING",
                "description": "Scope of the task: 'daily', 'weekly', 'monthly', 'yearly', or 'once'.",
                "enum": ["daily", "weekly", "monthly", "yearly", "once"],
            },
            "due": {
                "type": "STRING",
                "description": "Due date/time in natural language ('tomorrow 6pm', 'end of month') or ISO format.",
            },
            "priority": {
                "type": "STRING",
                "description": "Task priority: 'low', 'normal', or 'high'. Defaults to 'normal'.",
                "enum": ["low", "normal", "high"],
            },
            "recurrence": {
                "type": "STRING",
                "description": "Recurrence rule: 'daily', 'weekly', 'weekdays', 'monthly', 'yearly', or null.",
            },
            "status": {
                "type": "STRING",
                "description": "Filter or target status: 'pending', 'done', 'skipped', or 'all'.",
                "enum": ["pending", "done", "skipped", "all"],
            },
            "task_id": {
                "type": "STRING",
                "description": "Unique ID of the task when known.",
            },
            "date": {
                "type": "STRING",
                "description": "Target date for filtering (YYYY-MM-DD or 'today').",
            },
            "name": {
                "type": "STRING",
                "description": "Habit name for habit operations.",
            },
            "target": {
                "type": "STRING",
                "description": "Habit target frequency (e.g. 'daily', '3x/week', '5x/week').",
            },
            "reminder_time": {
                "type": "STRING",
                "description": "Preferred reminder time for habit (e.g. '08:00', '20:30').",
            },
            "habit_id": {
                "type": "STRING",
                "description": "Unique ID of the habit.",
            },
            "duration_minutes": {
                "type": "INTEGER",
                "description": "Duration in minutes to snooze the last reminder.",
            },
            "brief": {
                "type": "BOOLEAN",
                "description": "Whether to return a very brief/compact response for smaller local models.",
            },
        },
        "required": ["action"],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
#  Storage & Paths
# ─────────────────────────────────────────────────────────────────────────────

def _default_store_path() -> Path:
    """Return default path to memory/productivity.json."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
    mem_dir = base / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    return mem_dir / "productivity.json"


class ProductivityStore:
    """
    Thread-safe, crash-isolated JSON store for tasks, habits, and scheduler state.
    Uses atomic temp-file write + rename and verifies persistence.
    """

    def __init__(self, file_path: Optional[Path | str] = None):
        self.path = Path(file_path) if file_path else _default_store_path()
        self._lock = threading.RLock()
        self._ensure_init()

    def _default_data(self) -> dict:
        return {
            "tasks": {},
            "habits": {},
            "notified_reminder_ids": [],
            "last_missed_check": None,
            "last_reminded_task_id": None,
            "config": {
                "rollover_missed_tasks": True,
                "max_proactive_nudges_per_day": 2,
                "quiet_hours_start": "22:00",
                "quiet_hours_end": "08:00",
            },
        }

    def _ensure_init(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._save_raw(self._default_data())

    def load(self) -> dict:
        """Load data from disk. If corrupt, back up and return fresh clean state."""
        with self._lock:
            if not self.path.exists():
                return self._default_data()
            try:
                content = self.path.read_text(encoding="utf-8")
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise ValueError("Root JSON is not an object")
                # Ensure essential top-level keys
                defaults = self._default_data()
                for k, v in defaults.items():
                    if k not in data:
                        data[k] = v
                return data
            except Exception as e:
                logger.error(f"[Productivity] Corrupt JSON store at {self.path}: {e}")
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = self.path.with_name(f"{self.path.stem}.corrupt.{ts}{self.path.suffix}")
                try:
                    if self.path.exists():
                        self.path.rename(backup_path)
                        logger.warning(f"[Productivity] Backed up corrupt store to {backup_path}")
                except Exception as be:
                    logger.error(f"[Productivity] Failed to backup corrupt store: be")
                clean = self._default_data()
                self._save_raw(clean)
                return clean

    def _save_raw(self, data: dict) -> None:
        """Write to temp file and atomically rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".tmp")
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, self.path)

    def save_and_verify(self, data: dict) -> bool:
        """Atomically save data and read it back to confirm persistence."""
        with self._lock:
            try:
                self._save_raw(data)
                # Read back verification
                readback = json.loads(self.path.read_text(encoding="utf-8"))
                # Verify key task count or structure
                if len(readback.get("tasks", {})) == len(data.get("tasks", {})):
                    return True
                return False
            except Exception as e:
                logger.error(f"[Productivity] Failed save/verify: {e}")
                return False


_global_store: Optional[ProductivityStore] = None


def get_store(custom_path: Optional[Path | str] = None) -> ProductivityStore:
    global _global_store
    if custom_path:
        return ProductivityStore(custom_path)
    if _global_store is None:
        _global_store = ProductivityStore()
    return _global_store


# ─────────────────────────────────────────────────────────────────────────────
#  Natural Date & Time Parser
# ─────────────────────────────────────────────────────────────────────────────

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def parse_natural_date(
    text: Optional[str],
    now: Optional[datetime] = None,
) -> Tuple[Optional[datetime], Optional[str], Optional[str]]:
    """
    Parse a natural date/time string.
    Returns: (parsed_datetime, recurrence_rule, ask_question)

    If clear, returns (dt, recurrence_or_none, None).
    If ambiguous or unrecognizable, returns (None, None, clarification_question).
    If text is empty/None, returns (None, None, None).
    """
    if not text or not text.strip():
        return None, None, None

    curr = now or datetime.now()
    raw = text.strip().lower()

    # Check recurring expressions first
    m_recur = re.search(r"^(?:every|each)\s+(day|daily|week|weekly|month|monthly|year|yearly|weekday|weekdays|monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri|saturday|sat|sunday|sun)\b", raw)
    if m_recur:
        target = m_recur.group(1)
        if target in ("day", "daily"):
            rule = "daily"
            dt = curr.replace(second=0, microsecond=0)
            return dt, rule, None
        elif target in ("weekday", "weekdays"):
            rule = "weekdays"
            dt = curr.replace(second=0, microsecond=0)
            return dt, rule, None
        elif target in ("week", "weekly"):
            rule = "weekly"
            dt = curr.replace(second=0, microsecond=0)
            return dt, rule, None
        elif target in ("month", "monthly"):
            rule = "monthly"
            dt = curr.replace(second=0, microsecond=0)
            return dt, rule, None
        elif target in ("year", "yearly"):
            rule = "yearly"
            dt = curr.replace(second=0, microsecond=0)
            return dt, rule, None
        elif target in _WEEKDAYS:
            rule = "weekly"
            day_idx = _WEEKDAYS[target]
            days_ahead = (day_idx - curr.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target_date = (curr + timedelta(days=days_ahead)).date()
            dt = datetime.combine(target_date, dt_time(9, 0))
            return dt, rule, None

    # Recurrence keywords alone
    if raw in ("daily", "everyday"):
        return curr.replace(second=0, microsecond=0), "daily", None
    if raw in ("weekly",):
        return curr.replace(second=0, microsecond=0), "weekly", None
    if raw in ("weekdays",):
        return curr.replace(second=0, microsecond=0), "weekdays", None
    if raw in ("monthly",):
        return curr.replace(second=0, microsecond=0), "monthly", None
    if raw in ("yearly",):
        return curr.replace(second=0, microsecond=0), "yearly", None

    # 1. ISO format parsing
    for iso_fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d-%m-%Y %H:%M",
        "%d-%m-%Y",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
    ):
        try:
            dt = datetime.strptime(raw, iso_fmt)
            if iso_fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
                dt = datetime.combine(dt.date(), dt_time(18, 0))
            return dt, None, None
        except ValueError:
            pass

    # 2. Relative offsets: "in X minutes/hours/days/weeks"
    m_rel = re.search(r"\bin\s+(\d+)\s*(min|minute|minutes|hour|hours|hr|hrs|day|days|week|weeks)\b", raw)
    if m_rel:
        qty = int(m_rel.group(1))
        unit = m_rel.group(2)
        if "min" in unit:
            return curr + timedelta(minutes=qty), None, None
        elif "hour" in unit or "hr" in unit:
            return curr + timedelta(hours=qty), None, None
        elif "day" in unit:
            return curr + timedelta(days=qty), None, None
        elif "week" in unit:
            return curr + timedelta(weeks=qty), None, None

    # 3. End of month / End of week
    if "end of month" in raw:
        last_day = calendar.monthrange(curr.year, curr.month)[1]
        dt = datetime(curr.year, curr.month, last_day, 18, 0)
        return dt, None, None

    if "end of week" in raw:
        days_to_sun = (6 - curr.weekday()) % 7
        if days_to_sun == 0:
            days_to_sun = 7
        target_d = (curr + timedelta(days=days_to_sun)).date()
        dt = datetime.combine(target_d, dt_time(18, 0))
        return dt, None, None

    # Extract optional time component from string
    parsed_time: Optional[dt_time] = None
    m_time = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", raw)
    if m_time:
        hr = int(m_time.group(1))
        mn = int(m_time.group(2) or 0)
        ampm = m_time.group(3)
        if ampm == "pm" and hr < 12:
            hr += 12
        elif ampm == "am" and hr == 12:
            hr = 0
        parsed_time = dt_time(hr, mn)
    else:
        m_24h = re.search(r"\b(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)\b", raw)
        if m_24h:
            parsed_time = dt_time(int(m_24h.group(1)), int(m_24h.group(2)))
        elif "morning" in raw:
            parsed_time = dt_time(9, 0)
        elif "afternoon" in raw:
            parsed_time = dt_time(14, 0)
        elif "evening" in raw:
            parsed_time = dt_time(18, 0)
        elif "night" in raw:
            parsed_time = dt_time(20, 0)

    # 4. Today / Tomorrow
    if "today" in raw:
        t = parsed_time or dt_time(18, 0)
        dt = datetime.combine(curr.date(), t)
        if parsed_time and dt <= curr:
            # Time has passed today
            return dt + timedelta(days=1), None, None
        return dt, None, None

    if "tomorrow" in raw:
        t = parsed_time or dt_time(9, 0)
        dt = datetime.combine((curr + timedelta(days=1)).date(), t)
        return dt, None, None

    # 5. Day of week (e.g. "monday", "next friday", "this sunday")
    for name, day_num in _WEEKDAYS.items():
        if re.search(rf"\b(next\s+|this\s+)?{name}\b", raw):
            is_next = "next" in raw
            days_ahead = (day_num - curr.weekday()) % 7
            if days_ahead == 0 or is_next:
                days_ahead += 7
            target_date = (curr + timedelta(days=days_ahead)).date()
            t = parsed_time or dt_time(9, 0)
            return datetime.combine(target_date, t), None, None

    # 6. Standalone time today/tomorrow
    if parsed_time is not None:
        dt = datetime.combine(curr.date(), parsed_time)
        if dt <= curr:
            dt += timedelta(days=1)
        return dt, None, None

    # If we couldn't parse it with confidence, ask for clarification
    return (
        None,
        None,
        f"I could not clearly understand the date '{text}'. Could you clarify (e.g. 'tomorrow 6pm', 'in 2 hours', or 'YYYY-MM-DD')?",
    )


def compute_next_recurrence(current_due: Optional[datetime], rule: str, now: Optional[datetime] = None) -> datetime:
    """Compute the next due datetime given a recurrence rule."""
    curr = now or datetime.now()
    base = current_due if (current_due and current_due > curr) else curr
    r = rule.lower().strip()

    if r == "daily":
        return base + timedelta(days=1)
    elif r == "weekdays":
        # Next Mon-Fri
        next_dt = base + timedelta(days=1)
        while next_dt.weekday() >= 5:  # Saturday or Sunday
            next_dt += timedelta(days=1)
        return next_dt
    elif r == "weekly":
        return base + timedelta(days=7)
    elif r == "monthly":
        year = base.year
        month = base.month + 1
        if month > 12:
            month = 1
            year += 1
        max_days = calendar.monthrange(year, month)[1]
        day = min(base.day, max_days)
        return datetime(year, month, day, base.hour, base.minute, base.second)
    elif r == "yearly":
        year = base.year + 1
        max_days = calendar.monthrange(year, base.month)[1]
        day = min(base.day, max_days)
        return datetime(year, base.month, day, base.hour, base.minute, base.second)
    else:
        return base + timedelta(days=1)


# ─────────────────────────────────────────────────────────────────────────────
#  Fuzzy Task Matching (difflib)
# ─────────────────────────────────────────────────────────────────────────────

def resolve_task_by_title_or_id(
    query: str,
    tasks: Dict[str, dict],
) -> Tuple[Optional[dict], List[dict], Optional[str]]:
    """
    Resolve task deterministically or return ambiguous candidates.
    Returns: (matched_task, candidates_list, error_or_ask_message)
    """
    q = (query or "").strip()
    if not q:
        return None, [], "Please specify the task title or ID."

    # 1. Exact ID match
    if q in tasks:
        return tasks[q], [], None

    # 2. Exact Title match (case-insensitive)
    exact_matches = [t for t in tasks.values() if t.get("title", "").strip().lower() == q.lower()]
    if len(exact_matches) == 1:
        return exact_matches[0], [], None
    elif len(exact_matches) > 1:
        candidates = exact_matches
        titles_str = ", ".join(f"'{c.get('title')}' (id: {c.get('id')})" for c in candidates)
        return None, candidates, f"Multiple tasks match '{q}': {titles_str}. Which one did you mean?"

    # 3. Substring & Fuzzy Match
    scored_candidates: List[Tuple[float, dict]] = []
    q_lower = q.lower()
    for t in tasks.values():
        title = t.get("title", "")
        t_lower = title.strip().lower()
        # Direct substring score
        if q_lower in t_lower:
            ratio = 0.85 + (len(q_lower) / max(len(t_lower), 1)) * 0.14
        else:
            ratio = difflib.SequenceMatcher(None, q_lower, t_lower).ratio()

        if ratio >= 0.55:
            scored_candidates.append((ratio, t))

    scored_candidates.sort(key=lambda x: x[0], reverse=True)

    if not scored_candidates:
        return None, [], f"No task found matching '{q}'."

    top_score, top_task = scored_candidates[0]

    # Check for ambiguity: multiple candidates with close scores
    close_candidates = [t for score, t in scored_candidates if score >= 0.55 and (top_score - score) <= 0.15]
    if len(close_candidates) > 1:
        c_str = ", ".join(f"'{c.get('title')}' (id: {c.get('id')})" for c in close_candidates)
        return None, close_candidates, f"Multiple tasks might match '{q}': {c_str}. Which one did you mean?"

    if top_score >= 0.6:
        return top_task, [], None

    return None, [], f"No task clearly matched '{q}'. Closest was '{top_task.get('title')}'."


# ─────────────────────────────────────────────────────────────────────────────
#  Task Actions
# ─────────────────────────────────────────────────────────────────────────────

def add_task(store: ProductivityStore, params: dict) -> str:
    """Add a new task."""
    title = (params.get("title") or "").strip()
    if not title:
        return outcome.failed("productivity", "Task title cannot be empty.")

    notes = params.get("notes", "").strip()
    scope = params.get("scope", "once").lower()
    if scope not in ("daily", "weekly", "monthly", "yearly", "once"):
        scope = "once"

    due_raw = params.get("due")
    parsed_due, auto_recurrence, ask_q = parse_natural_date(due_raw)
    if ask_q:
        return outcome.ask("productivity", ask_q)

    recurrence = params.get("recurrence") or auto_recurrence
    priority = (params.get("priority") or "normal").lower()
    if priority not in ("low", "normal", "high"):
        priority = "normal"

    task_id = str(uuid.uuid4())[:8]
    now_iso = datetime.now().isoformat()
    due_iso = parsed_due.isoformat() if parsed_due else None

    # Build reminder times
    reminder_times = [due_iso] if due_iso else []

    new_task = {
        "id": task_id,
        "title": title,
        "notes": notes,
        "scope": scope,
        "due": due_iso,
        "priority": priority,
        "status": "pending",
        "recurrence": recurrence,
        "created_at": now_iso,
        "completed_at": None,
        "reminder_times": reminder_times,
    }

    data = store.load()
    data["tasks"][task_id] = new_task
    verified = store.save_and_verify(data)

    if not verified:
        return outcome.unverified(
            "productivity",
            "Could not verify task persistence to disk.",
            id=task_id,
            title=title,
        )

    # Register Undo
    def _undo_add():
        d = store.load()
        if task_id in d.get("tasks", {}):
            del d["tasks"][task_id]
            store.save_and_verify(d)
        return f"Task '{title}' removed."

    push_undo(f"add task '{title}'", _undo_add)

    brief = params.get("brief", False)
    if brief:
        return outcome.done("productivity", id=task_id, title=title, status="pending")

    return outcome.done(
        "productivity",
        id=task_id,
        title=title,
        due=due_iso or "none",
        priority=priority,
        recurrence=recurrence or "none",
        scope=scope,
    )


def list_tasks(store: ProductivityStore, params: dict) -> str:
    """List tasks filtered by scope, date, or status."""
    data = store.load()
    tasks = list(data.get("tasks", {}).values())

    filter_status = params.get("status", "pending").lower()
    filter_scope = params.get("scope")
    filter_date = params.get("date")

    # Apply status filter
    if filter_status and filter_status != "all":
        tasks = [t for t in tasks if t.get("status") == filter_status]

    # Apply scope filter
    if filter_scope and filter_scope != "all":
        tasks = [t for t in tasks if t.get("scope") == filter_scope.lower()]

    # Apply date filter
    if filter_date:
        parsed_d, _, _ = parse_natural_date(filter_date)
        target_d = (parsed_d.date().isoformat() if parsed_d else filter_date)
        tasks = [t for t in tasks if t.get("due") and t["due"].startswith(target_d)]

    # Sort tasks by due date (None at the end), then priority
    prio_order = {"high": 0, "normal": 1, "low": 2}
    tasks.sort(
        key=lambda x: (
            x.get("due") or "9999-99-99",
            prio_order.get(x.get("priority", "normal"), 1),
            x.get("title", ""),
        )
    )

    if not tasks:
        return outcome.done(
            "productivity",
            count=0,
            summary="No tasks found matching criteria.",
            status=filter_status,
        )

    brief = params.get("brief", False)
    items = []
    for t in tasks:
        due_str = t.get("due") or "no due date"
        prio = f" [{t.get('priority')}]" if t.get("priority") != "normal" else ""
        rec = f" (recurs: {t.get('recurrence')})" if t.get("recurrence") else ""
        items.append(f"• {t.get('title')} (id: {t.get('id')}, due: {due_str}{prio}{rec})")

    summary_text = f"{len(tasks)} tasks: " + "; ".join(items)
    return outcome.done(
        "productivity",
        count=len(tasks),
        tasks=summary_text if not brief else f"{len(tasks)} tasks found",
        status=filter_status,
    )


def complete_task(store: ProductivityStore, params: dict) -> str:
    """Mark a task completed. If recurring, generate next recurrence instance."""
    data = store.load()
    tasks = data.get("tasks", {})

    target_query = params.get("task_id") or params.get("title") or ""
    task, candidates, err = resolve_task_by_title_or_id(target_query, tasks)
    if err and not task:
        if candidates:
            return outcome.ask("productivity", err)
        return outcome.failed("productivity", err)

    task_id = task["id"]
    old_task_snapshot = dict(task)

    if task.get("status") == "done":
        return outcome.done("productivity", id=task_id, title=task["title"], note="Already completed.")

    now = datetime.now()
    now_iso = now.isoformat()
    task["status"] = "done"
    task["completed_at"] = now_iso

    recurrence = task.get("recurrence")
    next_task_id = None
    next_due_iso = None

    if recurrence:
        current_due = datetime.fromisoformat(task["due"]) if task.get("due") else now
        next_due = compute_next_recurrence(current_due, recurrence, now)
        next_due_iso = next_due.isoformat()
        next_task_id = str(uuid.uuid4())[:8]

        new_instance = {
            "id": next_task_id,
            "title": task["title"],
            "notes": task.get("notes", ""),
            "scope": task.get("scope", "once"),
            "due": next_due_iso,
            "priority": task.get("priority", "normal"),
            "status": "pending",
            "recurrence": recurrence,
            "created_at": now_iso,
            "completed_at": None,
            "reminder_times": [next_due_iso],
        }
        data["tasks"][next_task_id] = new_instance

    data["tasks"][task_id] = task
    verified = store.save_and_verify(data)

    if not verified:
        return outcome.unverified("productivity", "Failed to verify completion save.", id=task_id)

    # Undo handler
    def _undo_complete():
        d = store.load()
        if task_id in d.get("tasks", {}):
            d["tasks"][task_id] = old_task_snapshot
        if next_task_id and next_task_id in d.get("tasks", {}):
            del d["tasks"][next_task_id]
        store.save_and_verify(d)
        return f"Task '{old_task_snapshot.get('title')}' unmarked from complete."

    push_undo(f"complete task '{task['title']}'", _undo_complete)

    res_facts = {
        "id": task_id,
        "title": task["title"],
        "status": "done",
    }
    if next_task_id:
        res_facts["recurring_next_id"] = next_task_id
        res_facts["next_due"] = next_due_iso

    return outcome.done("productivity", **res_facts)


def update_task(store: ProductivityStore, params: dict) -> str:
    """Update an existing task's fields."""
    data = store.load()
    tasks = data.get("tasks", {})

    target_query = params.get("task_id") or params.get("title") or ""
    task, candidates, err = resolve_task_by_title_or_id(target_query, tasks)
    if err and not task:
        if candidates:
            return outcome.ask("productivity", err)
        return outcome.failed("productivity", err)

    task_id = task["id"]
    old_task_snapshot = dict(task)

    # Update allowed fields
    if "notes" in params:
        task["notes"] = params["notes"]
    if "priority" in params and params["priority"]:
        p = params["priority"].lower()
        if p in ("low", "normal", "high"):
            task["priority"] = p
    if "scope" in params and params["scope"]:
        s = params["scope"].lower()
        if s in ("daily", "weekly", "monthly", "yearly", "once"):
            task["scope"] = s
    if "recurrence" in params:
        task["recurrence"] = params["recurrence"]
    if "status" in params and params["status"]:
        st = params["status"].lower()
        if st in ("pending", "done", "skipped"):
            task["status"] = st

    if "due" in params and params["due"]:
        parsed_dt, auto_rec, ask_q = parse_natural_date(params["due"])
        if ask_q:
            return outcome.ask("productivity", ask_q)
        if parsed_dt:
            task["due"] = parsed_dt.isoformat()
            task["reminder_times"] = [task["due"]]
            if auto_rec and not task.get("recurrence"):
                task["recurrence"] = auto_rec

    data["tasks"][task_id] = task
    verified = store.save_and_verify(data)
    if not verified:
        return outcome.unverified("productivity", "Failed to verify update save.", id=task_id)

    def _undo_update():
        d = store.load()
        if task_id in d.get("tasks", {}):
            d["tasks"][task_id] = old_task_snapshot
            store.save_and_verify(d)
        return f"Task '{old_task_snapshot.get('title')}' reverted to previous state."

    push_undo(f"update task '{task['title']}'", _undo_update)

    return outcome.done("productivity", id=task_id, title=task["title"], updated=True)


def delete_task(store: ProductivityStore, params: dict) -> str:
    """Delete a task (reversible via undo)."""
    data = store.load()
    tasks = data.get("tasks", {})

    target_query = params.get("task_id") or params.get("title") or ""
    task, candidates, err = resolve_task_by_title_or_id(target_query, tasks)
    if err and not task:
        if candidates:
            return outcome.ask("productivity", err)
        return outcome.failed("productivity", err)

    task_id = task["id"]
    task_snapshot = dict(task)

    del data["tasks"][task_id]
    verified = store.save_and_verify(data)
    if not verified:
        return outcome.unverified("productivity", "Failed to verify task deletion.", id=task_id)

    def _undo_delete():
        d = store.load()
        d.setdefault("tasks", {})[task_id] = task_snapshot
        store.save_and_verify(d)
        return f"Deleted task '{task_snapshot.get('title')}' restored."

    push_undo(f"delete task '{task_snapshot.get('title')}'", _undo_delete)

    return outcome.done("productivity", id=task_id, title=task_snapshot["title"], deleted=True)


def daily_summary(store: ProductivityStore, params: dict) -> str:
    """Return summary of today's pending tasks, overdue tasks, and habit status."""
    data = store.load()
    tasks = list(data.get("tasks", {}).values())
    config = data.get("config", {})

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    rollover_enabled = config.get("rollover_missed_tasks", True)
    rolled_over_count = 0

    pending_today = []
    overdue = []
    completed_today = []

    for t in tasks:
        st = t.get("status")
        due = t.get("due")
        completed_at = t.get("completed_at")

        if st == "done" and completed_at and completed_at.startswith(today_str):
            completed_today.append(t)
        elif st == "pending":
            if not due:
                continue
            due_date = due.split("T")[0]
            if due_date == today_str:
                pending_today.append(t)
            elif due_date < today_str:
                overdue.append(t)
                # Rollover daily/weekly items if enabled
                if rollover_enabled and t.get("scope") in ("daily", "weekly"):
                    # Update due date to today
                    try:
                        due_dt = datetime.fromisoformat(due)
                        new_due = datetime.combine(now.date(), due_dt.time())
                        t["due"] = new_due.isoformat()
                        rolled_over_count += 1
                    except Exception:
                        pass

    if rolled_over_count > 0:
        store.save_and_verify(data)

    brief = params.get("brief", False)
    summary_parts = [
        f"Today ({today_str}): {len(pending_today)} due, {len(overdue)} overdue, {len(completed_today)} completed."
    ]
    if rolled_over_count > 0:
        summary_parts.append(f"{rolled_over_count} missed recurring tasks rolled over to today.")

    return outcome.done(
        "productivity",
        date=today_str,
        pending_today=len(pending_today),
        overdue=len(overdue),
        completed_today=len(completed_today),
        rolled_over=rolled_over_count,
        summary=" ".join(summary_parts),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Main Dispatcher (run)
# ─────────────────────────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """
    Auto-discovered plugin entrypoint.
    Dispatches to appropriate action and returns structured outcome string.
    """
    action = (parameters.get("action") or "").strip().lower()
    store = get_store()

    try:
        if action == "add_task":
            result = add_task(store, parameters)
        elif action == "list_tasks":
            result = list_tasks(store, parameters)
        elif action == "complete_task":
            result = complete_task(store, parameters)
        elif action == "update_task":
            result = update_task(store, parameters)
        elif action == "delete_task":
            result = delete_task(store, parameters)
        elif action == "daily_summary":
            result = daily_summary(store, parameters)
        elif action in ("add_habit", "log_habit", "list_habits", "habit_stats", "weekly_review"):
            # Will be fully fleshed out in Part 3
            result = outcome.failed("productivity", f"Habit action '{action}' is being initialized.")
        elif action == "snooze":
            # Will be fully fleshed out in Part 2
            result = outcome.failed("productivity", "Snooze action is being initialized.")
        else:
            result = outcome.failed(
                "productivity",
                f"Unknown productivity action: '{action}'.",
                valid_actions=[
                    "add_task", "list_tasks", "complete_task", "update_task", "delete_task",
                    "daily_summary", "snooze", "add_habit", "log_habit", "list_habits",
                    "habit_stats", "weekly_review"
                ],
            )
    except Exception as e:
        logger.exception(f"[Productivity] Error running action '{action}': {e}")
        result = outcome.failed("productivity", f"Productivity plugin error: {e}")

    if player:
        try:
            player.write_log(f"Productivity: {action} -> {result}")
        except Exception:
            pass

    return result
