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
                    "coaching_nudge",
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
#  Notifications & Scheduler
# ─────────────────────────────────────────────────────────────────────────────

def _dispatch_desktop_notification(title: str, message: str) -> bool:
    """Send native desktop notification reusing actions/reminder.py mechanism."""
    try:
        from actions.reminder import notify_user
        return bool(notify_user(message, title=title))
    except Exception as e:
        logger.warning(f"[Productivity] Desktop notification dispatch failed: {e}")
        return False


def check_due_reminders(
    store: ProductivityStore,
    now: Optional[datetime] = None,
    player=None,
    boot_check: bool = False,
) -> List[dict]:
    """
    Check for pending task reminders that are due.
    - If boot_check is True or first run, fires reminders missed while JARVIS was closed once, marked as [MISSED].
    - Normal due reminders fire once when due.
    - Persists already-notified IDs so restarts never dupe.
    Returns list of dispatched reminder records.
    """
    curr = now or datetime.now()
    curr_iso = curr.isoformat()

    data = store.load()
    tasks = data.get("tasks", {})
    notified_ids = set(data.get("notified_reminder_ids", []))
    last_missed_check = data.get("last_missed_check")
    dispatched = []

    is_first_startup_check = boot_check or (last_missed_check is None)

    for task_id, task in tasks.items():
        if task.get("status") != "pending":
            continue
        due = task.get("due")
        if not due:
            continue

        try:
            due_dt = datetime.fromisoformat(due)
        except Exception:
            continue

        # Check all reminder times or due
        reminder_times = task.get("reminder_times") or [due]
        for rem_str in reminder_times:
            rem_key = f"{task_id}:{rem_str}"
            if rem_key in notified_ids:
                continue

            try:
                rem_dt = datetime.fromisoformat(rem_str)
            except Exception:
                rem_dt = due_dt

            if rem_dt <= curr:
                # This reminder is due!
                is_missed = bool(boot_check)
                prio_str = task.get("priority", "normal").upper()
                due_display = rem_dt.strftime("%Y-%m-%d %I:%M %p")

                if is_missed:
                    notif_title = f"[MISSED] {task.get('title')} [{prio_str}]"
                    notif_msg = f"Was due {due_display} (missed while closed)."
                    spk_text = f"Sir, you have a missed reminder for {task.get('title')}, which was due earlier at {due_display}."
                else:
                    notif_title = f"Reminder: {task.get('title')} [{prio_str}]"
                    notif_msg = f"Due: {due_display} | Priority: {task.get('priority', 'normal')}"
                    spk_text = f"Sir, reminder for your task: {task.get('title')}."

                success = _dispatch_desktop_notification(notif_title, notif_msg)

                if player:
                    try:
                        player.write_log(f"🔔 {notif_title}: {notif_msg}")
                    except Exception:
                        pass
                    if hasattr(player, "request_say") and callable(player.request_say):
                        try:
                            player.request_say(spk_text)
                        except Exception as se:
                            logger.debug(f"[Productivity] Speak reminder error: {se}")

                notified_ids.add(rem_key)
                data["last_reminded_task_id"] = task_id
                dispatched.append({
                    "task_id": task_id,
                    "title": task.get("title"),
                    "key": rem_key,
                    "missed": is_missed,
                    "verified": success,
                })

    if dispatched or is_first_startup_check:
        data["notified_reminder_ids"] = list(notified_ids)
        data["last_missed_check"] = curr_iso
        store.save_and_verify(data)

    return dispatched


