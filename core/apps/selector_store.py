"""
core/apps/selector_store.py — Ordered Resilient Selector Candidate Store with Memory.

Features:
- Stores ordered list of candidate selectors (role, aria-label, text, CSS) per app element.
- Remembers which candidate worked last and promotes it to the front of candidate list.
- Persists state to config/selectors.json.
- Self-healing when website layouts change.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("selector_store")

_DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "selectors.json"


class SelectorStore:
    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else _DEFAULT_PATH
        self._data: Dict[str, Dict[str, List[str]]] = {}
        self.load()

    def load(self) -> Dict[str, Dict[str, List[str]]]:
        if not self.path.exists():
            self._data = {}
            return self._data
        try:
            raw = self.path.read_text(encoding="utf-8")
            self._data = json.loads(raw)
        except Exception as e:
            logger.warning("[SelectorStore] Failed to load %s: %s", self.path, e)
            self._data = {}
        return self._data

    def save(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            logger.error("[SelectorStore] Failed to save %s: %s", self.path, e)
            return False

    def get_candidates(self, app: str, element_key: str, default: Optional[List[str]] = None) -> List[str]:
        """Get ordered list of candidate selectors for an app element, prioritizing the last working one."""
        app_clean = (app or "").strip().lower()
        key_clean = (element_key or "").strip().lower()
        candidates = self._data.get(app_clean, {}).get(key_clean, [])
        if not candidates and default:
            candidates = list(default)
            if app_clean not in self._data:
                self._data[app_clean] = {}
            self._data[app_clean][key_clean] = candidates
            self.save()
        return list(candidates)

    def record_success(self, app: str, element_key: str, working_selector: str) -> None:
        """Promote working selector to the first position in candidate list."""
        app_clean = (app or "").strip().lower()
        key_clean = (element_key or "").strip().lower()
        sel = working_selector.strip()
        if not sel:
            return

        if app_clean not in self._data:
            self._data[app_clean] = {}
        current = self._data[app_clean].get(key_clean, [])

        if sel in current:
            current.remove(sel)
        current.insert(0, sel)
        self._data[app_clean][key_clean] = current
        self.save()
        logger.debug("[SelectorStore] Promoted working selector '%s' for %s.%s", sel, app_clean, key_clean)

    def add_selector(self, app: str, element_key: str, selector: str) -> None:
        """Add a new candidate selector to store."""
        app_clean = (app or "").strip().lower()
        key_clean = (element_key or "").strip().lower()
        sel = selector.strip()
        if not sel:
            return

        if app_clean not in self._data:
            self._data[app_clean] = {}
        current = self._data[app_clean].get(key_clean, [])
        if sel not in current:
            current.append(sel)
            self._data[app_clean][key_clean] = current
            self.save()


_selector_store: Optional[SelectorStore] = None


def get_selector_store() -> SelectorStore:
    global _selector_store
    if _selector_store is None:
        _selector_store = SelectorStore()
    return _selector_store
