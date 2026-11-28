"""
actions/timers_stopwatch.py — Timers and Stopwatch for JARVIS.

Provides accurate, non-blocking countdown timers and stopwatch functionality:
- timer_set: Set countdown timer with label and duration.
- timer_list: List active/completed timers with remaining time.
- timer_cancel: Cancel a running timer.
- stopwatch_start: Start or resume a stopwatch.
- stopwatch_stop: Pause/stop the stopwatch.
- stopwatch_lap: Record a lap split.
- stopwatch_reset: Reset the stopwatch back to zero.
- stopwatch_status: Query elapsed time and laps.

Persistent records stored in SQLite WAL database (memory/timers.db).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from core import outcome

logger = logging.getLogger(__name__)

_DB_PATH = Path("memory") / "timers.db"
_LOCK = threading.RLock()
_ACTIVE_TIMERS: Dict[str, threading.Timer] = {}


def _get_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timers (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            duration_sec REAL NOT NULL,
            started_at REAL NOT NULL,
            ends_at REAL NOT NULL,
            status TEXT NOT NULL,
            created_iso TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stopwatches (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            state TEXT NOT NULL,
            start_mono REAL,
            elapsed_accum REAL NOT NULL DEFAULT 0.0,
            laps_json TEXT NOT NULL DEFAULT '[]',
            updated_iso TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _on_timer_finished(timer_id: str, label: str, player: Any = None) -> None:
    with _LOCK:
        _ACTIVE_TIMERS.pop(timer_id, None)
        try:
            conn = _get_db()
            with conn:
                conn.execute(
                    "UPDATE timers SET status = 'completed' WHERE id = ? AND status = 'running'",
                    (timer_id,),
                )
        except Exception as e:
            logger.error("Failed to mark timer %s completed: %s", timer_id, e)

    msg = f"⏰ Timer finished: '{label}'"
    if player and hasattr(player, "write_log"):
        try:
            player.write_log(msg)
        except Exception:
            pass
    logger.info(msg)


def timer_set(
    duration_seconds: float,
    label: str = "Timer",
    player: Any = None,
) -> str:
    if duration_seconds <= 0:
        return outcome.failed("timer_set", "Duration must be greater than 0 seconds.")

    timer_id = f"tmr_{uuid.uuid4().hex[:8]}"
    now_ts = time.time()
    ends_at = now_ts + duration_seconds
    now_iso = datetime.now(timezone.utc).isoformat()

    with _LOCK:
        conn = _get_db()
        with conn:
            conn.execute(
                """
                INSERT INTO timers (id, label, duration_sec, started_at, ends_at, status, created_iso)
                VALUES (?, ?, ?, ?, ?, 'running', ?)
                """,
                (timer_id, label, duration_seconds, now_ts, ends_at, now_iso),
            )

        t = threading.Timer(duration_seconds, _on_timer_finished, args=[timer_id, label, player])
        t.daemon = True
        _ACTIVE_TIMERS[timer_id] = t
        t.start()

    minutes = int(duration_seconds // 60)
    secs = int(duration_seconds % 60)
    time_str = f"{minutes}m {secs}s" if minutes > 0 else f"{secs}s"

    if player and hasattr(player, "write_log"):
        try:
            player.write_log(f"⏰ Timer set for {time_str}: '{label}' ({timer_id})")
        except Exception:
            pass

    return outcome.done(
        "timer_set",
        timer_id=timer_id,
        label=label,
        duration_seconds=duration_seconds,
        formatted=time_str,
        ends_at=datetime.fromtimestamp(ends_at, tz=timezone.utc).strftime("%H:%M:%S UTC"),
    )


def timer_list() -> str:
    now_ts = time.time()
    with _LOCK:
        conn = _get_db()
        rows = conn.execute(
            "SELECT id, label, duration_sec, started_at, ends_at, status, created_iso FROM timers ORDER BY started_at DESC LIMIT 20"
        ).fetchall()

    active: List[Dict[str, Any]] = []
    completed: List[Dict[str, Any]] = []

    for r in rows:
        status = r["status"]
        rem = max(0.0, r["ends_at"] - now_ts)
        item = {
            "id": r["id"],
            "label": r["label"],
            "duration_sec": r["duration_sec"],
            "remaining_sec": round(rem, 1),
            "status": status,
        }
        if status == "running" and rem > 0:
            active.append(item)
        else:
            completed.append(item)

    return outcome.done(
        "timer_list",
        active_count=len(active),
        active_timers=active,
        recent_completed=completed[:5],
    )


def timer_cancel(timer_id: str) -> str:
    with _LOCK:
        t = _ACTIVE_TIMERS.pop(timer_id, None)
        if t:
            t.cancel()

        conn = _get_db()
        with conn:
            cur = conn.execute(
                "UPDATE timers SET status = 'cancelled' WHERE id = ? AND status = 'running'",
                (timer_id,),
            )
            affected = cur.rowcount

    if affected > 0 or t is not None:
        return outcome.done("timer_cancel", timer_id=timer_id, status="cancelled")
    return outcome.failed("timer_cancel", f"Active timer with id '{timer_id}' not found.")


def stopwatch_start(label: str = "default") -> str:
    now_mono = time.monotonic()
    now_iso = datetime.now(timezone.utc).isoformat()

    with _LOCK:
        conn = _get_db()
        row = conn.execute("SELECT * FROM stopwatches WHERE label = ?", (label,)).fetchone()
        if row:
            if row["state"] == "running":
                return outcome.done("stopwatch_start", label=label, status="already running")
            with conn:
                conn.execute(
                    "UPDATE stopwatches SET state = 'running', start_mono = ?, updated_iso = ? WHERE label = ?",
                    (now_mono, now_iso, label),
                )
        else:
            sw_id = f"sw_{uuid.uuid4().hex[:8]}"
            with conn:
                conn.execute(
                    """
                    INSERT INTO stopwatches (id, label, state, start_mono, elapsed_accum, laps_json, updated_iso)
                    VALUES (?, ?, 'running', ?, 0.0, '[]', ?)
                    """,
                    (sw_id, label, now_mono, now_iso),
                )

    return outcome.done("stopwatch_start", label=label, status="running")


def stopwatch_stop(label: str = "default") -> str:
    now_mono = time.monotonic()
    now_iso = datetime.now(timezone.utc).isoformat()

    with _LOCK:
        conn = _get_db()
        row = conn.execute("SELECT * FROM stopwatches WHERE label = ?", (label,)).fetchone()
        if not row:
            return outcome.failed("stopwatch_stop", f"Stopwatch '{label}' does not exist.")

        if row["state"] != "running":
            return outcome.done(
                "stopwatch_stop",
                label=label,
                status="already stopped",
                total_elapsed_sec=round(row["elapsed_accum"], 2),
            )

        start = row["start_mono"] or now_mono
        additional = max(0.0, now_mono - start)
        new_accum = row["elapsed_accum"] + additional

        with conn:
            conn.execute(
                "UPDATE stopwatches SET state = 'stopped', start_mono = NULL, elapsed_accum = ?, updated_iso = ? WHERE label = ?",
                (new_accum, now_iso, label),
            )

    return outcome.done(
        "stopwatch_stop",
        label=label,
        status="stopped",
        total_elapsed_sec=round(new_accum, 2),
    )


def stopwatch_lap(label: str = "default") -> str:
    now_mono = time.monotonic()

    with _LOCK:
        conn = _get_db()
        row = conn.execute("SELECT * FROM stopwatches WHERE label = ?", (label,)).fetchone()
        if not row:
            return outcome.failed("stopwatch_lap", f"Stopwatch '{label}' does not exist.")

        elapsed = row["elapsed_accum"]
        if row["state"] == "running" and row["start_mono"]:
            elapsed += max(0.0, now_mono - row["start_mono"])

        try:
            laps = json.loads(row["laps_json"])
        except Exception:
            laps = []

        lap_num = len(laps) + 1
        lap_entry = {"lap": lap_num, "elapsed_sec": round(elapsed, 2)}
        laps.append(lap_entry)

        with conn:
            conn.execute(
                "UPDATE stopwatches SET laps_json = ? WHERE label = ?",
                (json.dumps(laps), label),
            )

    return outcome.done(
        "stopwatch_lap",
        label=label,
        lap=lap_num,
        lap_elapsed_sec=round(elapsed, 2),
    )


def stopwatch_reset(label: str = "default") -> str:
    now_iso = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        conn = _get_db()
        row = conn.execute("SELECT * FROM stopwatches WHERE label = ?", (label,)).fetchone()
        if not row:
            return outcome.failed("stopwatch_reset", f"Stopwatch '{label}' does not exist.")

        with conn:
            conn.execute(
                "UPDATE stopwatches SET state = 'reset', start_mono = NULL, elapsed_accum = 0.0, laps_json = '[]', updated_iso = ? WHERE label = ?",
                (now_iso, label),
            )

    return outcome.done("stopwatch_reset", label=label, status="reset", total_elapsed_sec=0.0)


def stopwatch_status(label: str = "default") -> str:
    now_mono = time.monotonic()
    with _LOCK:
        conn = _get_db()
        row = conn.execute("SELECT * FROM stopwatches WHERE label = ?", (label,)).fetchone()
        if not row:
            return outcome.done(
                "stopwatch_status",
                label=label,
                exists=False,
                state="not_started",
                total_elapsed_sec=0.0,
            )

        elapsed = row["elapsed_accum"]
        if row["state"] == "running" and row["start_mono"]:
            elapsed += max(0.0, now_mono - row["start_mono"])

        try:
            laps = json.loads(row["laps_json"])
        except Exception:
            laps = []

    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    ms = int((elapsed - int(elapsed)) * 100)
    formatted = f"{mins:02d}:{secs:02d}.{ms:02d}"

    return outcome.done(
        "stopwatch_status",
        label=label,
        exists=True,
        state=row["state"],
        total_elapsed_sec=round(elapsed, 2),
        formatted=formatted,
        laps_count=len(laps),
        laps=laps,
    )


def timers_stopwatch(parameters: dict, player: Any = None, **kwargs) -> str:
    params = parameters or {}
    action = params.get("action", "").lower().strip()

    if action in ("timer_set", "set_timer"):
        duration = params.get("duration_seconds")
        if duration is None and "seconds" in params:
            duration = params.get("seconds")
        if duration is None and "minutes" in params:
            duration = float(params.get("minutes", 0)) * 60.0
        if duration is None:
            return outcome.failed("timer_set", "Missing required parameter 'duration_seconds' or 'minutes'.")
        label = params.get("label", "Timer")
        return timer_set(float(duration), label=str(label), player=player)

    elif action in ("timer_list", "list_timers"):
        return timer_list()

    elif action in ("timer_cancel", "cancel_timer"):
        timer_id = params.get("timer_id") or params.get("id")
        if not timer_id:
            return outcome.failed("timer_cancel", "Missing required parameter 'timer_id'.")
        return timer_cancel(str(timer_id))

    elif action in ("stopwatch_start", "start_stopwatch"):
        label = params.get("label", "default")
        return stopwatch_start(str(label))

    elif action in ("stopwatch_stop", "stop_stopwatch", "pause_stopwatch"):
        label = params.get("label", "default")
        return stopwatch_stop(str(label))

    elif action in ("stopwatch_lap", "lap_stopwatch"):
        label = params.get("label", "default")
        return stopwatch_lap(str(label))

    elif action in ("stopwatch_reset", "reset_stopwatch"):
        label = params.get("label", "default")
        return stopwatch_reset(str(label))

    elif action in ("stopwatch_status", "status_stopwatch"):
        label = params.get("label", "default")
        return stopwatch_status(str(label))

    supported_actions = [
        "timer_set",
        "timer_list",
        "timer_cancel",
        "stopwatch_start",
        "stopwatch_stop",
        "stopwatch_lap",
        "stopwatch_reset",
        "stopwatch_status",
    ]
    return outcome.failed(
        "timers_stopwatch",
        f"Action '{action}' is not supported. Supported: {', '.join(supported_actions)}",
        action=action,
    )


TOOL = {
    "name": "timers_stopwatch",
    "permission": "normal",
    "description": (
        "Timers and stopwatch manager: countdown timers and multi-lap stopwatches. "
        "Supports action: 'timer_set' (with 'duration_seconds' and 'label'), 'timer_list', "
        "'timer_cancel' (with 'timer_id'), 'stopwatch_start', 'stopwatch_stop', 'stopwatch_lap', "
        "'stopwatch_reset', 'stopwatch_status' (with 'label')."
    ),
    "brief": "Manage countdown timers and stopwatches.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "Exact action: timer_set | timer_list | timer_cancel | "
                    "stopwatch_start | stopwatch_stop | stopwatch_lap | stopwatch_reset | stopwatch_status"
                ),
            },
            "duration_seconds": {
                "type": "NUMBER",
                "description": "Duration in seconds for countdown timer.",
            },
            "minutes": {
                "type": "NUMBER",
                "description": "Alternative duration in minutes for countdown timer.",
            },
            "label": {
                "type": "STRING",
                "description": "Label or description for the timer or stopwatch.",
            },
            "timer_id": {
                "type": "STRING",
                "description": "Timer ID to cancel (e.g. tmr_a1b2c3d4).",
            },
        },
        "required": ["action"],
    },
    "handler": timers_stopwatch,
}
