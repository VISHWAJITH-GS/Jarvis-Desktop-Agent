"""
tests/test_whatsapp_send_verification.py — Offline DOM Fixture and Contract Tests
for WhatsApp Web Sending, New-Bubble Verification, and Composing Help.

Validates Part 2 & Part 3.5:
- Exact text must appear as a NEW outgoing bubble that was not present before the action.
- Older sent bubbles never count (verdict: not_done).
- Wrong chat open stops immediately (verdict: wrong_target).
- Delivery ticks verified: sent/delivered -> verified, clock icon -> unverifiable (queued locally).
- Composing help ("I need a message asking Ramnath for...") drafts text and waits for approval.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core import confirm, outcome
from plugins import _whatsapp_web, whatsapp_automation


class TestWhatsAppSendVerification(unittest.TestCase):
    def setUp(self):
        confirm.reset_state()
        _whatsapp_web.clear_idempotency_cache()
        self.driver = _whatsapp_web.WhatsAppWebDriver()

    def tearDown(self):
        confirm.reset_state()
        _whatsapp_web.clear_idempotency_cache()

    # ── 1. New Outgoing Bubble Verification ──────────────────────────────────

    def test_new_outgoing_bubble_verified(self):
        """When a new outgoing bubble appears after send, verdict is verified."""
        mock_page = MagicMock()
        mock_page.wait_for_selector = AsyncMock()
        mock_page.keyboard = MagicMock()
        mock_page.keyboard.type = AsyncMock()
        mock_page.keyboard.press = AsyncMock()

        # Chat header matches Mom
        mock_header = MagicMock()
        mock_header.is_visible = AsyncMock(return_value=True)
        mock_header.inner_text = AsyncMock(return_value="Mom\nonline")
        mock_title = MagicMock()
        mock_title.inner_text = AsyncMock(return_value="Mom")
        mock_title.get_attribute = AsyncMock(return_value="Mom")

        def _qs(sel):
            if "chat-title" in sel:
                return mock_title
            if "conversation-header" in sel or "header" in sel:
                return mock_header
            return None

        mock_page.query_selector = AsyncMock(side_effect=_qs)
        mock_header.query_selector = AsyncMock(side_effect=_qs)

        # Baseline: 0 outgoing bubbles before send
        # After send: 1 new outgoing bubble with matching text and delivered double tick
        new_bubble = MagicMock()
        new_bubble.get_attribute = AsyncMock(return_value="msg-id-12345")
        bubble_text = MagicMock()
        bubble_text.inner_text = AsyncMock(return_value="See you for dinner!")
        dbl_tick = MagicMock()
        dbl_tick.is_visible = AsyncMock(return_value=True)

        def _b_qs(sel):
            if "text" in sel or "copyable" in sel:
                return bubble_text
            if "dblcheck" in sel or "check" in sel:
                return dbl_tick
            return None

        new_bubble.query_selector = AsyncMock(side_effect=_b_qs)

        # First call (baseline before typing): []
        # Second call (after typing/Enter): [new_bubble]
        mock_page.query_selector_all = AsyncMock(side_effect=[[], [new_bubble], [new_bubble]])

        loop = asyncio.new_event_loop()
        res = loop.run_until_complete(
            self.driver._send_message_in_page(mock_page, "Mom", "See you for dinner!")
        )
        loop.close()

        self.assertEqual(res.status, outcome.STATUS_DONE)
        self.assertTrue(res.verified)
        self.assertEqual(res.data.get("verdict"), "verified")
        self.assertEqual(res.data.get("tick"), "delivered")

    # ── 2. Old Outgoing Bubble Never Counts (verdict: not_done) ───────────────

    def test_old_outgoing_bubble_never_counts(self):
        """If an identical sent bubble was already present before sending and no NEW bubble appears, verdict is not_done."""
        mock_page = MagicMock()
        mock_page.wait_for_selector = AsyncMock()
        mock_page.keyboard = MagicMock()
        mock_page.keyboard.type = AsyncMock()
        mock_page.keyboard.press = AsyncMock()

        # Chat header matches Mom
        mock_header = MagicMock()
        mock_header.is_visible = AsyncMock(return_value=True)
        mock_header.inner_text = AsyncMock(return_value="Mom\nonline")
        mock_title = MagicMock()
        mock_title.inner_text = AsyncMock(return_value="Mom")
        mock_title.get_attribute = AsyncMock(return_value="Mom")

        def _qs(sel):
            if "chat-title" in sel:
                return mock_title
            if "conversation-header" in sel or "header" in sel:
                return mock_header
            return None

        mock_page.query_selector = AsyncMock(side_effect=_qs)
        mock_header.query_selector = AsyncMock(side_effect=_qs)

        # An OLD bubble from 2 hours ago that happens to have the exact same text
        old_bubble = MagicMock()
        old_bubble.get_attribute = AsyncMock(return_value="old-msg-id-001")
        old_text = MagicMock()
        old_text.inner_text = AsyncMock(return_value="See you for dinner!")
        old_bubble.query_selector = AsyncMock(return_value=old_text)

        # query_selector_all returns the SAME old bubble before and after (no new bubble created)
        mock_page.query_selector_all = AsyncMock(return_value=[old_bubble])

        loop = asyncio.new_event_loop()
        res = loop.run_until_complete(
            self.driver._send_message_in_page(mock_page, "Mom", "See you for dinner!")
        )
        loop.close()

        self.assertEqual(res.status, outcome.STATUS_FAILED)
        self.assertFalse(res.verified)
        self.assertEqual(res.data.get("verdict"), "not_done")
        self.assertIn("no new outgoing bubble", res.message)

    # ── 3. Wrong Chat Open (verdict: wrong_target) ───────────────────────────

    def test_wrong_chat_open_refusal(self):
        """If the open chat header is for the wrong person, refuse to send (verdict: wrong_target)."""
        mock_page = MagicMock()

        # Header indicates 'Savemom' or 'Stranger' is open, but target is 'Mom'
        mock_header = MagicMock()
        mock_header.is_visible = AsyncMock(return_value=True)
        mock_header.inner_text = AsyncMock(return_value="Savemom\nOnline")
        mock_title = MagicMock()
        mock_title.inner_text = AsyncMock(return_value="Savemom")
        mock_title.get_attribute = AsyncMock(return_value="Savemom")

        def _qs(sel):
            if "chat-title" in sel:
                return mock_title
            if "conversation-header" in sel or "header" in sel:
                return mock_header
            return None

        mock_page.query_selector = AsyncMock(side_effect=_qs)
        mock_header.query_selector = AsyncMock(side_effect=_qs)
        self.driver._resolve_contact_in_page = AsyncMock(
            return_value=outcome.result_done("resolve_contact", "Verified", verified=True)
        )

        loop = asyncio.new_event_loop()
        res = loop.run_until_complete(
            self.driver._send_message_in_page(mock_page, "Alice", "Hello Alice")
        )
        loop.close()

        self.assertEqual(res.status, outcome.STATUS_FAILED)
        self.assertFalse(res.verified)
        self.assertIn("Wrong chat open", res.message)
        self.assertEqual(res.data.get("verdict"), "wrong_target")

    # ── 4. Composing Help / Drafting Mode ────────────────────────────────────

    def test_composing_help_drafts_and_waits_for_approval(self):
        """User asking for composing help ('I need a message asking Ramnath...') drafts text and waits for confirmation."""
        with patch("core.gemini.text", return_value="Dear Ramnath, could you please share the previous year question papers? Thanks!"):
            res = whatsapp_automation.run({
                "contact": "Ramnath",
                "message": "I need a message asking Ramnath for previous year question papers",
                "action": "draft",
            })

        self.assertEqual(res.status, outcome.STATUS_NEEDS_CONFIRMATION)
        self.assertIn("drafted the following message", res.message.lower())
        self.assertIn("previous year question papers", res.message)
        self.assertTrue(res.data.get("is_draft"))


if __name__ == "__main__":
    unittest.main()
