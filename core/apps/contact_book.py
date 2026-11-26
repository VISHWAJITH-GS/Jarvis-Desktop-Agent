"""
core/apps/contact_book.py — Centralized Contact Book with Fuzzy Disambiguation & Alias Resolution.

Loads from config/contacts.json.
Provides:
- Exact & alias match
- Token and sequence similarity matching via difflib (e.g. "Yashwanth" -> "Yaswanth TCE")
- Disambiguation when multiple candidates match closely
"""

from __future__ import annotations

import difflib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("contact_book")

_DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "contacts.json"


class ContactBook:
    def __init__(self, contacts_path: Optional[Path | str] = None):
        self.path = Path(contacts_path) if contacts_path else _DEFAULT_PATH
        self._contacts: List[Dict[str, Any]] = []
        self.load()

    def load(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            self._contacts = []
            return self._contacts
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, list):
                self._contacts = data
            elif isinstance(data, dict):
                self._contacts = data.get("contacts", [])
        except Exception as e:
            logger.warning("[ContactBook] Failed to load %s: %s", self.path, e)
            self._contacts = []
        return self._contacts

    def save(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._contacts, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            logger.error("[ContactBook] Failed to save %s: %s", self.path, e)
            return False

    def add_contact(
        self,
        name: str,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        aliases: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        entry = {
            "name": name.strip(),
            "phone": str(phone or "").strip(),
            "email": str(email or "").strip(),
            "aliases": [a.strip() for a in (aliases or []) if a.strip()],
        }
        # Update if name exists
        for idx, c in enumerate(self._contacts):
            if c.get("name", "").lower() == entry["name"].lower():
                self._contacts[idx] = entry
                self.save()
                return entry
        self._contacts.append(entry)
        self.save()
        return entry

    def resolve_contact(self, query: str) -> Tuple[str, Any, Optional[str]]:
        """
        Resolves query against contacts list.
        Returns:
            ("match", contact_dict, None)
            ("ambiguous", [candidate_dicts], prompt_message)
            ("not_found", None, error_message)
        """
        q = (query or "").strip().lower()
        if not q:
            return "not_found", None, "No contact name or query specified."

        self.load()
        if not self._contacts:
            return "not_found", None, f"No contacts found in {self.path.name}."

        # 1. Exact match on name or alias
        for c in self._contacts:
            name = str(c.get("name", "")).strip().lower()
            if name == q:
                return "match", c, None
            for alias in c.get("aliases", []):
                if str(alias).strip().lower() == q:
                    return "match", c, None

        # 2. Substring match
        partial_matches = []
        for c in self._contacts:
            name = str(c.get("name", "")).strip().lower()
            aliases = [str(a).strip().lower() for a in c.get("aliases", [])]
            if q in name or name in q or any(q in a or a in q for a in aliases):
                partial_matches.append(c)

        if len(partial_matches) == 1:
            return "match", partial_matches[0], None
        elif len(partial_matches) > 1:
            names = [c.get("name") for c in partial_matches]
            prompt = f"Found multiple contacts matching '{query}': {', '.join(names)}. Which one did you mean?"
            return "ambiguous", partial_matches, prompt

        # 3. Fuzzy ratio match (SequenceMatcher across name and tokens)
        scored = []
        for c in self._contacts:
            name = str(c.get("name", "")).strip().lower()
            aliases = [str(a).strip().lower() for a in c.get("aliases", [])]
            pool_names = [name] + aliases

            scores = []
            for candidate in pool_names:
                # Direct ratio
                ratio = difflib.SequenceMatcher(None, q, candidate).ratio()
                scores.append(ratio)
                # Token ratio
                q_tokens = q.split()
                cand_tokens = candidate.split()
                for qt in q_tokens:
                    for ct in cand_tokens:
                        scores.append(difflib.SequenceMatcher(None, qt, ct).ratio())

            max_score = max(scores, default=0.0)
            if max_score >= 0.70:
                scored.append((max_score, c))

        if not scored:
            return "not_found", None, f"Contact '{query}' not found."

        scored.sort(key=lambda x: x[0], reverse=True)
        top_score = scored[0][0]

        # Single clear winner (>= 0.75 and >= 0.10 higher than 2nd, or only 1)
        if len(scored) == 1 or (top_score >= 0.75 and (top_score - scored[1][0] >= 0.10)):
            return "match", scored[0][1], None

        # Multiple close candidates
        close_candidates = [c for s, c in scored if (top_score - s) <= 0.12]
        names = [c.get("name") for c in close_candidates]
        prompt = f"Did you mean {', or '.join(names)} for '{query}'?"
        return "ambiguous", close_candidates, prompt


_contact_book: Optional[ContactBook] = None


def get_contact_book() -> ContactBook:
    global _contact_book
    if _contact_book is None:
        _contact_book = ContactBook()
    return _contact_book
