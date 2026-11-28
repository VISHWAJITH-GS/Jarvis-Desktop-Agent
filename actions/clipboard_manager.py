"""
actions/clipboard_manager.py — Clipboard history, secret filtering, pinned snippets & FTS search.

Consolidates:
  - clipboard_capture, clipboard_history, clipboard_pin, clipboard_pinned, clipboard_paste (clipboard_manager_tools.py)
  - clipboard_search, clipboard_get (clipboard_tools.py)
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.lazyimport import LazyModule, installed
from core.outcome import ActionResult
from core.undo import push_undo

_PYPERCLIP = installed("pyperclip")
pyperclip = LazyModule("pyperclip")

# Secret detection patterns: tokens, API keys, private keys, passwords
SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_-]{20,}"),                          # OpenAI / Anthropic
    re.compile(r"ghp_[a-zA-Z0-9]{30,}"),                           # GitHub Personal Access Token
    re.compile(r"AKIA[0-9A-Z]{16}"),                               # AWS Access Key
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),                         # Google API Key
    re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]{20,}", re.I),           # Bearer Tokens
    re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),        # Private Keys
    re.compile(r"(?:password|passwd|pwd)\s*[:=]\s*\S+", re.I),     # Inline passwords
]

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
        _DB_PATH = base_dir / "clipboard.db"

    conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clipboard_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            timestamp REAL NOT NULL,
            is_pinned INTEGER DEFAULT 0,
            pin_title TEXT,
            is_secret INTEGER DEFAULT 0,
            char_count INTEGER NOT NULL
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clip_time ON clipboard_entries(timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clip_pinned ON clipboard_entries(is_pinned);")
    conn.commit()
    return conn


def detect_and_mask_secrets(text: str) -> tuple[bool, str]:
    """Check for credentials or secret keys; mask them if found."""
    is_secret = False
    cleaned = text
    for pat in SECRET_PATTERNS:
        if pat.search(cleaned):
            is_secret = True
            cleaned = pat.sub("[REDACTED_SECRET]", cleaned)
    return is_secret, cleaned


def _save_to_history(text: str) -> int:
    """Save an item to clipboard history unless it duplicates the most recent entry."""
    if not text or not text.strip():
        return 0
    is_sec, masked = detect_and_mask_secrets(text)
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT content FROM clipboard_entries ORDER BY id DESC LIMIT 1;")
        row = cur.fetchone()
        if row and row[0] == masked:
            return 0  # Deduplicate identical consecutive captures

        cur.execute(
            """
            INSERT INTO clipboard_entries (content, timestamp, is_pinned, is_secret, char_count)
            VALUES (?, ?, 0, ?, ?);
            """,
            (masked, time.time(), 1 if is_sec else 0, len(masked)),
        )
        conn.commit()
        return cur.lastrowid or 0
    finally:
        conn.close()


def clipboard_manager(
    parameters: Optional[Dict[str, Any]] = None,
    player: Any = None,
    session_memory: Any = None,
) -> ActionResult:
    params = parameters or {}
    action = str(params.get("action") or "history").strip().lower()
    text = str(params.get("text") or "").strip()
    query = str(params.get("query") or "").strip()
    title = str(params.get("title") or "").strip()
    limit = int(params.get("limit") or 20)
    entry_id = params.get("id")

    # ── 1. History ───────────────────────────────────────────────────────────
    if action in ("history", "list", "recent"):
        conn = _get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, content, timestamp, is_pinned, pin_title, is_secret, char_count
                FROM clipboard_entries
                ORDER BY is_pinned DESC, id DESC LIMIT ?;
                """,
                (limit,),
            )
            rows = cur.fetchall()
            items = []
            for r in rows:
                items.append({
                    "id": r[0],
                    "content": r[1][:120] + ("..." if len(r[1]) > 120 else ""),
                    "timestamp": r[2],
                    "is_pinned": bool(r[3]),
                    "pin_title": r[4],
                    "is_secret": bool(r[5]),
                    "char_count": r[6],
                })
            return ActionResult.done(
                "clipboard_manager",
                message=f"Retrieved {len(items)} clipboard entries.",
                data={"entries": items, "count": len(items)},
            )
        finally:
            conn.close()

    # ── 2. Capture / Read current clipboard ──────────────────────────────────
    elif action in ("capture", "read", "get"):
        clip_text = pyperclip.paste() if _PYPERCLIP else ""
        if not clip_text:
            return ActionResult.done(
                "clipboard_manager",
                message="Clipboard is empty.",
                data={"content": ""},
            )
        _save_to_history(clip_text)
        is_sec, masked = detect_and_mask_secrets(clip_text)
        safe_preview = masked[:200] + ("..." if len(masked) > 200 else "")
        return ActionResult.done(
            "clipboard_manager",
            message=f"Clipboard contents: '{safe_preview}'.",
            data={
                "content": safe_preview,
                "is_secret": is_sec,
                "length": len(clip_text),
            },
        )

    # ── 3. Paste / Copy to OS clipboard ──────────────────────────────────────
    elif action in ("paste", "copy", "set", "write"):
        target_text = text
        if not target_text and entry_id is not None:
            # Load text from history ID
            conn = _get_db()
            try:
                cur = conn.cursor()
                cur.execute("SELECT content FROM clipboard_entries WHERE id = ?;", (int(entry_id),))
                row = cur.fetchone()
                if row:
                    target_text = row[0]
            finally:
                conn.close()

        if not target_text:
            return ActionResult.failed(
                "clipboard_manager",
                error="No text or valid history entry ID provided to copy to clipboard.",
            )

        if not _PYPERCLIP:
            return ActionResult.unsupported(
                "clipboard_manager",
                "pyperclip is not available on this platform.",
            )

        old_text = pyperclip.paste()
        pyperclip.copy(target_text)
        _save_to_history(target_text)

        # Register undo
        push_undo(
            f"clipboard paste '{target_text[:30]}'",
            lambda: pyperclip.copy(old_text),
        )

        return ActionResult.done(
            "clipboard_manager",
            message=f"Copied {len(target_text)} characters to clipboard.",
            data={"length": len(target_text)},
        )

    # ── 4. Search history ────────────────────────────────────────────────────
    elif action in ("search", "find"):
        if not query:
            return ActionResult.failed("clipboard_manager", error="Parameter 'query' is required for search.")

        conn = _get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, content, timestamp, is_pinned, pin_title, char_count
                FROM clipboard_entries
                WHERE content LIKE ? OR pin_title LIKE ?
                ORDER BY id DESC LIMIT ?;
                """,
                (f"%{query}%", f"%{query}%", limit),
            )
            rows = cur.fetchall()
            matches = [
                {
                    "id": r[0],
                    "content": r[1][:140] + ("..." if len(r[1]) > 140 else ""),
                    "timestamp": r[2],
                    "is_pinned": bool(r[3]),
                    "pin_title": r[4],
                    "char_count": r[5],
                }
                for r in rows
            ]
            return ActionResult.done(
                "clipboard_manager",
                message=f"Found {len(matches)} clipboard entries matching '{query}'.",
                data={"matches": matches, "query": query},
            )
        finally:
            conn.close()

    # ── 5. Pin snippet ───────────────────────────────────────────────────────
    elif action in ("pin", "bookmark"):
        if entry_id is None and not text:
            return ActionResult.failed("clipboard_manager", error="Provide 'id' or 'text' to pin.")

        conn = _get_db()
        try:
            cur = conn.cursor()
            if entry_id is not None:
                cur.execute(
                    "UPDATE clipboard_entries SET is_pinned = 1, pin_title = ? WHERE id = ?;",
                    (title or "Pinned Snippet", int(entry_id)),
                )
            else:
                is_sec, masked = detect_and_mask_secrets(text)
                cur.execute(
                    """
                    INSERT INTO clipboard_entries (content, timestamp, is_pinned, pin_title, is_secret, char_count)
                    VALUES (?, ?, 1, ?, ?, ?);
                    """,
                    (masked, time.time(), title or "Pinned Snippet", 1 if is_sec else 0, len(masked)),
                )
            conn.commit()
            return ActionResult.done(
                "clipboard_manager",
                message=f"Snippet pinned successfully with title '{title or 'Pinned Snippet'}'.",
            )
        finally:
            conn.close()

    # ── 6. Pinned snippets ───────────────────────────────────────────────────
    elif action in ("pinned", "get_pinned"):
        conn = _get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, content, timestamp, pin_title, char_count
                FROM clipboard_entries
                WHERE is_pinned = 1
                ORDER BY id DESC;
                """
            )
            rows = cur.fetchall()
            pinned = [
                {
                    "id": r[0],
                    "content": r[1],
                    "timestamp": r[2],
                    "title": r[3] or f"Snippet #{r[0]}",
                    "char_count": r[4],
                }
                for r in rows
            ]
            return ActionResult.done(
                "clipboard_manager",
                message=f"Retrieved {len(pinned)} pinned snippets.",
                data={"pinned": pinned, "count": len(pinned)},
            )
        finally:
            conn.close()

    # ── 7. Clear history ─────────────────────────────────────────────────────
    elif action in ("clear", "delete_all"):
        conn = _get_db()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM clipboard_entries WHERE is_pinned = 0;")
            deleted = cur.rowcount
            conn.commit()
            return ActionResult.done(
                "clipboard_manager",
                message=f"Cleared {deleted} unpinned clipboard history items.",
            )
        finally:
            conn.close()

    else:
        return ActionResult.unsupported(
            "clipboard_manager",
            f"Unsupported clipboard action '{action}'. Use 'history', 'capture', 'paste', 'search', 'pin', 'pinned', or 'clear'.",
        )


TOOL = {
    "name": "clipboard_manager",
    "description": (
        "Manages OS clipboard history, secret-masked snippet persistence, FTS search, and pinned snippets. "
        "Allows capturing current clipboard, querying history, copying text, pasting past snippets, "
        "and pinning important text snippets."
    ),
    "brief": "Clipboard history, secret filtering, snippet pinning, and search",
    "tier": "core",
    "permission": "normal",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Clipboard action: 'history', 'capture', 'paste', 'search', 'pin', 'pinned', or 'clear'",
                "enum": ["history", "capture", "paste", "search", "pin", "pinned", "clear"],
            },
            "text": {
                "type": "STRING",
                "description": "Text content to copy/set to the clipboard, or text to pin",
            },
            "query": {
                "type": "STRING",
                "description": "Search term for clipboard history query",
            },
            "id": {
                "type": "INTEGER",
                "description": "Clipboard entry ID to copy, pin, or retrieve",
            },
            "title": {
                "type": "STRING",
                "description": "Optional title for pinned snippets",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Maximum number of history entries to return (default: 20)",
            },
        },
        "required": ["action"],
    },
    "handler": clipboard_manager,
}
