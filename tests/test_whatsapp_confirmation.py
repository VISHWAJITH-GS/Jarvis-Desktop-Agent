"""
tests/test_whatsapp_confirmation.py — Comprehensive tests for WhatsApp confirmation gate:
1. confirm once -> sent once
2. "confirm" x4 -> still once (idempotent, rejects duplicates, never launches twice)
3. cancel -> nothing sent
4. expired pending -> asks again
5. confirm with no pending action -> asks what to confirm
6. Multi-language confirmation and cancellation support
7. WhatsApp is NOT opened before confirmation
8. Honest status: "done" only after sent tick is verified, otherwise "unverified" (never speaks success)
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from core import confirm
from core import outcome
from plugins import whatsapp_automation


class TestWhatsAppConfirmation(unittest.TestCase):
    def setUp(self):
        confirm.reset_state()
        self.polish_patch = patch(
            "plugins.whatsapp_automation._polish_message_formal",
            side_effect=lambda x: x,
        )
        self.polish_patch.start()

    def tearDown(self):
        self.polish_patch.stop()
        confirm.reset_state()

    # ── Test 1: Confirm once -> sent once ────────────────────────────────────

    def test_confirm_once_sent_once(self):
        """User says confirm -> action executes exactly once."""
        send_mock = MagicMock(
            return_value=outcome.result_done(
                "whatsapp_send",
                "I have sent the message to Mom: 'HI'",
                verified=True,
                data={"contact": "Mom", "text": "HI", "verified": True},
            )
        )

        pending = confirm.save_pending(
            tool="whatsapp_send",
            params={"contact": "Mom", "text": "HI"},
            run=send_mock,
            expires_in=60.0,
            title="Send WhatsApp message to Mom",
            detail="Message: 'HI'",
        )
        self.assertIsNotNone(pending.id)
        self.assertEqual(pending.status, "needs_confirmation")

        # User confirms
        res = confirm.intercept_confirmation("confirm")
        self.assertIsNotNone(res)
        self.assertEqual(send_mock.call_count, 1)
        self.assertEqual(res.status, outcome.STATUS_DONE)
        self.assertTrue(res.verified)

        # Check action in store marked executed
        stored = confirm.get_pending(pending.id)
        self.assertIsNotNone(stored)
        self.assertTrue(stored.executed)
        self.assertEqual(stored.status, "executed")

    # ── Test 2: "confirm" x4 -> still once ───────────────────────────────────

    def test_confirm_x4_still_once(self):
        """Saying confirm 4 times runs the action only once; duplicates are rejected."""
        send_mock = MagicMock(
            return_value=outcome.result_done(
                "whatsapp_send",
                "I have sent the message to Mom: 'HI'",
                verified=True,
                data={"contact": "Mom", "text": "HI"},
            )
        )

        pending = confirm.save_pending(
            tool="whatsapp_send",
            params={"contact": "Mom", "text": "HI"},
            run=send_mock,
            expires_in=60.0,
            title="Send WhatsApp message to Mom",
        )

        # 1st confirm -> executes
        res1 = confirm.intercept_confirmation("confirm")
        self.assertEqual(send_mock.call_count, 1)
        self.assertEqual(res1.status, outcome.STATUS_DONE)

        # 2nd confirm -> rejected duplicate
        res2 = confirm.intercept_confirmation("confirm")
        self.assertEqual(send_mock.call_count, 1)
        self.assertTrue(res2.data.get("duplicate"))

        # 3rd confirm -> rejected duplicate
        res3 = confirm.intercept_confirmation("yes")
        self.assertEqual(send_mock.call_count, 1)
        self.assertTrue(res3.data.get("duplicate"))

        # 4th confirm -> rejected duplicate
        res4 = confirm.intercept_confirmation("proceed")
        self.assertEqual(send_mock.call_count, 1)
        self.assertTrue(res4.data.get("duplicate"))

        # Total executions: strictly 1!
        self.assertEqual(send_mock.call_count, 1)

    # ── Test 3: Cancel -> nothing sent ───────────────────────────────────────

    def test_cancel_nothing_sent(self):
        """User says cancel -> action is discarded, nothing sent."""
        send_mock = MagicMock(return_value="Should not run")

        pending = confirm.save_pending(
            tool="whatsapp_send",
            params={"contact": "Mom", "text": "HI"},
            run=send_mock,
            expires_in=60.0,
            title="Send WhatsApp message to Mom",
        )

        res = confirm.intercept_confirmation("cancel")
        self.assertIsNotNone(res)
        self.assertEqual(send_mock.call_count, 0)

        stored = confirm.get_pending(pending.id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "cancelled")
        self.assertFalse(stored.executed)

        # Subsequent confirm also does not execute cancelled action
        res_after = confirm.intercept_confirmation("confirm")
        self.assertEqual(send_mock.call_count, 0)

    # ── Test 4: Expired pending -> asks again ────────────────────────────────

    def test_expired_pending_asks_again(self):
        """When an action has expired (>60s), confirmation asks again and does not send."""
        send_mock = MagicMock(return_value="Should not run")

        # Create action with 0.05s timeout
        pending = confirm.save_pending(
            tool="whatsapp_send",
            params={"contact": "Mom", "text": "HI"},
            run=send_mock,
            expires_in=0.05,
            title="Send WhatsApp message to Mom",
        )

        time.sleep(0.08)  # Let it expire

        # User attempts to confirm expired action
        res = confirm.intercept_confirmation("confirm")
        self.assertIsNotNone(res)
        self.assertEqual(send_mock.call_count, 0)
        # Asks again
        self.assertEqual(res.status, outcome.STATUS_NEEDS_CONFIRMATION)
        self.assertTrue(res.data.get("expired"))
        self.assertIn("expired", res.message.lower())

    # ── Test 5: Confirm with no pending action -> asks what to confirm ───────

    def test_confirm_with_no_pending_action_asks_what_to_confirm(self):
        """Confirming when nothing is pending asks what to confirm."""
        self.assertIsNone(confirm.get_pending())

        res = confirm.intercept_confirmation("confirm")
        self.assertIsNotNone(res)
        self.assertTrue(res.data.get("no_pending"))
        self.assertIn("what would you like me to confirm", res.message.lower())

    # ── Test 6: Multi-language support ───────────────────────────────────────

    def test_multilingual_confirmations_and_cancellations(self):
        """Language-agnostic confirmation and cancellation lexicons."""
        cases = [
            ("yes", True),
            ("send", True),
            ("go ahead", True),
            ("do it", True),
            ("proceed", True),
            ("sí", True),
            ("hazlo", True),
            ("enviar", True),
            ("oui", True),
            ("envoyer", True),
            ("ja", True),
            ("bestätigen", True),
            ("haan", True),
            ("bhej do", True),
            ("sari", True),
            ("确认", True),
            ("送信", True),
            ("no", False),
            ("cancel", False),
            ("stop", False),
            ("cancela", False),
            ("non", False),
            ("nein", False),
            ("nahi", False),
            ("mat bhejo", False),
            ("取消", False),
            ("キャンセル", False),
        ]

        for word, is_confirm in cases:
            if is_confirm:
                self.assertTrue(
                    confirm.matches_confirm(word),
                    f"Expected '{word}' to match confirm vocabulary",
                )
            else:
                self.assertTrue(
                    confirm.matches_cancel(word),
                    f"Expected '{word}' to match cancel vocabulary",
                )

    # ── Test 7: WhatsApp is NOT opened before confirmation ───────────────────

    def test_do_not_open_whatsapp_before_confirmation(self):
        """Calling whatsapp_automation without confirmation does NOT launch WhatsApp."""
        with patch("plugins._whatsapp_core.get") as mock_wa_get:
            res = whatsapp_automation.run({"contact": "Mom", "message": "HI"})

            # Must NOT touch WhatsApp driver before confirmation
            self.assertEqual(mock_wa_get.call_count, 0)

            # Must return needs_confirmation
            self.assertEqual(res.status, outcome.STATUS_NEEDS_CONFIRMATION)
            self.assertIn("id", res.data)
            action_id = res.data["id"]

            # Store contains the pending action with exact params
            pending = confirm.get_pending(action_id)
            self.assertIsNotNone(pending)
            self.assertEqual(pending.params.get("contact"), "Mom")
            self.assertIn("HI", pending.params.get("text"))
            self.assertEqual(pending.expires_in, 60.0)

    # ── Test 8: Honest ActionResult status: done vs unverified ───────────────

    def test_honest_status_done_only_after_sent_tick_verified(self):
        """'done' ONLY after sent tick is verified; otherwise 'unverified'. Never speak success on unverified."""
        mock_driver = MagicMock()

        # Case A: Sent tick confirmed visible
        mock_driver.send_message_to.return_value = (
            True,
            "Sent message snippet 'HI' confirmed visible in chat history with verified sent tick.",
        )
        with patch("plugins._whatsapp_core.get", return_value=(mock_driver, "")):
            res_done = whatsapp_automation.run(
                {"contact": "Mom", "message": "HI", "_execution_authorized": True}
            )
            self.assertEqual(res_done.status, outcome.STATUS_DONE)
            self.assertTrue(res_done.verified)
            self.assertIn("I have sent the message to Mom", res_done.message)

        # Case B: Keystrokes sent, but sent tick unverified
        mock_driver.send_message_to.return_value = (
            True,
            "Message input field cleared on send, but sent tick could not be confirmed.",
        )
        with patch("plugins._whatsapp_core.get", return_value=(mock_driver, "")):
            res_unver = whatsapp_automation.run(
                {"contact": "Mom", "message": "HI", "_execution_authorized": True}
            )
            self.assertEqual(res_unver.status, outcome.STATUS_UNVERIFIED)
            self.assertFalse(res_unver.verified)
            # Must NOT claim message was sent
            self.assertNotIn("I have sent the message", res_unver.message)
            self.assertIn("could not be confirmed", res_unver.message)


if __name__ == "__main__":
    unittest.main()