def snooze(store: ProductivityStore, params: dict) -> str:
    """Snooze the last reminded task or a specified task."""
    data = store.load()
    tasks = data.get("tasks", {})
    duration = int(params.get("duration_minutes") or 10)
    if duration <= 0:
        duration = 10

    target_query = params.get("task_id") or params.get("title")
    task = None
    if target_query:
        task, candidates, err = resolve_task_by_title_or_id(target_query, tasks)
        if err and not task:
            if candidates:
                return outcome.ask("productivity", err)
            return outcome.failed("productivity", err)
    else:
        last_id = data.get("last_reminded_task_id")
        if last_id and last_id in tasks and tasks[last_id].get("status") == "pending":
            task = tasks[last_id]
        else:
            # Fall back to most urgently due pending task
            pending_with_due = [t for t in tasks.values() if t.get("status") == "pending" and t.get("due")]
            if pending_with_due:
                pending_with_due.sort(key=lambda t: t.get("due", ""))
                task = pending_with_due[0]

    if not task:
        return outcome.failed("productivity", "No recently reminded task found to snooze.")

    task_id = task["id"]
    old_task_snapshot = dict(task)

    now = datetime.now()
    new_due = now + timedelta(minutes=duration)
    new_due_iso = new_due.isoformat()

    task["due"] = new_due_iso
    reminder_times = task.get("reminder_times", [])
    if new_due_iso not in reminder_times:
        reminder_times.append(new_due_iso)
    task["reminder_times"] = reminder_times

    # Remove any notified entry matching this new due time so it fires cleanly
    notified_ids = set(data.get("notified_reminder_ids", []))
    new_key = f"{task_id}:{new_due_iso}"
    notified_ids.discard(new_key)
    data["notified_reminder_ids"] = list(notified_ids)

    data["tasks"][task_id] = task
    data["last_reminded_task_id"] = task_id
    verified = store.save_and_verify(data)

    if not verified:
        return outcome.unverified("productivity", "Failed to verify snooze save.", id=task_id)

    def _undo_snooze():
        d = store.load()
        if task_id in d.get("tasks", {}):
            d["tasks"][task_id] = old_task_snapshot
            store.save_and_verify(d)
        return f"Snooze on '{old_task_snapshot.get('title')}' reverted."

    push_undo(f"snooze task '{task['title']}' for {duration}m", _undo_snooze)

    return outcome.done(
        "productivity",
        id=task_id,
        title=task["title"],
        snoozed_minutes=duration,
        new_due=new_due_iso,
    )


