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

    # ── Test 4: Expired pending -> cleared and never resurfaced ──────────────

    def test_expired_pending_asks_again(self):
        """When an action has expired (>60s), confirmation is cancelled and never resurfaced."""
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
        # Does NOT resurface; reports expired and cancelled
        self.assertEqual(res.status, outcome.STATUS_FAILED)
        self.assertTrue(res.data.get("expired"))
        self.assertIn("expired", res.message.lower())

    def test_pending_cleared_on_user_new_request(self):
        """When user changes the subject or asks for something else, pending action is cleared (Part 4.2)."""
        send_mock = MagicMock(return_value="Should not run")
        pending = confirm.save_pending(
            tool="whatsapp_send",
            params={"contact": "Mom", "text": "HI"},
            run=send_mock,
            expires_in=60.0,
            title="Send WhatsApp message to Mom",
        )
        self.assertIsNotNone(confirm.get_pending())

        # User asks for something else (action command)
        res = confirm.intercept_confirmation("open youtube")
        self.assertIsNone(res)
        self.assertIsNone(confirm.get_pending())

    # ── Test 5: Confirm with no pending action -> harmless & conversational phrases pass through ──

    def test_confirm_with_no_pending_action_asks_what_to_confirm(self):
        """Confirming when nothing is pending says 'There is nothing to confirm.'
        Conversational phrases (Okay, Yes please, Send it again) return None when nothing is pending."""
        self.assertIsNone(confirm.get_pending())

        # Explicit confirm says there is nothing to confirm
        res = confirm.intercept_confirmation("confirm")
        self.assertIsNotNone(res)
        self.assertTrue(res.data.get("no_pending"))
        self.assertIn("nothing to confirm", res.message.lower())

        # Conversational phrases return None when nothing is pending (do not hijack dialogue)
        for phrase in ("Okay", "ok", "Yes please", "sure", "Send it again", "send again", "try again"):
            intercepted = confirm.intercept_confirmation(phrase)
            self.assertIsNone(intercepted, f"Phrase '{phrase}' should return None when nothing is pending")

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
        with patch("plugins._whatsapp_web.get_driver", return_value=mock_driver):
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
        with patch("plugins._whatsapp_web.get_driver", return_value=mock_driver):
            res_unver = whatsapp_automation.run(
                {"contact": "Mom", "message": "HI", "_execution_authorized": True}
            )
            self.assertEqual(res_unver.status, outcome.STATUS_UNVERIFIED)
            self.assertFalse(res_unver.verified)
            # Must NOT claim message was sent
            self.assertNotIn("I have sent the message", res_unver.message)
            self.assertIn("could not be confirmed", res_unver.message)

    # ── Test 9: Action requests and conversational sentences do NOT false-confirm ──

    def test_command_and_conversational_utterances_not_false_confirmed(self):
        """Action initiating requests and conversational sentences must NOT match confirm."""
        false_cases = [
            "Send a message to my mom, Hi.",
            "Send a message to Mom - HI",
            "Then send their hair a high.",
            "Can you send an email to Bob?",
            "Please send a message to Dad",
            "Send text to sister: arrived safely",
            "Are you sure?",
            "Is everything fine?",
        ]
        for utterance in false_cases:
            self.assertFalse(
                confirm.matches_confirm(utterance),
                f"Utterance '{utterance}' falsely matched confirmation!",
            )

    # ── Test 10: Conversational sentences do NOT false-cancel ────────────────

    def test_conversational_sentences_not_false_cancelled(self):
        """Conversational sentences containing negative words must NOT cancel pending actions."""
        false_cases = [
            "No, it doesn't make Message is not sent.",
            "No problem at all",
            "I have no doubt about that",
            "There is no issue here",
        ]
        for utterance in false_cases:
            self.assertFalse(
                confirm.matches_cancel(utterance),
                f"Utterance '{utterance}' falsely matched cancellation!",
            )

    # ── Test 11: has_recent_execution uses executed_at and expires accurately ─

    def test_has_recent_execution_uses_executed_at(self):
        """has_recent_execution checks actual executed_at time, not full timeout duration."""
        pending = confirm.save_pending(
            tool="whatsapp_send",
            params={"contact": "Mom", "text": "HI"},
            run=lambda: "Done",
            expires_in=60.0,
            title="Send WhatsApp message to Mom",
        )
        self.assertFalse(confirm.has_recent_execution("whatsapp_send", seconds=5.0))

        # Execute the action
        confirm.resolve(True, action_id=pending.id)
        self.assertTrue(confirm.has_recent_execution("whatsapp_send", seconds=5.0))

        # Simulate 6 seconds passed after execution (within 60s timeout, but beyond 5s deduplication window)
        stored = confirm.get_pending(pending.id)
        self.assertIsNotNone(stored)
        stored.executed_at = time.monotonic() - 6.0

        # Should now return False, allowing subsequent messages to be sent!
        self.assertFalse(confirm.has_recent_execution("whatsapp_send", seconds=5.0))

    # ── Test 13: Parameter canonicalization and deduplication ────────────────

    def test_parameter_canonicalization_and_deduplication(self):
        """Parameter variants (key order, alias keys, extra whitespace, capitalization, redundant action/platform) map to the same pending id."""
        # 1. Base action
        p1 = confirm.save_pending(
            tool="whatsapp_send",
            params={"contact": "Mom", "text": "Hi."},
            expires_in=60.0,
            title="Send WhatsApp message to Mom",
        )
        self.assertIsNotNone(p1.id)

        # 2. Re-calling with aliases, extra spaces, redundant fields
        p2 = confirm.save_pending(
            tool="send_message",
            params={
                "action": "send_message",
                "platform": "whatsapp",
                "receiver": "  mom  ",
                "message_text": "Hi.  ",
            },
            expires_in=60.0,
        )
        # Must return the SAME pending action and ID
        self.assertEqual(p1.id, p2.id)

        # 3. Another variant using recipient and message
        p3 = confirm.save_pending(
            tool="whatsapp_send",
            params={
                "recipient": "MOM",
                "message": "Hi.",
                "action": "whatsapp_send",
            },
            expires_in=60.0,
        )
        self.assertEqual(p1.id, p3.id)

    # ── Test 14: Confirmed action immediate execution & feedback callback ─────

    def test_confirmed_action_immediate_execution_and_feedback_callback(self):
        """When an action is confirmed, it executes immediately and invokes the session feedback callback."""
        feedback_received = []

        def _on_feedback(res, pending):
            feedback_received.append((res, pending))

        confirm.set_feedback_callback(_on_feedback)
        try:
            executed_flag = []

            def _handler():
                executed_flag.append(True)
                return outcome.result_done("whatsapp_send", "Message sent to Mom.", verified=True)

            pending = confirm.save_pending(
                tool="whatsapp_send",
                params={"contact": "Mom", "text": "Hi."},
                run=_handler,
                expires_in=60.0,
                title="Send WhatsApp message to Mom",
            )

            # Confirm via voice
            res = confirm.intercept_confirmation("confirm")
            self.assertIsNotNone(res)
            self.assertEqual(len(executed_flag), 1)
            self.assertEqual(len(feedback_received), 1)
            fb_res, fb_pending = feedback_received[0]
            self.assertEqual(fb_pending.id, pending.id)
            self.assertEqual(fb_res.status, outcome.STATUS_DONE)
        finally:
            confirm.set_feedback_callback(None)

    # ── Test 15: Re-calling after confirmed execution returns done ───────────

    def test_re_call_after_confirmed_execution_returns_done(self):
        """When model re-calls tool after confirmed execution, it returns already sent, not needs_confirmation."""
        mock_driver = MagicMock()
        mock_driver.send_message_to.return_value = (True, "Sent tick confirmed visible in chat history.")

        with patch("plugins._whatsapp_web.get_driver", return_value=mock_driver):
            # 1. First call asks confirmation
            res1 = whatsapp_automation.run({"contact": "Mom", "message": "Hi."})
            self.assertEqual(res1.status, outcome.STATUS_NEEDS_CONFIRMATION)

            # 2. User confirms -> executes
            res_conf = confirm.intercept_confirmation("confirm")
            self.assertIsNotNone(res_conf)
            self.assertEqual(res_conf.status, outcome.STATUS_DONE)

            # 3. Model re-calls tool with equivalent parameters (the bug scenario!)
            res2 = whatsapp_automation.run({"contact": "Mom", "message": "Hi."})
            self.assertEqual(res2.status, outcome.STATUS_DONE)
            self.assertTrue(res2.data.get("already_sent"))
            self.assertIn("already been sent", res2.message.lower())

    # ── Test 16: Status/read/list calls never require confirmation ────────────

    def test_status_read_list_calls_never_require_confirmation(self):
        """Status, read, and list calls always execute immediately without confirmation."""
        from core.permissions import ActionExecutionGuard, ActionRiskLevel

        # 1. WhatsApp read actions
        with patch("plugins.whatsapp_reader.run") as mock_wa_reader:
            mock_wa_reader.return_value = outcome.result_done("whatsapp_read", "Latest messages read.", verified=True)
            res = whatsapp_automation.run({"action": "read_chat", "contact": "Mom"})
            self.assertEqual(mock_wa_reader.call_count, 1)
            self.assertEqual(res.status, outcome.STATUS_DONE)

        # 2. Permissions matrix risk checks
        self.assertEqual(ActionExecutionGuard.get_risk_level("system_status"), ActionRiskLevel.IMMEDIATE)
        self.assertEqual(ActionExecutionGuard.get_risk_level("list_tasks"), ActionRiskLevel.IMMEDIATE)
        self.assertEqual(ActionExecutionGuard.get_risk_level("whatsapp_send", {"action": "read"}), ActionRiskLevel.IMMEDIATE)
        self.assertEqual(ActionExecutionGuard.get_risk_level("windows_control", {"action": "get_wifi_status"}), ActionRiskLevel.IMMEDIATE)

    # ── Test 17: Browser control cannot bypass confirmation gate on WhatsApp Web

    def test_browser_control_cannot_bypass_confirmation_gate_on_whatsapp_web(self):
        """browser_control allows normal typing/navigation, but gates Send clicks, Enter, and press_enter."""
        from actions.browser_control import browser_control

        with patch("actions.browser_control._get_active_url", return_value="https://web.whatsapp.com"):
            with patch("actions.browser_session.get_browser_session_manager") as mock_bm:
                mock_mgr = MagicMock()
                mock_mgr.check_connection.return_value = (True, "")
                mock_page = MagicMock()
                mock_page.is_closed.return_value = False
                mock_mgr.active_page = mock_page
                mock_bm.return_value = mock_mgr

                # 1. Normal typing in search or composer without enter is ALLOWED (Task 2)
                with patch("actions.action_executor.get_action_executor") as mock_exec_getter:
                    mock_exec = MagicMock()
                    mock_exec.input_text.return_value = "Typed 'Search query'"
                    mock_exec_getter.return_value = mock_exec
                    res_type = browser_control({"action": "type", "text": "Search query", "index": 1})
                    self.assertNotIn("blocked by the confirmation gate", str(res_type).lower())

                # 2. Clicking Send button triggers confirmation gate (Task 3)
                res_click = browser_control({"action": "click", "text": "Send"})
                self.assertEqual(res_click.status, outcome.STATUS_NEEDS_CONFIRMATION)
                self.assertIn("Send to", res_click.message)

                # 3. Pressing Enter while composer is focused triggers confirmation gate (Task 3)
                with patch("actions.whatsapp_gate.is_whatsapp_send_action", return_value=True):
                    res_press = browser_control({"action": "press", "key": "Enter"})
                    self.assertEqual(res_press.status, outcome.STATUS_NEEDS_CONFIRMATION)

                # 4. Typing with press_enter=True triggers confirmation gate (Task 3)
                with patch("actions.whatsapp_gate.is_whatsapp_send_action", return_value=True):
                    res_type_enter = browser_control({"action": "type", "text": "Hi", "press_enter": True})
                    self.assertEqual(res_type_enter.status, outcome.STATUS_NEEDS_CONFIRMATION)

    # ── Test 18: screen_process and recall_memory do not return Unknown tool ──

    def test_screen_process_and_recall_memory_not_unknown_tool(self):
        """Dispatcher correctly handles inline tools without overwriting with 'Unknown tool'."""
        import asyncio
        from core.tool_dispatcher import ToolDispatcher

        mock_live = MagicMock()
        mock_live._vision_busy = False
        mock_live._vision_last_time = 0.0
        mock_live._action_registry = None
        mock_live._plugin_registry = None

        dispatcher = ToolDispatcher(mock_live)

        class FakeFC:
            def __init__(self, name, args):
                self.id = "fc_test_1"
                self.name = name
                self.args = args

        with patch("core.platform_hooks.get_hook", return_value=lambda: (b"dummy_img", "image/jpeg")):
            # Test screen_process
            fc_screen = FakeFC("screen_process", {"text": "What is on screen?"})
            resp = asyncio.run(dispatcher.execute_tool_inner(mock_live, fc_screen))
            self.assertNotIn("Unknown tool", resp.response["result"])
            self.assertIn("VISION_ACTIVE", resp.response["result"])

        with patch("core.tool_dispatcher.search_memory", return_value="Memory snippet"):
            # Test recall_memory
            fc_mem = FakeFC("recall_memory", {"query": "wifi password"})
            resp_mem = asyncio.run(dispatcher.execute_tool_inner(mock_live, fc_mem))
            self.assertNotIn("Unknown tool", resp_mem.response["result"])
            self.assertEqual(resp_mem.response["result"], "Memory snippet")


if __name__ == "__main__":
    unittest.main()
