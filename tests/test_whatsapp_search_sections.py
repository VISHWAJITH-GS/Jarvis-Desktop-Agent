"""
tests/test_whatsapp_search_sections.py — Offline DOM Fixture and Contract Tests
for WhatsApp Web Search Parsing by Section, Ranking, Disambiguation and Chat Opening.

Validates Part 3.2 & 3.3:
- Search results with a chat and a group of the same name: sections parsed, ranking correct,
  "ask" lists kinds correctly (e.g. "1) chat 'Kavin', 2) group 'Kavin' (5 members)").
- Never says "two contacts named X" when one is a chat and one is a group.
- Person requested ("contact X" / "chat with X") excludes groups.
- Group requested ("group X") excludes 1:1 chats.
- Remembers alias memory so the same ambiguity is not asked again.
- Open chat strictly verifies header name and kind; "already open" is decided strictly
  from reading the active header, never from a guess or screenshot.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core import outcome
from plugins import _whatsapp_web
from plugins._whatsapp_web import rank_and_disambiguate_entries, save_contact_alias, get_contact_alias


class TestWhatsAppSearchSections(unittest.TestCase):
    def setUp(self):
        _whatsapp_web.clear_idempotency_cache()
        self.driver = _whatsapp_web.WhatsAppWebDriver()

    def tearDown(self):
        _whatsapp_web.clear_idempotency_cache()

    # ── 1. Pure Ranking & Disambiguation Logic Tests ─────────────────────────

    def test_disambiguation_chat_and_group_same_name(self):
        """When both a 1:1 chat and a group share the same name, ask user with explicit kinds."""
        entries = [
            {
                "section": "Chats",
                "display_name": "Ram Nath TCE",
                "kind": "chat",
                "subtitle": "",
                "row_index": 0,
            },
            {
                "section": "Groups in common",
                "display_name": "Ram Nath TCE",
                "kind": "group",
                "subtitle": "14 members",
                "row_index": 1,
            },
        ]

        verdict, val, msg = rank_and_disambiguate_entries(
            entries,
            clean_name="Ram Nath TCE",
            target_name="Ram Nath TCE",
            is_group_requested=False,
            is_person_requested=False,
        )

        self.assertEqual(verdict, "ask")
        self.assertEqual(len(val), 2)
        # Must clearly identify kind: "1) chat 'Ram Nath TCE', 2) group 'Ram Nath TCE' (14 members)"
        self.assertIn("1) chat 'Ram Nath TCE'", msg)
        self.assertIn("2) group 'Ram Nath TCE' (14 members)", msg)
        # Must NEVER say "two contacts named"
        self.assertNotIn("two contacts named", msg.lower())

    def test_person_requested_excludes_group(self):
        """When user says 'contact Ram Nath' or 'chat with Ram Nath', group is filtered out."""
        entries = [
            {
                "section": "Chats",
                "display_name": "Ram Nath TCE",
                "kind": "chat",
                "subtitle": "",
                "row_index": 0,
            },
            {
                "section": "Groups in common",
                "display_name": "Ram Nath TCE",
                "kind": "group",
                "subtitle": "14 members",
                "row_index": 1,
            },
        ]

        verdict, val, msg = rank_and_disambiguate_entries(
            entries,
            clean_name="contact Ram Nath TCE",
            target_name="Ram Nath TCE",
            is_group_requested=False,
            is_person_requested=True,
        )

        self.assertEqual(verdict, "chosen")
        self.assertEqual(val["display_name"], "Ram Nath TCE")
        self.assertEqual(val["kind"], "chat")

    def test_group_requested_excludes_person(self):
        """When user says 'group Ram Nath TCE', 1:1 chat is filtered out."""
        entries = [
            {
                "section": "Chats",
                "display_name": "Ram Nath TCE",
                "kind": "chat",
                "subtitle": "",
                "row_index": 0,
            },
            {
                "section": "Groups in common",
                "display_name": "Ram Nath TCE",
                "kind": "group",
                "subtitle": "14 members",
                "row_index": 1,
            },
        ]

        verdict, val, msg = rank_and_disambiguate_entries(
            entries,
            clean_name="group Ram Nath TCE",
            target_name="Ram Nath TCE",
            is_group_requested=True,
            is_person_requested=False,
        )

        self.assertEqual(verdict, "chosen")
        self.assertEqual(val["display_name"], "Ram Nath TCE")
        self.assertEqual(val["kind"], "group")

    def test_alias_memory_disambiguates_without_asking(self):
        """When user's previous disambiguation choice was remembered in alias memory, pick it directly."""
        entries = [
            {
                "section": "Chats",
                "display_name": "Kavin",
                "kind": "chat",
                "subtitle": "",
                "row_index": 0,
            },
            {
                "section": "Groups in common",
                "display_name": "Kavin",
                "kind": "group",
                "subtitle": "5 members",
                "row_index": 1,
            },
        ]

        # Simulate user previously chose the group
        chosen_mock = entries[1]
        with patch("plugins._whatsapp_web.get_contact_alias", return_value={"display_name": "Kavin", "kind": "group"}):
            verdict, val, msg = rank_and_disambiguate_entries(
                entries,
                clean_name="Kavin",
                target_name="Kavin",
                is_group_requested=False,
                is_person_requested=False,
            )

        self.assertEqual(verdict, "chosen")
        self.assertEqual(val["kind"], "group")

    # ── 2. Full Page Resolve & Opening Tests ─────────────────────────────────

    def test_already_open_decided_strictly_from_header(self):
        """If active chat header already matches target name and kind, return done without searching."""
        mock_page = MagicMock()
        mock_page.wait_for_selector = AsyncMock()
        mock_page.query_selector = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=[])

        # Header indicates 'Ram Nath TCE' (1:1 chat) is currently open
        mock_header = MagicMock()
        mock_header.is_visible = AsyncMock(return_value=True)
        mock_header.inner_text = AsyncMock(return_value="Ram Nath TCE\nclick here for contact info")

        mock_title = MagicMock()
        mock_title.inner_text = AsyncMock(return_value="Ram Nath TCE")
        mock_title.get_attribute = AsyncMock(return_value="Ram Nath TCE")

        mock_subtitle = MagicMock()
        mock_subtitle.inner_text = AsyncMock(return_value="click here for contact info")
        mock_subtitle.get_attribute = AsyncMock(return_value="click here for contact info")

        def _qs(sel):
            if "chat-title" in sel:
                return mock_title
            if "chat-subtitle" in sel:
                return mock_subtitle
            if "conversation-header" in sel or "header" in sel:
                return mock_header
            return None

        mock_page.query_selector.side_effect = _qs
        mock_header.query_selector.side_effect = _qs

        loop = asyncio.new_event_loop()
        res = loop.run_until_complete(self.driver._resolve_contact_in_page(mock_page, "Ram Nath TCE"))
        loop.close()

        self.assertEqual(res.status, outcome.STATUS_DONE)
        self.assertTrue(res.data.get("already_open"))
        self.assertEqual(res.data.get("method"), "header_already_open")
        # Search input must never have been touched
        mock_page.wait_for_selector.assert_not_called()

    def test_already_open_rejected_when_kind_mismatches(self):
        """If group is currently open but user requested 'contact X', do NOT claim already open."""
        mock_page = MagicMock()
        mock_page.wait_for_selector = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=[])

        # Header indicates a group named 'Kavin' is open
        mock_header = MagicMock()
        mock_header.is_visible = AsyncMock(return_value=True)
        mock_header.inner_text = AsyncMock(return_value="Kavin\n5 members")

        mock_title = MagicMock()
        mock_title.inner_text = AsyncMock(return_value="Kavin")
        mock_title.get_attribute = AsyncMock(return_value="Kavin")

        mock_subtitle = MagicMock()
        mock_subtitle.inner_text = AsyncMock(return_value="5 members")
        mock_subtitle.get_attribute = AsyncMock(return_value="5 members")

        def _qs(sel):
            if "chat-title" in sel:
                return mock_title
            if "chat-subtitle" in sel:
                return mock_subtitle
            if "conversation-header" in sel or "header" in sel:
                return mock_header
            return None

        mock_page.query_selector.side_effect = _qs
        mock_header.query_selector.side_effect = _qs

        # Search box mock
        mock_search = MagicMock()
        mock_search.click = AsyncMock()
        mock_page.wait_for_selector.return_value = mock_search
        mock_page.keyboard = MagicMock()
        mock_page.keyboard.press = AsyncMock()
        mock_page.keyboard.type = AsyncMock()
        mock_page.query_selector_all = AsyncMock(return_value=[])

        loop = asyncio.new_event_loop()
        # Requesting contact Kavin
        res = loop.run_until_complete(self.driver._resolve_contact_in_page(mock_page, "contact Kavin"))
        loop.close()

        # Did not claim already open; proceeded to search and failed because search items are empty
        self.assertNotEqual(res.data.get("method"), "header_already_open")
        self.assertFalse(res.data.get("already_open", False))
        mock_search.click.assert_called_once()


if __name__ == "__main__":
    unittest.main()
