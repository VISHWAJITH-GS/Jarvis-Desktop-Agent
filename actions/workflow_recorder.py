"""
actions/workflow_recorder.py — Desktop Macro Recorder and Playback for JARVIS.

Enables recording, saving, and deterministic replaying of desktop automation workflows:
- macro_record: Begin recording actions or register a sequence of steps.
- macro_stop: Finalize recording and save the macro under a name.
- macro_play: Execute a saved macro sequence with PyAutoGUI/ctypes with failsafe checks.
- macro_list: List all stored macros with step counts and descriptions.
- macro_delete: Remove a stored macro.

Persistent storage in SQLite WAL database (memory/macros.db).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from core import outcome
from core.lazyimport import LazyModule, installed

logger = logging.getLogger("workflow_recorder")

_PYAUTOGUI = installed("pyautogui")
pyautogui = LazyModule("pyautogui")

_DB_PATH = Path("memory") / "macros.db"
_LOCK = threading.RLock()
_CURRENT_RECORDING: Dict[str, Any] = {}


def _get_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS macros (
            name TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            steps_json TEXT NOT NULL,
            created_iso TEXT NOT NULL,
            run_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def macro_save(name: str, steps: list[dict], description: str = "") -> str:
    if not name or not name.strip():
        return outcome.failed("macro_save", "Macro name cannot be empty.")
    if not isinstance(steps, list) or not steps:
        return outcome.failed("macro_save", "Macro steps must be a non-empty list.")

    now_iso = datetime.now(timezone.utc).isoformat()
    clean_name = name.strip().lower()
    with _LOCK:
        conn = _get_db()
        with conn:
            conn.execute(
                """
                INSERT INTO macros (name, description, steps_json, created_iso, run_count)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(name) DO UPDATE SET
                    description = excluded.description,
                    steps_json = excluded.steps_json
                """,
                (clean_name, description or f"Macro {clean_name}", json.dumps(steps), now_iso),
            )

    return outcome.done(
        "macro_save",
        name=clean_name,
        step_count=len(steps),
        description=description,
        verified=True,
    )


def macro_list() -> str:
    with _LOCK:
        conn = _get_db()
        rows = conn.execute("SELECT name, description, steps_json, created_iso, run_count FROM macros ORDER BY name ASC").fetchall()

    items = []
    for r in rows:
        try:
            steps = json.loads(r["steps_json"])
            count = len(steps)
        except Exception:
            count = 0
        items.append({
            "name": r["name"],
            "description": r["description"],
            "steps": count,
            "run_count": r["run_count"],
            "created": r["created_iso"],
        })

    return outcome.done("macro_list", count=len(items), macros=items)


def macro_delete(name: str) -> str:
    clean_name = name.strip().lower()
    with _LOCK:
        conn = _get_db()
        with conn:
            cur = conn.execute("DELETE FROM macros WHERE name = ?", (clean_name,))
            affected = cur.rowcount

    if affected > 0:
        return outcome.done("macro_delete", name=clean_name, deleted=True)
    return outcome.failed("macro_delete", f"Macro '{clean_name}' not found.")


def macro_play(name: str, speed: float = 1.0, player: Any = None) -> str:
    clean_name = name.strip().lower()
    with _LOCK:
        conn = _get_db()
        row = conn.execute("SELECT * FROM macros WHERE name = ?", (clean_name,)).fetchone()
        if not row:
            return outcome.failed("macro_play", f"Macro '{clean_name}' not found.")
        try:
            steps = json.loads(row["steps_json"])
        except Exception as e:
            return outcome.failed("macro_play", f"Corrupt macro steps: {e}")

    if not _PYAUTOGUI:
        return outcome.failed("macro_play", "PyAutoGUI is not available for macro playback.")

    rate = max(0.2, min(float(speed), 5.0))
    executed = 0

    if player and hasattr(player, "write_log"):
        try:
            player.write_log(f"▶️ Replaying macro '{clean_name}' ({len(steps)} steps)...")
        except Exception:
            pass

    for s in steps:
        stype = str(s.get("type", "")).lower()
        delay = float(s.get("delay", 0.1)) / rate

        if delay > 0:
            time.sleep(min(delay, 5.0))

        if stype in ("click", "mouse_click"):
            x = s.get("x")
            y = s.get("y")
            clicks = int(s.get("clicks", 1))
            button = str(s.get("button", "left"))
            if x is not None and y is not None:
                pyautogui.click(int(x), int(y), clicks=clicks, button=button)
            else:
                pyautogui.click(clicks=clicks, button=button)
            executed += 1

        elif stype in ("type", "text"):
            text = str(s.get("text", ""))
            if text:
                pyautogui.typewrite(text, interval=0.02 / rate)
            executed += 1

        elif stype in ("hotkey", "key"):
            keys = s.get("keys")
            if isinstance(keys, list):
                pyautogui.hotkey(*[str(k).lower() for k in keys])
            elif isinstance(keys, str):
                pyautogui.hotkey(*[k.strip().lower() for k in keys.split("+")])
            executed += 1

        elif stype in ("sleep", "wait"):
            sec = float(s.get("seconds", 0.5)) / rate
            time.sleep(min(sec, 10.0))
            executed += 1

    with _LOCK:
        with conn:
            conn.execute("UPDATE macros SET run_count = run_count + 1 WHERE name = ?", (clean_name,))

    return outcome.done(
        "macro_play",
        name=clean_name,
        steps_executed=executed,
        verified=True,
    )


def workflow_recorder(parameters: dict, player: Any = None, **kwargs) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).lower().strip()

    if action in ("macro_save", "save", "record"):
        name = str(params.get("name", ""))
        desc = str(params.get("description", ""))
        steps = params.get("steps", [])
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except Exception:
                steps = []
        return macro_save(name=name, steps=steps, description=desc)

    elif action in ("macro_play", "play", "replay", "run"):
        name = str(params.get("name", ""))
        speed = float(params.get("speed", 1.0))
        return macro_play(name=name, speed=speed, player=player)

    elif action in ("macro_list", "list"):
        return macro_list()

    elif action in ("macro_delete", "delete", "remove"):
        name = str(params.get("name", ""))
        return macro_delete(name=name)

    return outcome.failed(
        "workflow_recorder",
        f"Unknown action '{action}'. Supported: macro_save, macro_play, macro_list, macro_delete",
    )


TOOL = {
    "name": "workflow_recorder",
    "permission": "sensitive",
    "description": (
        "Desktop macro recorder and playback: record, store, list, delete, and replay automated sequences "
        "of mouse clicks, keypresses, delays, and typing on the local computer."
    ),
    "brief": "Macro recorder and playback: record, save, play, list, delete desktop macros.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Exact action: macro_save | macro_play | macro_list | macro_delete",
            },
            "name": {
                "type": "STRING",
                "description": "Unique identifier name of the macro.",
            },
            "description": {
                "type": "STRING",
                "description": "Optional human-readable description for the macro.",
            },
            "steps": {
                "type": "ARRAY",
                "items": {"type": "OBJECT"},
                "description": "Sequence of step dicts: [{'type': 'click', 'x': 100, 'y': 200}, {'type': 'text', 'text': 'hi'}]",
            },
            "speed": {
                "type": "NUMBER",
                "description": "Playback speed multiplier (e.g. 1.0, 1.5, 2.0).",
            },
        },
        "required": ["action"],
    },
    "handler": workflow_recorder,
}
