"""
plugins/_whatsapp_core.py — Backward-compatibility bridge for WhatsApp Web driver.

Desktop WhatsApp automation has been fully migrated to WhatsApp Web over CDP
running inside the dedicated Jarvis Chrome profile.
This module preserves the legacy interface for tests and external callers
with zero desktop application dependencies.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from plugins import _whatsapp_web


class WhatsAppDriver:
    """Compatibility bridge routing legacy calls to WhatsAppWebDriver."""

    def __init__(self):
        self._web = _whatsapp_web.get_driver()

    def open_whatsapp(self) -> bool:
        state, page, _ = self._web.ensure_page_ready(background=False)
        return state in ("ready", "done")

    def resolve_contact(self, contact_name: str) -> Tuple[bool, str, List[str]]:
        res = self._web.resolve_contact(contact_name)
        if res.is_success():
            contact = res.data.get("contact", contact_name)
            return True, contact, [contact]
        elif res.data.get("candidates") or res.status in ("ask", "needs_confirmation"):
            candidates = res.data.get("candidates", [])
            return False, res.message, candidates
        return False, res.error or res.message, []

    def open_chat(self, contact_name: str) -> Tuple[bool, str]:
        ok, title, _ = self.resolve_contact(contact_name)
        return ok, title

    def send_message_to(self, contact_name: str, message: str) -> Tuple[bool, str]:
        return self._web.send_message_to(contact_name, message)

    def read_chat_messages(
        self, contact_name: Optional[str] = None, limit: int = 5
    ) -> Tuple[bool, List[Dict[str, str]], str]:
        return self._web.read_chat_messages(contact_name=contact_name, limit=limit)

    def read_latest_incoming_messages(
        self, limit: int = 5
    ) -> Tuple[bool, List[Dict[str, str]], str]:
        return self._web.read_latest_incoming_messages(limit=limit)

    def summarize_conversation(self, contact_name: str) -> Tuple[bool, str]:
        return self._web.summarize_conversation(contact_name)


def get() -> Tuple[WhatsAppDriver, str]:
    """Return compatibility driver and status reason."""
    return WhatsAppDriver(), ""
