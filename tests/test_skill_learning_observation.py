"""
tests/test_skill_learning_observation.py — Verification of Part 1 (Richer Observation while Learning).

Tests:
1. Purely observant: HUD learning indicator shown; confirmations and other actions suppressed during learning.
2. Semantic click identity: role, accessible name, automation_id, app, URL; never bare (x, y) coordinates.
3. Keyboard shortcuts and typed text: shortcuts captured; secrets/passwords redacted to [SECRET:length=N] with length.
4. Navigation: URL changes, tab switches, window activations.
5. Clipboard operations: copy/paste with char_count only (no full text).
6. File dialogs: path and filename captured.
7. User narration: speech captured as narration event aligned with timestamps.
8. Privacy safety: memory-only traces, auto-pause on password manager, banking, and lock screen.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from core.confirm import ConfirmationManager
from core.skill_learning.event_recorder import EventRecorder
from core.skill_learning.learning_manager import LearningManager
from core.skill_learning.models import UIEvent
from core.skill_learning.observation_engine import ObservationEngine
from core.skill_learning.skill_security import is_privacy_risk_window, redact_secret_with_length


class TestPurelyObservantAndHUD(unittest.TestCase):
    def setUp(self):
        self.manager = LearningManager.get_instance()
        if self.manager.is_learning_active():
            self.manager.cancel_learning()

    def tearDown(self):
        if self.manager.is_learning_active():
            self.manager.cancel_learning()

    def test_hud_indicator_shown_on_learning_start(self):
        mock_player = MagicMock()
        self.manager.start_learning(goal="Export customer report", player=mock_player)
        self.assertTrue(self.manager.is_learning_active())
        mock_player.show_content.assert_called_once()
        args, _ = mock_player.show_content.call_args
        self.assertEqual(args[0], "SKILL LEARNING ACTIVE")
        self.assertIn("Export customer report", args[1])

    def test_confirmation_suppressed_during_learning(self):
        self.manager.start_learning(goal="Demonstrating workflow")
        run_called = []

        def dummy_action():
            run_called.append(True)
            return "executed"

        res = ConfirmationManager.request_action(
            action="delete_file",
            parameters={"file": "test.txt"},
            risk_level="high_impact",
            title="Delete File",
            detail="Delete test file",
            run=dummy_action,
        )
        self.assertIn("Learning mode is active", res)
        self.assertEqual(run_called, [], "Action should not have executed during learning")


class TestSemanticClicks(unittest.TestCase):
    def setUp(self):
        self.recorder = EventRecorder()
        self.recorder.start()

    def tearDown(self):
        self.recorder.stop()

    def test_semantic_click_capture_not_bare_coordinates(self):
        with patch("core.skill_learning.event_recorder.capture_element_at_point") as mock_cap, \
             patch("core.skill_learning.event_recorder.get_current_ui_state") as mock_ui:
            
            mock_cap.return_value = {
                "name": "Export Data",
                "control_type": "Button",
                "role": "button",
                "automation_id": "btn_export_data",
                "class_name": "AppButton",
            }
            ui_state = MagicMock()
            ui_state.app_name = "Chrome"
            ui_state.window_title = "Analytics Dashboard"
            ui_state.url = "https://app.example.com/analytics"
            mock_ui.return_value = ui_state

            evt = self.recorder.record_click(100, 200, button="left")
            self.assertIsNotNone(evt)
            self.assertEqual(evt.element_name, "Export Data")
            self.assertEqual(evt.role, "button")
            self.assertEqual(evt.automation_id, "btn_export_data")
            self.assertEqual(evt.app_name, "Chrome")
            self.assertEqual(evt.url, "https://app.example.com/analytics")
            
            # Selector must NOT be bare coordinates
            self.assertNotIn("x", evt.selector)
            self.assertNotIn("y", evt.selector)
            self.assertEqual(evt.selector.get("name"), "Export Data")
            self.assertEqual(evt.selector.get("role"), "button")
            self.assertEqual(evt.selector.get("automation_id"), "btn_export_data")


class TestKeyboardShortcutsAndSecretRedaction(unittest.TestCase):
    def setUp(self):
        self.recorder = EventRecorder()
        self.recorder.start()

    def tearDown(self):
        self.recorder.stop()

    def test_record_keyboard_shortcuts(self):
        for shortcut in ("Ctrl+A", "Ctrl+C", "Ctrl+V", "Ctrl+S"):
            evt = self.recorder.record_hotkey(shortcut, app_name="Notepad", window_title="Doc - Notepad")
            self.assertIsNotNone(evt)
            self.assertEqual(evt.event_type, "hotkey")
            self.assertEqual(evt.shortcut, shortcut)
            self.assertEqual(evt.input_value, shortcut)

    def test_secret_redaction_with_length(self):
        secret_password = "super_secret_password_123"
        sanitized, is_secret, sec_len = redact_secret_with_length(
            control_info={"name": "Password", "control_type": "Edit", "is_sensitive": True},
            text_value=secret_password,
        )
        self.assertTrue(is_secret)
        self.assertEqual(sec_len, len(secret_password))
        self.assertEqual(sanitized, f"[SECRET:length={len(secret_password)}]")
        self.assertNotIn(secret_password, sanitized)

    def test_api_key_redaction_in_text_input(self):
        api_key = "sk-1234567890abcdef1234567890abcdef"
        evt = self.recorder.record_text_input(api_key, element_info={"name": "API Key", "control_type": "Edit"})
        self.assertIsNotNone(evt)
        self.assertTrue(evt.is_secret)
        self.assertEqual(evt.secret_length, len(api_key))
        self.assertEqual(evt.input_value, f"[SECRET:length={len(api_key)}]")
        self.assertNotIn(api_key, evt.input_value)


class TestNavigationClipboardAndDialogs(unittest.TestCase):
    def setUp(self):
        self.recorder = EventRecorder()
        self.recorder.start()

    def tearDown(self):
        self.recorder.stop()

    def test_record_navigation(self):
        evt = self.recorder.record_navigation("https://github.com/settings", app_name="Chrome", window_title="Settings")
        self.assertIsNotNone(evt)
        self.assertEqual(evt.event_type, "navigation")
        self.assertEqual(evt.url, "https://github.com/settings")

    def test_record_clipboard_char_count_only(self):
        sensitive_snippet = "Private client financial data..."
        evt = self.recorder.record_clipboard("copy", char_count=len(sensitive_snippet), app_name="Excel")
        self.assertIsNotNone(evt)
        self.assertEqual(evt.event_type, "clipboard")
        self.assertEqual(evt.char_count, len(sensitive_snippet))
        self.assertNotIn(sensitive_snippet, evt.input_value)
        self.assertEqual(evt.input_value, f"[CLIPBOARD:{len(sensitive_snippet)} chars]")

    def test_record_file_dialog(self):
        evt = self.recorder.record_file_dialog("save", file_path="C:/Users/Report/2026_q1.xlsx", filename="2026_q1.xlsx")
        self.assertIsNotNone(evt)
        self.assertEqual(evt.event_type, "file_dialog")
        self.assertEqual(evt.file_path, "C:/Users/Report/2026_q1.xlsx")
        self.assertEqual(evt.filename, "2026_q1.xlsx")


class TestUserNarrationAlignment(unittest.TestCase):
    def setUp(self):
        self.manager = LearningManager.get_instance()
        if self.manager.is_learning_active():
            self.manager.cancel_learning()

    def tearDown(self):
        if self.manager.is_learning_active():
            self.manager.cancel_learning()

    def test_spoken_narration_captured_as_intent_clue(self):
        self.manager.start_learning(goal="Summarize file")
        t0 = time.time()
        evt = self.manager.record_narration("Now I click upload and select the financial report", timestamp=t0)
        self.assertIsNotNone(evt)
        self.assertEqual(evt.event_type, "narration")
        self.assertEqual(evt.input_value, "Now I click upload and select the financial report")

        # Check trace summary includes narration
        summary = self.manager.observation_engine.format_trace_for_analysis([evt])
        self.assertIn("User Narration (Intent Clue)", summary)
        self.assertIn("Now I click upload", summary)


class TestPrivacyRiskWindowDetection(unittest.TestCase):
    def test_password_manager_detected(self):
        is_risk, rtype = is_privacy_risk_window(title="Bitwarden - Master Password", app_name="Bitwarden")
        self.assertTrue(is_risk)
        self.assertEqual(rtype, "password_manager")

    def test_banking_detected(self):
        is_risk, rtype = is_privacy_risk_window(title="Chase Online Banking - Accounts", url="https://chase.com")
        self.assertTrue(is_risk)
        self.assertEqual(rtype, "banking")

    def test_lock_screen_detected(self):
        is_risk, rtype = is_privacy_risk_window(title="Windows Security", process_name="LogonUI.exe")
        self.assertTrue(is_risk)
        self.assertEqual(rtype, "lock_screen")

    def test_safe_window_not_detected(self):
        is_risk, _ = is_privacy_risk_window(title="Visual Studio Code", app_name="Code", process_name="Code.exe")
        self.assertFalse(is_risk)


if __name__ == "__main__":
    unittest.main()