class ProductivityScheduler:
    """Daemon scheduler checking due reminders every 30 seconds."""

    def __init__(
        self,
        store: Optional[ProductivityStore] = None,
        player=None,
        interval_seconds: float = 30.0,
    ):
        self.store = store or get_store()
        self.player = player
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        # Initial boot check: fire missed reminders once
        try:
            check_due_reminders(self.store, player=self.player, boot_check=True)
        except Exception as e:
            logger.error(f"[ProductivityScheduler] Initial boot check failed: {e}")

        def _loop():
            while not self._stop_event.is_set():
                try:
                    check_due_reminders(self.store, player=self.player, boot_check=False)
                except Exception as ex:
                    logger.error(f"[ProductivityScheduler] Periodic reminder check error: {ex}")
                self._stop_event.wait(self.interval_seconds)

        self._thread = threading.Thread(target=_loop, daemon=True, name="productivity-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None


_global_scheduler: Optional[ProductivityScheduler] = None


def ensure_scheduler_started(player=None) -> ProductivityScheduler:
    global _global_scheduler
    if _global_scheduler is None:
        _global_scheduler = ProductivityScheduler(player=player)
        _global_scheduler.start()
    elif player and not _global_scheduler.player:
        _global_scheduler.player = player
    return _global_scheduler


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

    # Active habits and streaks
    habits = [h for h in data.get("habits", {}).values() if not h.get("archived", False)]
    habit_items = []
    for h in habits:
        done_icon = "✓" if today_str in h.get("log", []) else "○"
        habit_items.append(f"{done_icon} {h.get('name')} (streak: {h.get('streak', 0)}d)")

    brief = params.get("brief", False)
    summary_parts = [
        f"Today ({today_str}): {len(pending_today)} due, {len(overdue)} overdue, {len(completed_today)} completed."
    ]
    if rolled_over_count > 0:
        summary_parts.append(f"{rolled_over_count} missed recurring tasks rolled over to today.")
    if habit_items:
        summary_parts.append("Habit streaks: " + "; ".join(habit_items))

    return outcome.done(
        "productivity",
        date=today_str,
        pending_today=len(pending_today),
        overdue=len(overdue),
        completed_today=len(completed_today),
        rolled_over=rolled_over_count,
        habit_streaks="; ".join(habit_items) if habit_items else "None",
        summary=" ".join(summary_parts),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Habit Tracking & Coaching Analytics
# ─────────────────────────────────────────────────────────────────────────────

def resolve_habit_by_name_or_id(
    query: str,
    habits: Dict[str, dict],
) -> Tuple[Optional[dict], List[dict], Optional[str]]:
    """Resolve habit deterministically or return ambiguous candidates."""
    q = (query or "").strip()
    if not q:
        return None, [], "Please specify the habit name or ID."

    # 1. Exact ID match
    if q in habits:
        return habits[q], [], None

    # 2. Exact Name match (case-insensitive)
    exact = [h for h in habits.values() if h.get("name", "").strip().lower() == q.lower()]
    if len(exact) == 1:
        return exact[0], [], None
    elif len(exact) > 1:
        names_str = ", ".join(f"'{c.get('name')}' (id: {c.get('id')})" for c in exact)
        return None, exact, f"Multiple habits match '{q}': {names_str}. Which one did you mean?"

    # 3. Fuzzy & Substring Match
    scored = []
    q_lower = q.lower()
    for h in habits.values():
        name = h.get("name", "")
        n_lower = name.strip().lower()
        if q_lower in n_lower:
            ratio = 0.85 + (len(q_lower) / max(len(n_lower), 1)) * 0.14
        else:
            ratio = difflib.SequenceMatcher(None, q_lower, n_lower).ratio()

        if ratio >= 0.55:
            scored.append((ratio, h))

    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return None, [], f"No habit found matching '{q}'."

    top_score, top_habit = scored[0]
    close = [h for score, h in scored if score >= 0.55 and (top_score - score) <= 0.15]
    if len(close) > 1:
        c_str = ", ".join(f"'{c.get('name')}' (id: {c.get('id')})" for c in close)
        return None, close, f"Multiple habits might match '{q}': {c_str}. Which one did you mean?"

    if top_score >= 0.6:
        return top_habit, [], None

    return None, [], f"No habit clearly matched '{q}'. Closest was '{top_habit.get('name')}'."


def calculate_habit_streaks(log_dates: List[str], today: Optional[date] = None) -> Tuple[int, int]:
    """
    Calculate current streak and longest streak from list of YYYY-MM-DD date strings.
    Handles day boundaries and timezone date normalization.
    """
    if not log_dates:
        return 0, 0

    curr_d = today or datetime.now().date()
    parsed_dates = set()
    for ds in log_dates:
        try:
            parsed_dates.add(date.fromisoformat(ds.split("T")[0]))
        except Exception:
            pass

    if not parsed_dates:
        return 0, 0

    sorted_dates = sorted(parsed_dates)

    # 1. Current streak: active if logged today or yesterday
    current_streak = 0
    check_d = curr_d
    if check_d not in parsed_dates:
        check_d = curr_d - timedelta(days=1)

    while check_d in parsed_dates:
        current_streak += 1
        check_d -= timedelta(days=1)

    # 2. Longest streak in history
    longest_streak = 0
    running = 0
    prev_d = None
    for d in sorted_dates:
        if prev_d is None or (d - prev_d).days == 1:
            running += 1
        elif (d - prev_d).days > 1:
            running = 1
        longest_streak = max(longest_streak, running)
        prev_d = d

    longest_streak = max(longest_streak, current_streak)
    return current_streak, longest_streak


def calculate_completion_rate(
    log_dates: List[str],
    days: int,
    target: str = "daily",
    today: Optional[date] = None,
) -> float:
    """Calculate percentage completion rate over the last N days."""
    if not log_dates or days <= 0:
        return 0.0

    curr_d = today or datetime.now().date()
    start_d = curr_d - timedelta(days=days - 1)

    parsed = set()
    for ds in log_dates:
        try:
            d = date.fromisoformat(ds.split("T")[0])
            if start_d <= d <= curr_d:
                parsed.add(d)
        except Exception:
            pass

    # Determine expected completions
    target_lower = target.lower().strip()
    if "3x" in target_lower:
        expected = max(1, round(days * 3 / 7))
    elif "4x" in target_lower:
        expected = max(1, round(days * 4 / 7))
    elif "5x" in target_lower:
        expected = max(1, round(days * 5 / 7))
    else:
        expected = days

    pct = (len(parsed) / expected) * 100.0
    return min(100.0, round(pct, 1))


def find_best_weekday(log_dates: List[str]) -> Optional[str]:
    """Find the weekday with the most completions if enough history exists."""
    if len(log_dates) < 3:
        return None

    day_counts: Dict[str, int] = {}
    for ds in log_dates:
        try:
            d = date.fromisoformat(ds.split("T")[0])
            w = d.strftime("%A")
            day_counts[w] = day_counts.get(w, 0) + 1
        except Exception:
            pass

    if not day_counts:
        return None
    return max(day_counts, key=day_counts.get)


def add_habit(store: ProductivityStore, params: dict) -> str:
    """Add a new habit to track."""
    name = (params.get("name") or params.get("title") or "").strip()
    if not name:
        return outcome.failed("productivity", "Habit name cannot be empty.")

    target = (params.get("target") or "daily").strip()
    reminder_time = params.get("reminder_time")
    if reminder_time:
        reminder_time = reminder_time.strip()

    habit_id = str(uuid.uuid4())[:8]
    now_iso = datetime.now().isoformat()

    new_habit = {
        "id": habit_id,
        "name": name,
        "target": target,
        "reminder_time": reminder_time,
        "log": [],
        "streak": 0,
        "longest_streak": 0,
        "created_at": now_iso,
        "archived": False,
    }

    data = store.load()
    data.setdefault("habits", {})[habit_id] = new_habit
    verified = store.save_and_verify(data)
    if not verified:
        return outcome.unverified("productivity", "Failed to verify habit save.", id=habit_id)

    def _undo_add_habit():
        d = store.load()
        if habit_id in d.get("habits", {}):
            del d["habits"][habit_id]
            store.save_and_verify(d)
        return f"Habit '{name}' removed."

    push_undo(f"add habit '{name}'", _undo_add_habit)

    return outcome.done(
        "productivity",
        id=habit_id,
        name=name,
        target=target,
        reminder_time=reminder_time or "none",
    )


def log_habit(store: ProductivityStore, params: dict) -> str:
    """Log completion of a habit for today or a specific date."""
    data = store.load()
    habits = data.get("habits", {})

    target_query = params.get("habit_id") or params.get("name") or params.get("title") or ""
    habit, candidates, err = resolve_habit_by_name_or_id(target_query, habits)
    if err and not habit:
        if candidates:
            return outcome.ask("productivity", err)
        return outcome.failed("productivity", err)

    habit_id = habit["id"]
    old_snapshot = {
        **habit,
        "log": list(habit.get("log", [])),
    }

    target_date = params.get("date")
    if target_date:
        parsed_dt, _, _ = parse_natural_date(target_date)
        log_d = parsed_dt.date() if parsed_dt else datetime.now().date()
    else:
        log_d = datetime.now().date()

    log_d_str = log_d.isoformat()
    current_log = habit.setdefault("log", [])
    already_logged = log_d_str in current_log

    if not already_logged:
        current_log.append(log_d_str)
        current_log.sort()

    # Recalculate streaks
    streak, longest = calculate_habit_streaks(current_log, today=datetime.now().date())
    habit["streak"] = streak
    habit["longest_streak"] = max(habit.get("longest_streak", 0), longest)

    data["habits"][habit_id] = habit
    verified = store.save_and_verify(data)
    if not verified:
        return outcome.unverified("productivity", "Failed to verify habit log save.", id=habit_id)

    def _undo_log():
        d = store.load()
        if habit_id in d.get("habits", {}):
            d["habits"][habit_id] = old_snapshot
            store.save_and_verify(d)
        return f"Habit log for '{old_snapshot.get('name')}' reverted."

    push_undo(f"log habit '{habit['name']}'", _undo_log)

    return outcome.done(
        "productivity",
        id=habit_id,
        name=habit["name"],
        date=log_d_str,
        streak=streak,
        longest_streak=habit["longest_streak"],
        already_logged=already_logged,
    )


def list_habits(store: ProductivityStore, params: dict) -> str:
    """List habits with current streaks and today's status."""
    data = store.load()
    habits = list(data.get("habits", {}).values())

    filter_status = params.get("status", "active").lower()
    if filter_status == "active":
        habits = [h for h in habits if not h.get("archived", False)]
    elif filter_status == "archived":
        habits = [h for h in habits if h.get("archived", False)]

    today_str = datetime.now().strftime("%Y-%m-%d")
    items = []
    for h in habits:
        done_today = "✓" if today_str in h.get("log", []) else "○"
        s = h.get("streak", 0)
        items.append(f"{done_today} {h.get('name')} (streak: {s}d, target: {h.get('target')})")

    brief = params.get("brief", False)
    if not habits:
        return outcome.done("productivity", count=0, summary="No habits found.")

    return outcome.done(
        "productivity",
        count=len(habits),
        habits="; ".join(items) if not brief else f"{len(habits)} habits",
    )


def habit_stats(store: ProductivityStore, params: dict) -> str:
    """Return statistical insights and consistency rates for a habit."""
    data = store.load()
    habits = data.get("habits", {})

    target_query = params.get("habit_id") or params.get("name") or params.get("title") or ""
    habit, candidates, err = resolve_habit_by_name_or_id(target_query, habits)
    if err and not habit:
        if candidates:
            return outcome.ask("productivity", err)
        return outcome.failed("productivity", err)

    log_dates = habit.get("log", [])
    today = datetime.now().date()
    streak, longest = calculate_habit_streaks(log_dates, today=today)
    rate_7d = calculate_completion_rate(log_dates, 7, habit.get("target", "daily"), today=today)
    rate_30d = calculate_completion_rate(log_dates, 30, habit.get("target", "daily"), today=today)
    rate_90d = calculate_completion_rate(log_dates, 90, habit.get("target", "daily"), today=today)
    best_day = find_best_weekday(log_dates)

    facts = {
        "id": habit["id"],
        "name": habit["name"],
        "total_completions": len(log_dates),
        "streak": streak,
        "longest_streak": longest,
        "rate_7d": f"{rate_7d}%",
        "rate_30d": f"{rate_30d}%",
        "rate_90d": f"{rate_90d}%",
    }
    if best_day:
        facts["best_weekday"] = best_day
    else:
        facts["best_weekday"] = "Not enough history yet"

    return outcome.done("productivity", **facts)


def archive_habit(store: ProductivityStore, params: dict) -> str:
    """Archive a habit (reversible via undo)."""
    data = store.load()
    habits = data.get("habits", {})

    target_query = params.get("habit_id") or params.get("name") or params.get("title") or ""
    habit, candidates, err = resolve_habit_by_name_or_id(target_query, habits)
    if err and not habit:
        if candidates:
            return outcome.ask("productivity", err)
        return outcome.failed("productivity", err)

    habit_id = habit["id"]
    habit["archived"] = True
    data["habits"][habit_id] = habit
    verified = store.save_and_verify(data)
    if not verified:
        return outcome.unverified("productivity", "Failed to verify habit archival.", id=habit_id)

    def _undo_archive():
        d = store.load()
        if habit_id in d.get("habits", {}):
            d["habits"][habit_id]["archived"] = False
            store.save_and_verify(d)
        return f"Habit '{habit.get('name')}' unarchived."

    push_undo(f"archive habit '{habit['name']}'", _undo_archive)

    return outcome.done("productivity", id=habit_id, name=habit["name"], archived=True)


def weekly_review(store: ProductivityStore, params: dict) -> str:
    """
    Produce a short factual summary of the past 7 days and one data-driven suggestion.
    Never invents data; reports insufficient history honestly.
    """
    data = store.load()
    tasks = list(data.get("tasks", {}).values())
    habits = [h for h in data.get("habits", {}).values() if not h.get("archived", False)]

    now = datetime.now()
    today = now.date()
    seven_days_ago = today - timedelta(days=7)

    # 1. Tasks in last 7 days
    completed_7d = []
    pending_count = 0
    overdue_count = 0

    for t in tasks:
        st = t.get("status")
        comp = t.get("completed_at")
        due = t.get("due")
        if st == "done" and comp:
            try:
                comp_d = date.fromisoformat(comp.split("T")[0])
                if comp_d >= seven_days_ago:
                    completed_7d.append(t)
            except Exception:
                pass
        elif st == "pending":
            pending_count += 1
            if due:
                try:
                    due_d = date.fromisoformat(due.split("T")[0])
                    if due_d < today:
                        overdue_count += 1
                except Exception:
                    pass

    # 2. Habit consistency in last 7 days
    habit_performances = []
    skipped_habits = []

    for h in habits:
        log_dates = h.get("log", [])
        rate = calculate_completion_rate(log_dates, 7, h.get("target", "daily"), today=today)
        habit_performances.append((h["name"], rate, h.get("streak", 0)))
        if rate < 50.0:
            skipped_habits.append(h)

    # 3. Formulate one factual suggestion
    total_data_points = len(completed_7d) + sum(len(h.get("log", [])) for h in habits)
    if total_data_points < 3:
        suggestion = "Not enough history to determine weekly patterns yet. Complete a few more days of tasks and habits to unlock tailored suggestions."
    elif overdue_count > 2:
        suggestion = f"You have {overdue_count} overdue tasks accumulating. Consider scheduling smaller daily steps to regain momentum."
    elif skipped_habits:
        worst_habit = min(skipped_habits, key=lambda h: calculate_completion_rate(h.get("log", []), 7, h.get("target", "daily"), today=today))
        rem = worst_habit.get("reminder_time")
        if rem:
            suggestion = f"Consistency dropped on '{worst_habit['name']}' this week. Try adjusting your {rem} reminder to an earlier part of your day."
        else:
            suggestion = f"Consistency dropped on '{worst_habit['name']}' this week. Try assigning a fixed daily reminder time for it."
    elif all(p[1] >= 85.0 for p in habit_performances) and habit_performances:
        suggestion = "Outstanding consistency across all tracked habits this week. Keep this exact rhythm going into next week."
    else:
        suggestion = "Steady weekly progress. Focus on finishing your open tasks before picking up new ones."

    habit_summary_parts = [f"{name}: {rate}% (streak: {streak}d)" for name, rate, streak in habit_performances]
    habits_str = "; ".join(habit_summary_parts) if habit_summary_parts else "No active habits tracked"

    return outcome.done(
        "productivity",
        period=f"{seven_days_ago.isoformat()} to {today.isoformat()}",
        tasks_completed=len(completed_7d),
        tasks_pending=pending_count,
        tasks_overdue=overdue_count,
        habits=habits_str,
        suggestion=suggestion,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Coaching Nudges & Proactive Integration
# ─────────────────────────────────────────────────────────────────────────────

def is_in_quiet_hours(
    now: Optional[datetime] = None,
    start_str: str = "22:00",
    end_str: str = "08:00",
) -> bool:
    """True if `now` falls inside quiet hours interval [start_str, end_str)."""
    curr = (now or datetime.now()).time()
    try:
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
        start_t = dt_time(sh, sm)
        end_t = dt_time(eh, em)
    except Exception:
        start_t = dt_time(22, 0)
        end_t = dt_time(8, 0)

    if start_t > end_t:
        return curr >= start_t or curr < end_t
    else:
        return start_t <= curr < end_t


def generate_coaching_nudge(
    store: ProductivityStore,
    now: Optional[datetime] = None,
    record: bool = True,
) -> Optional[dict]:
    """
    Generate one rate-limited coaching nudge if not in quiet hours and real data warrants it.
    Never invents data.
    """
    curr = now or datetime.now()
    curr_date_str = curr.strftime("%Y-%m-%d")

    data = store.load()
    config = data.get("config", {})
    qh_start = config.get("quiet_hours_start", "22:00")
    qh_end = config.get("quiet_hours_end", "08:00")

    # 1. Quiet hours check
    if is_in_quiet_hours(curr, qh_start, qh_end):
        return None

    # 2. Rate limit check (max N per day)
    max_per_day = int(config.get("max_proactive_nudges_per_day", 2))
    nudge_history = data.get("proactive_nudges_log", [])
    today_nudges = [ts for ts in nudge_history if ts.startswith(curr_date_str)]
    if len(today_nudges) >= max_per_day:
        return None

    # 3. Identify real coaching opportunity
    habits = [h for h in data.get("habits", {}).values() if not h.get("archived", False)]
    tasks = list(data.get("tasks", {}).values())

    nudge = None

    # 3a. Streak protection: habit with active streak >= 2 not logged today
    streak_habits = [h for h in habits if h.get("streak", 0) >= 2 and curr_date_str not in h.get("log", [])]
    if streak_habits:
        streak_habits.sort(key=lambda h: h.get("streak", 0), reverse=True)
        top_h = streak_habits[0]
        nudge = {
            "type": "streak_protection",
            "habit": top_h["name"],
            "streak": top_h["streak"],
            "message": f"Your {top_h['streak']}-day streak for '{top_h['name']}' is active. Don't forget to log it today!",
        }

    # 3b. Frequently skipped habit nudge
    if not nudge and habits:
        low_habits = []
        for h in habits:
            if curr_date_str in h.get("log", []):
                continue
            rate = calculate_completion_rate(h.get("log", []), 7, h.get("target", "daily"), today=curr.date())
            if rate < 50.0:
                low_habits.append((h, rate))

        if low_habits:
            low_habits.sort(key=lambda x: x[1])
            worst_h, worst_rate = low_habits[0]
            nudge = {
                "type": "skipped_habit_nudge",
                "habit": worst_h["name"],
                "rate": worst_rate,
                "message": f"'{worst_h['name']}' has been skipped recently. A quick session today will rebuild your habit.",
            }

    # 3c. Overdue high-priority task nudge
    if not nudge:
        overdue_high = [
            t for t in tasks
            if t.get("status") == "pending"
            and t.get("priority") == "high"
            and t.get("due")
            and t["due"].split("T")[0] < curr_date_str
        ]
        if overdue_high:
            top_t = overdue_high[0]
            nudge = {
                "type": "overdue_task_nudge",
                "task": top_t["title"],
                "message": f"High priority task '{top_t['title']}' is overdue. Would you like to tackle it now?",
            }

    if nudge and record:
        nudge_history.append(curr.isoformat())
        data["proactive_nudges_log"] = nudge_history[-20:]
        store.save_and_verify(data)

    return nudge


def get_productivity_prompt_context(store: Optional[ProductivityStore] = None, now: Optional[datetime] = None) -> str:
    """Return coaching context string for ProactiveEngine prompt builder."""
    s = store or get_store()
    nudge = generate_coaching_nudge(s, now=now, record=True)
    if not nudge:
        return ""
    return nudge.get("message", "")


def coaching_nudge_action(store: ProductivityStore, params: dict) -> str:
    """Action entry point for checking or generating a coaching nudge."""
    nudge = generate_coaching_nudge(store, record=True)
    if not nudge:
        data = store.load()
        config = data.get("config", {})
        qh_start = config.get("quiet_hours_start", "22:00")
        qh_end = config.get("quiet_hours_end", "08:00")
        if is_in_quiet_hours(datetime.now(), qh_start, qh_end):
            reason = "Currently in quiet hours."
        else:
            reason = "No coaching nudge needed or daily rate limit reached."
        return outcome.done("productivity", nudge="none", reason=reason)

    return outcome.done(
        "productivity",
        nudge=nudge["message"],
        type=nudge["type"],
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
        elif action == "add_habit":
            result = add_habit(store, parameters)
        elif action == "log_habit":
            result = log_habit(store, parameters)
        elif action == "list_habits":
            result = list_habits(store, parameters)
        elif action == "habit_stats":
            result = habit_stats(store, parameters)
        elif action == "archive_habit":
            result = archive_habit(store, parameters)
        elif action == "weekly_review":
            result = weekly_review(store, parameters)
        elif action == "coaching_nudge":
            result = coaching_nudge_action(store, parameters)
        elif action == "snooze":
            result = snooze(store, parameters)
        else:
            result = outcome.failed(
                "productivity",
                f"Unknown productivity action: '{action}'.",
                valid_actions=[
                    "add_task", "list_tasks", "complete_task", "update_task", "delete_task",
                    "daily_summary", "snooze", "add_habit", "log_habit", "list_habits",
                    "habit_stats", "weekly_review", "coaching_nudge"
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
