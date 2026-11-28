"""
plugins/notes_manager.py — Notes, Daily Journaling & Scratchpad Manager.

Consolidates:
  - note_add, note_list, note_search, note_delete, note_today, note_daily_append (note_tools.py)
  - scratchpad_add, scratchpad_list, scratchpad_search, scratchpad_delete, scratchpad_clear (scratchpad_tools.py)
  - daily_note (file_tools.py)
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_DB_PATH: Optional[Path] = None


def _get_db() -> sqlite3.Connection:
    global _DB_PATH
    if _DB_PATH is None:
        try:
            from memory.config_manager import get_user_data_dir
            base_dir = get_user_data_dir()
        except Exception:
            base_dir = Path("memory")
        base_dir.mkdir(parents=True, exist_ok=True)
        _DB_PATH = base_dir / "notes.db"

    conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            tags TEXT DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_notes (
            date TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scratchpad (
            key TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_time ON notes(updated_at);")
    conn.commit()
    return conn


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "list").strip().lower()
    title = str(params.get("title") or "").strip()
    content = str(params.get("content") or params.get("text") or "").strip()
    tags = str(params.get("tags") or "").strip()
    query = str(params.get("query") or "").strip()
    key = str(params.get("key") or "").strip()
    note_id = params.get("id")
    date_str = str(params.get("date") or datetime.now().strftime("%Y-%m-%d")).strip()

    conn = _get_db()
    try:
        # ── 1. Add Note ──────────────────────────────────────────────────────
        if action in ("add", "create", "note_add"):
            if not title and not content:
                return "Sir, please provide a title or content for the note."
            final_title = title or (content[:30] + "..." if len(content) > 30 else content) or "Untitled Note"
            now = time.time()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO notes (title, content, tags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?);
                """,
                (final_title, content, tags, now, now),
            )
            new_id = cur.lastrowid
            conn.commit()
            if player and hasattr(player, "show_content"):
                player.show_content(f"NOTE #{new_id}: {final_title}", content)
            return f"Saved note #{new_id}: '{final_title}'."

        # ── 2. List Notes ────────────────────────────────────────────────────
        elif action in ("list", "note_list", "notes"):
            cur = conn.cursor()
            if tags:
                cur.execute(
                    "SELECT id, title, tags, updated_at FROM notes WHERE tags LIKE ? ORDER BY id DESC LIMIT 20;",
                    (f"%{tags}%",),
                )
            else:
                cur.execute("SELECT id, title, tags, updated_at FROM notes ORDER BY id DESC LIMIT 20;")
            rows = cur.fetchall()
            if not rows:
                return "Sir, you have no notes saved yet."
            summary = ", ".join(f"#{r[0]} '{r[1]}'" for r in rows)
            if player and hasattr(player, "show_content"):
                display_txt = "\n".join(f"• #{r[0]} | {r[1]} (tags: {r[2] or 'none'})" for r in rows)
                player.show_content("NOTES LIST", display_txt)
            return f"You have {len(rows)} notes: {summary}."

        # ── 3. Get / Read Note ───────────────────────────────────────────────
        elif action in ("get", "read", "note_get"):
            cur = conn.cursor()
            if note_id is not None:
                cur.execute("SELECT id, title, content, tags FROM notes WHERE id = ?;", (int(note_id),))
            elif title:
                cur.execute("SELECT id, title, content, tags FROM notes WHERE title LIKE ? LIMIT 1;", (f"%{title}%",))
            else:
                cur.execute("SELECT id, title, content, tags FROM notes ORDER BY id DESC LIMIT 1;")
            row = cur.fetchone()
            if not row:
                return "Sir, I could not find that note."
            if player and hasattr(player, "show_content"):
                player.show_content(f"NOTE #{row[0]}: {row[1]}", row[2])
            return f"Note #{row[0]} '{row[1]}': {row[2][:150]}" + ("..." if len(row[2]) > 150 else "")

        # ── 4. Search Notes ──────────────────────────────────────────────────
        elif action in ("search", "find", "note_search"):
            if not query:
                return "Sir, please specify what you want to search for in your notes."
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, title, content, tags FROM notes
                WHERE title LIKE ? OR content LIKE ? OR tags LIKE ?
                ORDER BY id DESC LIMIT 10;
                """,
                (f"%{query}%", f"%{query}%", f"%{query}%"),
            )
            rows = cur.fetchall()
            if not rows:
                return f"Sir, no notes matched '{query}'."
            summary = ", ".join(f"#{r[0]} '{r[1]}'" for r in rows)
            if player and hasattr(player, "show_content"):
                disp = "\n\n".join(f"=== #{r[0]} {r[1]} ===\n{r[2]}" for r in rows)
                player.show_content(f"SEARCH RESULTS: {query}", disp)
            return f"Found {len(rows)} notes matching '{query}': {summary}."

        # ── 5. Delete Note ───────────────────────────────────────────────────
        elif action in ("delete", "remove", "note_delete"):
            if note_id is None:
                return "Sir, please provide the note ID to delete."
            cur = conn.cursor()
            cur.execute("DELETE FROM notes WHERE id = ?;", (int(note_id),))
            deleted = cur.rowcount
            conn.commit()
            if deleted:
                return f"Deleted note #{note_id}, sir."
            return f"Sir, note #{note_id} was not found."

        # ── 6. Daily Note: Get ───────────────────────────────────────────────
        elif action in ("daily_get", "daily", "note_today"):
            cur = conn.cursor()
            cur.execute("SELECT content FROM daily_notes WHERE date = ?;", (date_str,))
            row = cur.fetchone()
            if not row:
                return f"No daily note entries for {date_str}."
            if player and hasattr(player, "show_content"):
                player.show_content(f"DAILY NOTE — {date_str}", row[0])
            return f"Daily note for {date_str}: {row[0][:150]}" + ("..." if len(row[0]) > 150 else "")

        # ── 7. Daily Note: Append ────────────────────────────────────────────
        elif action in ("daily_append", "daily_add", "note_daily_append"):
            if not content:
                return "Sir, please provide the entry text to add to today's daily note."
            cur = conn.cursor()
            cur.execute("SELECT content FROM daily_notes WHERE date = ?;", (date_str,))
            row = cur.fetchone()
            timestamp_label = datetime.now().strftime("%I:%M %p")
            new_bullet = f"• [{timestamp_label}] {content}"
            if row:
                updated_content = row[0] + "\n" + new_bullet
                cur.execute(
                    "UPDATE daily_notes SET content = ?, updated_at = ? WHERE date = ?;",
                    (updated_content, time.time(), date_str),
                )
            else:
                updated_content = f"# Daily Log: {date_str}\n\n" + new_bullet
                cur.execute(
                    "INSERT INTO daily_notes (date, content, updated_at) VALUES (?, ?, ?);",
                    (date_str, updated_content, time.time()),
                )
            conn.commit()
            if player and hasattr(player, "show_content"):
                player.show_content(f"DAILY NOTE — {date_str}", updated_content)
            return f"Appended entry to daily note for {date_str}."

        # ── 8. Scratchpad: Set ───────────────────────────────────────────────
        elif action in ("scratchpad_set", "scratchpad_add", "scratch_set"):
            k = key or "general"
            if not content:
                return "Sir, please provide the scratchpad text."
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO scratchpad (key, text, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at;
                """,
                (k, content, time.time()),
            )
            conn.commit()
            return f"Updated scratchpad item '{k}'."

        # ── 9. Scratchpad: Get / List ────────────────────────────────────────
        elif action in ("scratchpad_get", "scratchpad_list", "scratch_get"):
            cur = conn.cursor()
            if key:
                cur.execute("SELECT text FROM scratchpad WHERE key = ?;", (key,))
                row = cur.fetchone()
                if not row:
                    return f"No scratchpad item under '{key}'."
                return f"Scratchpad [{key}]: {row[0]}"
            cur.execute("SELECT key, text FROM scratchpad ORDER BY updated_at DESC;")
            rows = cur.fetchall()
            if not rows:
                return "Scratchpad is empty, sir."
            summary = ", ".join(f"{r[0]}: '{r[1][:30]}'" for r in rows)
            return f"Scratchpad items: {summary}."

        # ── 10. Scratchpad: Clear / Delete ───────────────────────────────────
        elif action in ("scratchpad_clear", "scratchpad_delete", "scratch_clear"):
            cur = conn.cursor()
            if key:
                cur.execute("DELETE FROM scratchpad WHERE key = ?;", (key,))
                conn.commit()
                return f"Deleted scratchpad item '{key}'."
            cur.execute("DELETE FROM scratchpad;")
            conn.commit()
            return "Cleared all scratchpad items, sir."

        else:
            return (
                f"Unknown notes action '{action}'. "
                f"Available actions: add, list, get, search, delete, daily_get, daily_append, "
                f"scratchpad_set, scratchpad_get, scratchpad_clear."
            )
    finally:
        conn.close()


