"""
plugins/conversation_search.py — Full-Text Search Over Past Conversations & Dialogues.

Consolidates:
  - conversation_search, conversation_list (conversation_tools.py)
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_DB_PATH: Optional[Path] = None
_HAS_FTS5: Optional[bool] = None


def _get_db() -> sqlite3.Connection:
    global _DB_PATH, _HAS_FTS5
    if _DB_PATH is None:
        try:
            from memory.config_manager import get_user_data_dir
            base_dir = get_user_data_dir()
        except Exception:
            base_dir = Path("memory")
        base_dir.mkdir(parents=True, exist_ok=True)
        _DB_PATH = base_dir / "conversations.db"

    conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp REAL NOT NULL,
            date_str TEXT NOT NULL
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_time ON conversation_messages(timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_role ON conversation_messages(role);")

    if _HAS_FTS5 is None:
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts USING fts5(
                    content, role, session_id, content='conversation_messages', content_rowid='id'
                );
            """)
            _HAS_FTS5 = True
        except Exception:
            _HAS_FTS5 = False

    conn.commit()
    return conn


def record_conversation_turn(session_id: str, role: str, content: str) -> None:
    """Record a user or assistant message to persistent conversation history."""
    if not content or not content.strip():
        return
    now = time.time()
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO conversation_messages (session_id, role, content, timestamp, date_str)
            VALUES (?, ?, ?, ?, ?);
            """,
            (session_id, role.lower(), content.strip(), now, date_str),
        )
        msg_id = cur.lastrowid
        if _HAS_FTS5:
            try:
                cur.execute(
                    """
                    INSERT INTO conversation_fts (rowid, content, role, session_id)
                    VALUES (?, ?, ?, ?);
                    """,
                    (msg_id, content.strip(), role.lower(), session_id),
                )
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "search").strip().lower()
    query = str(params.get("query") or "").strip()
    role_filter = str(params.get("role") or "").strip().lower()
    limit = int(params.get("limit") or 10)
    session_id = str(params.get("session_id") or "").strip()

    conn = _get_db()
    try:
        cur = conn.cursor()

        # ── 1. Search Conversations ──────────────────────────────────────────
        if action in ("search", "find", "query"):
            if not query:
                return "Sir, please provide a search keyword or topic to look up in past conversations."

            matches = []
            if _HAS_FTS5:
                try:
                    # Clean query for FTS MATCH syntax
                    clean_q = '"' + query.replace('"', '""') + '"'
                    sql = """
                        SELECT m.id, m.session_id, m.role, m.content, m.date_str
                        FROM conversation_messages m
                        JOIN conversation_fts f ON m.id = f.rowid
                        WHERE conversation_fts MATCH ?
                    """
                    sql_args: list[Any] = [clean_q]
                    if role_filter in ("user", "assistant"):
                        sql += " AND m.role = ?"
                        sql_args.append(role_filter)
                    if session_id:
                        sql += " AND m.session_id = ?"
                        sql_args.append(session_id)
                    sql += " ORDER BY m.id DESC LIMIT ?;"
                    sql_args.append(limit)
                    cur.execute(sql, tuple(sql_args))
                    matches = cur.fetchall()
                except Exception:
                    matches = []

            if not matches:
                # Substring fallback
                sql = """
                    SELECT id, session_id, role, content, date_str
                    FROM conversation_messages
                    WHERE content LIKE ?
                """
                sql_args = [f"%{query}%"]
                if role_filter in ("user", "assistant"):
                    sql += " AND role = ?"
                    sql_args.append(role_filter)
                if session_id:
                    sql += " AND session_id = ?"
                    sql_args.append(session_id)
                sql += " ORDER BY id DESC LIMIT ?;"
                sql_args.append(limit)
                cur.execute(sql, tuple(sql_args))
                matches = cur.fetchall()

            if not matches:
                return f"Sir, no past conversations matched '{query}'."

            summary_lines = []
            for r in matches:
                speaker = "You" if r[2] == "user" else "Jarvis"
                snippet = r[3][:100] + ("..." if len(r[3]) > 100 else "")
                summary_lines.append(f"[{r[4]}] {speaker}: {snippet}")

            if player and hasattr(player, "show_content"):
                disp = "\n\n".join(f"[{r[4]}] {'You' if r[2] == 'user' else 'Jarvis'}:\n{r[3]}" for r in matches)
                player.show_content(f"CONVERSATION SEARCH: {query}", disp)

            return f"Found {len(matches)} matching turns. Recent: " + "; ".join(summary_lines[:3])

        # ── 2. List Recent Sessions & Messages ───────────────────────────────
        elif action in ("list", "recent", "history"):
            sql = "SELECT id, session_id, role, content, date_str FROM conversation_messages WHERE 1=1"
            sql_args = []
            if role_filter in ("user", "assistant"):
                sql += " AND role = ?"
                sql_args.append(role_filter)
            if session_id:
                sql += " AND session_id = ?"
                sql_args.append(session_id)
            sql += " ORDER BY id DESC LIMIT ?;"
            sql_args.append(limit)

            cur.execute(sql, tuple(sql_args))
            rows = cur.fetchall()
            if not rows:
                return "Sir, there are no recorded conversation messages yet."

            turns = []
            for r in rows:
                spk = "You" if r[2] == "user" else "Jarvis"
                turns.append(f"{spk}: {r[3][:60]}")

            return f"Retrieved {len(rows)} recent messages: " + " | ".join(turns[:4])

        else:
            return f"Unknown conversation search action '{action}'. Use 'search' or 'list'."
    finally:
        conn.close()


PLUGIN = {
    "name": "conversation_search",
    "description": (
        "Full-text search and historical transcript lookup across past conversations. "
        "Allows finding past discussions, statements, user instructions, and assistant replies by keyword."
    ),
    "brief": "Full-text search over past conversation history",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Conversation action: 'search' or 'list'",
                "enum": ["search", "list"],
            },
            "query": {
                "type": "STRING",
                "description": "Search keyword, phrase, or topic to query in past dialogues",
            },
            "role": {
                "type": "STRING",
                "description": "Filter by message speaker: 'user' or 'assistant'",
                "enum": ["user", "assistant"],
            },
            "session_id": {
                "type": "STRING",
                "description": "Optional conversation session identifier",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Maximum number of conversation turns to return (default: 10)",
            },
        },
        "required": ["action"],
    },
    "permissions": ["file:read"],
}
