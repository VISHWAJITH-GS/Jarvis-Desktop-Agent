"""
gui/display_registry.py — Authoritative Registry & Lifecycle Manager for Jarvis-Created Displays.

Tracks every display window Jarvis creates:
  (id, title, kind, qt_window_ref, created_at, has_unsaved_content, is_minimized)
Enforces:
  1. Strict Isolation: Commands refer strictly to Jarvis's own display windows;
     it must NEVER touch other applications' windows.
  2. Disambiguation & Fuzzy Matching:
     - Match exact title -> fuzzy title -> numeric id.
     - "Close the display" closes the display if only 1 is open; asks if multiple exist.
     - Ambiguity returns status "ask" with the list of candidates.
  3. Safety: Closing displays with unsaved content prompts for confirmation.
"""

from __future__ import annotations

import difflib
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union


class DisplayEntry:
    """Metadata record for a Jarvis-created display window."""

    def __init__(
        self,
        id: int,
        title: str,
        kind: str = "image",
        qt_window_ref: Any = None,
        has_unsaved_content: bool = False,
        is_minimized: bool = False,
        created_at: Optional[float] = None,
    ):
        self.id = int(id)
        self.title = str(title or f"Display {id}").strip()
        self.kind = str(kind or "image").strip().lower()
        self.qt_window_ref = qt_window_ref
        self.has_unsaved_content = bool(has_unsaved_content)
        self.is_minimized = bool(is_minimized)
        self.created_at = created_at if created_at is not None else time.monotonic()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "name": f"Display {self.id}",
            "kind": self.kind,
            "minimized": self.is_minimized,
            "has_unsaved_content": self.has_unsaved_content,
            "created_at": self.created_at,
        }

    def __repr__(self) -> str:
        return f"<DisplayEntry id={self.id} title='{self.title}' kind='{self.kind}' min={self.is_minimized}>"