PLUGIN = {
    "name": "notes_manager",
    "description": (
        "Structured notes, daily journal logging, and ephemeral scratchpad manager. "
        "Allows creating and tagging notes, searching notes, reading daily journal logs, "
        "appending timestamped entries to today's daily note, and managing quick scratchpad text."
    ),
    "brief": "Notes, daily journaling logs, and scratchpad manager",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Notes action: 'add', 'list', 'get', 'search', 'delete', 'daily_get', 'daily_append', 'scratchpad_set', 'scratchpad_get', or 'scratchpad_clear'",
                "enum": [
                    "add",
                    "list",
                    "get",
                    "search",
                    "delete",
                    "daily_get",
                    "daily_append",
                    "scratchpad_set",
                    "scratchpad_get",
                    "scratchpad_clear",
                ],
            },
            "title": {"type": "STRING", "description": "Title of the note"},
            "content": {"type": "STRING", "description": "Body text or entry content"},
            "tags": {"type": "STRING", "description": "Comma-separated tags (e.g. 'work, ideas, todo')"},
            "query": {"type": "STRING", "description": "Search keyword for notes search"},
            "key": {"type": "STRING", "description": "Key identifier for scratchpad operations"},
            "id": {"type": "INTEGER", "description": "Note ID for get or delete"},
            "date": {"type": "STRING", "description": "Date in YYYY-MM-DD for daily notes (defaults to today)"},
        },
        "required": ["action"],
    },
    "permissions": ["file:read", "file:create_write"],
}
