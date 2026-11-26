"""
tests/test_whatsapp_gate.py — Comprehensive Unit Tests for the Send-Time Confirmation Gate on WhatsApp Web.

Verifies:
1. Typing in search (allowed -> is_whatsapp_send_action is False)
2. Typing in composer without enter (allowed -> is_whatsapp_send_action is False)
3. Clicking a chat / contact / search result row (allowed -> is_whatsapp_send_action is False)
4. Clicking Send button (gated -> is_whatsapp_send_action is True)
5. Enter in composer (gated -> is_whatsapp_send_action is True)
6. Type with press_enter=True into composer (gated -> is_whatsapp_send_action is True)
7. Shift+Enter in composer (allowed, newline -> is_whatsapp_send_action is False)
8. Non-WhatsApp sites (unchanged behavior -> is_whatsapp_send_action is False)
9. Truthful confirmation prompt with matching recipient
10. Truthful confirmation prompt warning on mismatched open chat
11. Fuzzy contact lookup handling spelling variants (Yashwanth vs Yaswanth TCE)
12. Honest delivery verification (new outgoing bubble confirmed vs unverified)
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from actions.whatsapp_gate import (
    format_confirmation_prompt,
    get_active_chat_contact,
    get_active_composer_text,
    is_whatsapp_send_action,
    verify_whatsapp_send_async,
)
from core import outcome
from plugins import _whatsapp_web


class TestWhatsAppGate(unittest.TestCase):
    def setUp(self):
        self.wa_url = "https://web.whatsapp.com"
        self.other_url = "https://google.com"

        # Mock page_states
        self.state_search_focused = {
            "url": self.wa_url,
            "focused_element": {
                "role": "textbox",
                "placeholder": "Search or start a new chat",
                "aria_label": "Search or start a new chat",
                "data_testid": "chat-list-search",
            },
            "is_composer_focused": False,
        }

        self.state_composer_focused = {
            "url": self.wa_url,
            "focused_element": {
                "role": "textbox",
                "placeholder": "Type a message",
                "aria_label": "Type a message",
                "data_testid": "conversation-compose-box-input",
            },
            "is_composer_focused": True,
        }

        self.state_non_whatsapp = {
            "url": self.other_url,
            "is_composer_focused": True,
        }

    # ── 1. Typing in search (allowed) ─────────────────────────────────────────

    def test_typing_in_search_is_allowed(self):
        """Typing contact query into search box must never be gated."""
        # By text/selector
        res = is_whatsapp_send_action(
            "type",
            {"text": "Yaswanth", "selector": "Search or start a new chat"},
            page_state=self.state_search_focused,
        )
        self.assertFalse(res, "Typing into contact search box must be allowed freely.")

        # With search box focused
        res2 = is_whatsapp_send_action(
            "type",
            {"text": "Mom"},
            page_state=self.state_search_focused,
        )
        self.assertFalse(res2, "Typing with search box focused must be allowed freely.")

    # ── 2. Typing in composer without enter (allowed) ─────────────────────────

    def test_typing_in_composer_without_enter_is_allowed(self):
        """Typing or drafting a message in the composer without pressing Enter is allowed freely."""
        res = is_whatsapp_send_action(
            "type",
            {"text": "Hello, see you at 6!", "press_enter": False},
            page_state=self.state_composer_focused,
        )
        self.assertFalse(res, "Drafting text in composer without Enter must be allowed.")

        # Default press_enter is False
        res2 = is_whatsapp_send_action(
            "input",
            {"text": "Meeting notes draft"},
            page_state=self.state_composer_focused,
        )
        self.assertFalse(res2, "Default input into composer must not trigger send gate.")

    # ── 3. Clicking a chat or contact row (allowed) ───────────────────────────

    def test_clicking_chat_row_is_allowed(self):
        """Clicking on a contact, group, or search result row must be allowed freely."""
        res1 = is_whatsapp_send_action(
            "click",
            {"text": "Yaswanth TCE"},
            page_state=self.state_search_focused,
        )
        self.assertFalse(res1, "Clicking contact row must be allowed.")

        res2 = is_whatsapp_send_action(
            "click",
            {"text": "Mom", "index": 5},
            page_state={
                "url": self.wa_url,
                "elements": {5: {"role": "row", "name": "Mom", "text": "Mom"}},
            },
        )
        self.assertFalse(res2, "Clicking chat row by index must be allowed.")

    # ── 4. Clicking Send button (gated) ───────────────────────────────────────

    def test_clicking_send_button_is_gated(self):
        """Clicking the Send button must trigger the send-time gate."""
        # By text
        self.assertTrue(
            is_whatsapp_send_action("click", {"text": "Send"}, page_state=self.state_composer_focused)
        )
        self.assertTrue(
            is_whatsapp_send_action("smart_click", {"description": "send button"}, page_state=self.state_composer_focused)
        )
        # Multilingual send buttons
        self.assertTrue(
            is_whatsapp_send_action("click", {"text": "Enviar"}, page_state=self.state_composer_focused)
        )
        self.assertTrue(
            is_whatsapp_send_action("click", {"text": "Bhejo"}, page_state=self.state_composer_focused)
        )

        # By element index with send icon / testid
        state_with_send_elem = {
            "url": self.wa_url,
            "elements": {
                12: {"role": "button", "data_icon": "send", "aria_label": "Send"},
            },
        }
        self.assertTrue(
            is_whatsapp_send_action("click", {"index": 12}, page_state=state_with_send_elem)
        )

    # ── 5. Enter in composer (gated) ──────────────────────────────────────────

    def test_enter_in_composer_is_gated(self):
        """Pressing Enter while the message composer is focused must trigger the gate."""
        self.assertTrue(
            is_whatsapp_send_action("press", {"key": "Enter"}, page_state=self.state_composer_focused)
        )
        self.assertTrue(
            is_whatsapp_send_action("press_key", {"key": "Return"}, page_state=self.state_composer_focused)
        )

        # Enter in search box must NOT be gated
        self.assertFalse(
            is_whatsapp_send_action("press", {"key": "Enter"}, page_state=self.state_search_focused),
            "Pressing Enter in search box to select contact must not be gated as send.",
        )

    # ── 6. Type with press_enter=True (gated) ─────────────────────────────────

    def test_type_with_press_enter_in_composer_is_gated(self):
        """Typing with press_enter=True targeting the composer must trigger the gate."""
        self.assertTrue(
            is_whatsapp_send_action(
                "type",
                {"text": "Final message", "press_enter": True},
                page_state=self.state_composer_focused,
            )
        )

        # Type with press_enter in search box is NOT send
        self.assertFalse(
            is_whatsapp_send_action(
                "type",
                {"text": "Rahul", "press_enter": True, "selector": "Search or start a new chat"},
                page_state=self.state_search_focused,
            )
        )

    # ── 7. Shift+Enter in composer (allowed, it's a newline) ───────────────────

    def test_shift_enter_in_composer_is_allowed(self):
        """Pressing Shift+Enter in the composer inserts a newline and must NOT trigger send gate."""
        self.assertFalse(
            is_whatsapp_send_action(
                "press",
                {"key": "Shift+Enter"},
                page_state=self.state_composer_focused,
            ),
            "Shift+Enter must be allowed as a newline.",
        )
        self.assertFalse(
            is_whatsapp_send_action(
                "press",
                {"key": "Enter", "shift": True},
                page_state=self.state_composer_focused,
            )
        )
        self.assertFalse(
            is_whatsapp_send_action(
                "press",
                {"key": "Enter", "modifiers": "Shift"},
                page_state=self.state_composer_focused,
            )
        )

    # ── 8. Non-WhatsApp sites (unchanged behavior) ────────────────────────────

    def test_non_whatsapp_sites_unchanged(self):
        """Actions on non-WhatsApp domains (e.g. Google, documentation) are not gated by WhatsApp gate."""
        self.assertFalse(
            is_whatsapp_send_action("click", {"text": "Send"}, page_state=self.state_non_whatsapp)
        )
        self.assertFalse(
            is_whatsapp_send_action("press", {"key": "Enter"}, page_state=self.state_non_whatsapp)
        )
        self.assertFalse(
            is_whatsapp_send_action(
                "type", {"text": "Query", "press_enter": True}, page_state=self.state_non_whatsapp
            )
        )

    # ── 9. Truthful Confirmation Prompt ───────────────────────────────────────

    def test_truthful_confirmation_prompt_format(self):
        """Prompt format must strictly be 'Send to <contact>: '<message text>'? (yes/no)'."""
        prompt = format_confirmation_prompt("Yaswanth TCE", "Are we meeting today?")
        self.assertEqual(prompt, "Send to Yaswanth TCE: 'Are we meeting today?'? (yes/no)")

    # ── 10. Warning on Mismatched Open Chat ───────────────────────────────────

    def test_warning_when_open_chat_mismatches_intended_contact(self):
        """If active chat differs from intended contact, prompt must contain a clear warning."""
        prompt = format_confirmation_prompt(
            open_contact="Stranger",
            composer_text="Secret token",
            intended_contact="Mom",
        )
        self.assertIn("Warning", prompt)
        self.assertIn("does not match intended recipient 'Mom'", prompt)
        self.assertIn("Send to Stranger: 'Secret token'?", prompt)

    # ── 11. Fuzzy Contact Lookup for Spelling Variants ────────────────────────

    def test_fuzzy_contact_lookup_spelling_variants(self):
        """Spelling variant 'Yashwanth' matches 'Yaswanth TCE' via fuzzy ratio."""
        entries = [
            {"display_name": "Yaswanth TCE", "kind": "chat", "row_index": 0},
            {"display_name": "College Group", "kind": "group", "row_index": 1},
        ]
        verdict, chosen, msg = _whatsapp_web.rank_and_disambiguate_entries(
            entries,
            clean_name="Yashwanth",
            target_name="Yashwanth",
        )
        self.assertEqual(verdict, "chosen")
        self.assertEqual(chosen["display_name"], "Yaswanth TCE")

    def test_ambiguous_fuzzy_contact_lookup_asks_user(self):
        """Multiple close spelling variants trigger an interactive disambiguation question."""
        entries = [
            {"display_name": "Yaswanth Kumar", "kind": "chat", "row_index": 0},
            {"display_name": "Yashwant Singh", "kind": "chat", "row_index": 1},
        ]
        verdict, candidates, msg = _whatsapp_web.rank_and_disambiguate_entries(
            entries,
            clean_name="Yashwanth",
            target_name="Yashwanth",
        )
        self.assertEqual(verdict, "ask")
        self.assertGreaterEqual(len(candidates), 2)
        self.assertIn("Which one would you like to open?", msg)

    # ── 12. Honest Delivery Verification ──────────────────────────────────────

    def test_verify_whatsapp_send_verifies_outgoing_bubble(self):
        """verify_whatsapp_send_async verifies when a new outgoing bubble with delivered tick appears."""
        import asyncio

        mock_page = MagicMock()
        mock_page.is_closed.return_value = False

        new_bubble = MagicMock()
        new_bubble.get_attribute = AsyncMock(return_value="new-id-999")
        text_el = MagicMock()
        text_el.inner_text = AsyncMock(return_value="See you soon!")
        new_bubble.query_selector = AsyncMock(side_effect=lambda sel: text_el if "text" in sel else None)

        dbl_tick = MagicMock()
        dbl_tick.is_visible = AsyncMock(return_value=True)

        def _qs(sel):
            if "text" in sel:
                return text_el
            if "dblcheck" in sel or "check" in sel:
                return dbl_tick
            return None

        new_bubble.query_selector = AsyncMock(side_effect=_qs)
        mock_page.query_selector_all = AsyncMock(return_value=[new_bubble])

        loop = asyncio.new_event_loop()
        ok, msg, data = loop.run_until_complete(
            verify_whatsapp_send_async(mock_page, "See you soon!", pre_ids=set())
        )
        loop.close()

        self.assertTrue(ok)
        self.assertTrue(data.get("verified"))
        self.assertEqual(data.get("tick"), "delivered")
        self.assertIn("See you soon!", msg)

    def test_verify_whatsapp_send_reports_unverified_on_clock_icon(self):
        """Clock icon (pending local queue) must be reported honestly as unverified, never success."""
        import asyncio

        mock_page = MagicMock()
        mock_page.is_closed.return_value = False

        pending_bubble = MagicMock()
        pending_bubble.get_attribute = AsyncMock(return_value="pending-id-1")
        text_el = MagicMock()
        text_el.inner_text = AsyncMock(return_value="Are you there?")

        clock_icon = MagicMock()
        clock_icon.is_visible = AsyncMock(return_value=True)

        def _qs(sel):
            if "text" in sel:
                return text_el
            if "status-time" in sel:
                return clock_icon
            return None

        pending_bubble.query_selector = AsyncMock(side_effect=_qs)
        mock_page.query_selector_all = AsyncMock(return_value=[pending_bubble])

        loop = asyncio.new_event_loop()
        ok, msg, data = loop.run_until_complete(
            verify_whatsapp_send_async(mock_page, "Are you there?", pre_ids=set())
        )
        loop.close()

        self.assertFalse(ok)
        self.assertFalse(data.get("verified"))
        self.assertEqual(data.get("verdict"), "unverifiable")
        self.assertEqual(data.get("tick"), "pending")
        self.assertIn("clock icon", msg)


if __name__ == "__main__":
    unittest.main()