class DisplayRegistry:
    """Thread-safe registry for Jarvis-created display windows."""

    _lock = threading.RLock()
    _displays: Dict[int, DisplayEntry] = {}

    @classmethod
    def reset(cls) -> None:
        """Reset registry state (primarily for tests)."""
        with cls._lock:
            cls._displays.clear()

    @classmethod
    def register(
        cls,
        id: int,
        title: str,
        kind: str = "image",
        qt_window_ref: Any = None,
        has_unsaved_content: bool = False,
        is_minimized: bool = False,
    ) -> DisplayEntry:
        """Register a newly created Jarvis display window."""
        with cls._lock:
            entry = DisplayEntry(
                id=id,
                title=title,
                kind=kind,
                qt_window_ref=qt_window_ref,
                has_unsaved_content=has_unsaved_content,
                is_minimized=is_minimized,
            )
            cls._displays[int(id)] = entry
            return entry

    @classmethod
    def unregister(cls, id: int) -> Optional[DisplayEntry]:
        """Remove a display window from the registry on close."""
        with cls._lock:
            return cls._displays.pop(int(id), None)

    @classmethod
    def update_state(
        cls,
        id: int,
        is_minimized: Optional[bool] = None,
        has_unsaved_content: Optional[bool] = None,
    ) -> Optional[DisplayEntry]:
        """Update window state flags."""
        with cls._lock:
            entry = cls._displays.get(int(id))
            if entry:
                if is_minimized is not None:
                    entry.is_minimized = bool(is_minimized)
                if has_unsaved_content is not None:
                    entry.has_unsaved_content = bool(has_unsaved_content)
            return entry

    @classmethod
    def get(cls, id: int) -> Optional[DisplayEntry]:
        with cls._lock:
            return cls._displays.get(int(id))

    @classmethod
    def list_displays(cls) -> List[Dict[str, Any]]:
        """Return list of active Jarvis displays."""
        with cls._lock:
            return [d.to_dict() for d in sorted(cls._displays.values(), key=lambda x: x.id)]

    @classmethod
    def resolve_target(cls, target: Any) -> Tuple[str, Optional[DisplayEntry], List[DisplayEntry]]:
        """
        Resolve a display target by:
          1. Exact numeric ID or 'display X'
          2. Exact title (case-insensitive)
          3. Fuzzy title match (SequenceMatcher > 0.6 or substring)
          4. Single display fallback when target is generic ('the display', '', 'display')

        Returns:
          (status, matched_entry, candidates_list)
          status is one of:
            - 'exact': exactly one unambiguous match found
            - 'none': no matches found
            - 'ask': ambiguous match among multiple candidates
        """
        with cls._lock:
            displays = list(cls._displays.values())

        if not displays:
            return "none", None, []

        query = str(target or "").strip().lower()

        # 0. Check generic query: "the display", "display", "window", ""
        is_generic = query in ("", "the display", "display", "window", "the window", "this display", "it")

        if is_generic:
            if len(displays) == 1:
                return "exact", displays[0], displays
            return "ask", None, displays

        # 1. Numeric ID match
        # Check if query is just an integer
        if isinstance(target, int):
            with cls._lock:
                entry = cls._displays.get(target)
                if entry:
                    return "exact", entry, [entry]
            return "none", None, []

        id_match = re.search(r"\b(?:display\s*)?(\d+)\b", query)
        if id_match:
            cand_id = int(id_match.group(1))
            with cls._lock:
                entry = cls._displays.get(cand_id)
                if entry:
                    return "exact", entry, [entry]

        # 2. Exact title / name match (case-insensitive)
        exact_matches = [
            d for d in displays
            if d.title.lower() == query or f"display {d.id}".lower() == query
        ]
        if len(exact_matches) == 1:
            return "exact", exact_matches[0], exact_matches
        elif len(exact_matches) > 1:
            return "ask", None, exact_matches

        # 3. Substring match
        substr_matches = [
            d for d in displays
            if query in d.title.lower() or query in f"display {d.id}".lower()
        ]
        if len(substr_matches) == 1:
            return "exact", substr_matches[0], substr_matches
        elif len(substr_matches) > 1:
            return "ask", None, substr_matches

        # 4. Fuzzy SequenceMatcher
        scored = []
        for d in displays:
            ratio = difflib.SequenceMatcher(None, query, d.title.lower()).ratio()
            # Also check against kind or display name
            ratio = max(ratio, difflib.SequenceMatcher(None, query, f"display {d.id}").ratio())
            if ratio >= 0.55:
                scored.append((ratio, d))

        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            top_ratio, top_d = scored[0]
            # If top match is substantially better than second match (> 0.2 gap)
            if len(scored) == 1 or (top_ratio - scored[1][0] >= 0.2 and top_ratio >= 0.7):
                return "exact", top_d, [top_d]
            candidates = [s[1] for s in scored if s[0] >= 0.55]
            return "ask", None, candidates

        return "none", None, []

    # ── Lifecycle Actions ─────────────────────────────────────────────────────

    @classmethod
    def minimize_display(cls, name_or_id: Any, player=None) -> str:
        """Minimize a Jarvis display window to the dock."""
        status, entry, candidates = cls.resolve_target(name_or_id)
        if status == "none":
            return f"Sir, I could not find a display matching '{name_or_id}'."
        if status == "ask":
            options = ", ".join(f"{i+1}) Display {c.id} '{c.title}' ({c.kind})" for i, c in enumerate(candidates))
            return f"Which display would you like to minimize? {options}."

        entry.is_minimized = True
        if player is not None:
            if hasattr(player, "minimize_display_window"):
                player.minimize_display_window(entry.id)
            elif hasattr(player, "_win") and hasattr(player._win, "_minimize_display_win"):
                player._win._minimize_display_sig.emit(entry.id)
        return f"Minimized Display {entry.id} ('{entry.title}')."

    @classmethod
    def restore_display(cls, name_or_id: Any, player=None) -> str:
        """Restore / focus a Jarvis display window."""
        status, entry, candidates = cls.resolve_target(name_or_id)
        if status == "none":
            return f"Sir, I could not find a display matching '{name_or_id}'."
        if status == "ask":
            options = ", ".join(f"{i+1}) Display {c.id} '{c.title}' ({c.kind})" for i, c in enumerate(candidates))
            return f"Which display would you like to restore? {options}."

        entry.is_minimized = False
        if player is not None:
            if hasattr(player, "focus_display"):
                player.focus_display(entry.id)
            elif hasattr(player, "_win") and hasattr(player._win, "_focus_display_sig"):
                player._win._focus_display_sig.emit(entry.id)
        return f"Restored Display {entry.id} ('{entry.title}')."

    @classmethod
    def close_display(cls, name_or_id: Any, player=None, force: bool = False) -> str:
        """Close a specific Jarvis display window cleanly."""
        status, entry, candidates = cls.resolve_target(name_or_id)
        if status == "none":
            return f"Sir, I could not find a display matching '{name_or_id}'."
        if status == "ask":
            options = ", ".join(f"{i+1}) Display {c.id} '{c.title}' ({c.kind})" for i, c in enumerate(candidates))
            return f"Which display would you like to close? {options}."

        # Safety check: if display has unsaved content and not force
        if entry.has_unsaved_content and not force:
            return (
                f"[CONFIRMATION_PENDING] Display {entry.id} ('{entry.title}') has unsaved content. "
                f"Are you sure you want to close it? Say confirm to proceed, or no to cancel."
            )

        cls.unregister(entry.id)
        if player is not None:
            if hasattr(player, "close_display_window"):
                player.close_display_window(entry.id)
            elif hasattr(player, "_win") and hasattr(player._win, "_close_display_sig"):
                player._win._close_display_sig.emit(entry.id)
        return f"Closed Display {entry.id} ('{entry.title}')."

    @classmethod
    def close_all_displays(cls, player=None) -> str:
        """Close all Jarvis-created display windows; never touches external apps."""
        with cls._lock:
            all_entries = list(cls._displays.values())
            cls._displays.clear()

        if not all_entries:
            return "Sir, there are no active display windows to close."

        count = len(all_entries)
        if player is not None:
            if hasattr(player, "close_display_window"):
                player.close_display_window(0)
            elif hasattr(player, "_win") and hasattr(player._win, "_close_display_sig"):
                player._win._close_display_sig.emit(0)

        return f"Closed all {count} display window{'s' if count != 1 else ''}."


# Module-level convenience functions
def register_display(id: int, title: str, kind: str = "image", qt_window_ref=None, has_unsaved_content: bool = False) -> DisplayEntry:
    return DisplayRegistry.register(id, title, kind, qt_window_ref, has_unsaved_content)


def unregister_display(id: int) -> Optional[DisplayEntry]:
    return DisplayRegistry.unregister(id)


def list_displays() -> List[Dict[str, Any]]:
    return DisplayRegistry.list_displays()


def resolve_display(target: Any) -> Tuple[str, Optional[DisplayEntry], List[DisplayEntry]]:
    return DisplayRegistry.resolve_target(target)


def minimize_display(name_or_id: Any, player=None) -> str:
    return DisplayRegistry.minimize_display(name_or_id, player=player)


def restore_display(name_or_id: Any, player=None) -> str:
    return DisplayRegistry.restore_display(name_or_id, player=player)


def close_display(name_or_id: Any, player=None, force: bool = False) -> str:
    return DisplayRegistry.close_display(name_or_id, player=player, force=force)


def close_all_displays(player=None) -> str:
    return DisplayRegistry.close_all_displays(player=player)
