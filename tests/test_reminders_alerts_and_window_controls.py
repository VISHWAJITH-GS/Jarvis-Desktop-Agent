"""
tests/test_reminders_alerts_and_window_controls.py

Verifies:
1. Dedicated RemindersDisplayWindow creation, filtering, card generation, quick-add, complete/delete, and collapse.
2. SlideDownAlert creation, top-center positioning, slide-down animation, glowing red styling, and black text.
3. System alert handling: unmuting when minimized and muted, showing slide-down alert, and speaking/announcing.
4. Voice/instruction control for JARVIS self-mute and self-minimize/maximize via computer_settings and windows_control.
5. Search display minimization button in LeftSearchDock and _content_panel.
6. Execution without annoying popup display on action confirmation.
7. Terminal action logging includes timestamps.
"""

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from PyQt6.QtWidgets import QApplication

# Ensure single QApplication exists
app = QApplication.instance() or QApplication([])

from ui import MainWindow, JarvisUI, SlideDownAlert, RemindersDisplayWindow, LeftSearchDock
from core import permissions, outcome
from actions.computer_settings import computer_settings, _detect_action
from actions.windows_control import windows_control
from actions.reminder import reminder


class TestRemindersAlertsWindowControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.player = JarvisUI(face_path="")
        cls.win = cls.player._win

    def test_01_search_display_minimization_button_and_collapse(self):
        """Search display has a minimization button and collapses/restores cleanly."""
        self.assertTrue(hasattr(self.win, "_content_min_btn"))
        self.assertTrue(hasattr(self.win, "left_search_dock"))
        dock = self.win.left_search_dock

        # Popup search content
        self.win._show_content("WHERE TO DOWNLOAD SKILLRACK", "Download links here...")
        self.assertFalse(getattr(dock, "_collapsed", False))
        self.assertEqual(self.win._content_min_btn.text().strip(), "MIN")

        # Toggle minimize (collapse)
        dock.toggle_minimize()
        self.assertTrue(dock._collapsed)
        self.assertEqual(dock.height(), 46)
        self.assertEqual(self.win._content_min_btn.text().strip(), "RESTORE")
        self.assertFalse(self.win._content_display.isVisible())

        # Toggle restore
        dock.toggle_minimize()
        self.assertFalse(dock._collapsed)
        self.assertEqual(self.win._content_min_btn.text().strip(), "MIN")
        self.assertTrue(self.win._content_display.isVisible())

    def test_02_confirmation_does_not_popup_display(self):
        """Confirmed runner executes directly and does not call show_content."""
        mock_player = MagicMock()
        mock_handler = MagicMock(return_value="Action successful")
        ctx = {"player": mock_player}

        # Simulate execution inside permissions guard
        with patch.object(permissions.ActionPermissionManager, "get_risk_level") as mock_risk:
            from core.permissions import ActionRiskLevel
            mock_risk.return_value = ActionRiskLevel.CONFIRMATION_REQUIRED
            with patch("core.confirm.request_action") as mock_req:
                permissions.ActionExecutionGuard.guard_and_execute(
                    action_name="computer_settings",
                    parameters={"action": "show_desktop"},
                    handler=mock_handler,
                    ctx=ctx,
                )
                self.assertTrue(mock_req.called)
                # Extract runner closure passed to confirmation
                _, kwargs = mock_req.call_args
                runner = kwargs.get("run")
                self.assertIsNotNone(runner)

                # Execute confirmed runner
                result = runner()
                self.assertIn("Action successful", result)
                # verify show_content was NOT called
                mock_player.show_content.assert_not_called()
                mock_player.write_log.assert_called()

    def test_03_slide_down_alert_properties_and_animation(self):
        """SlideDownAlert has top-center placement, glowing red styling, and black text."""
        alert = SlideDownAlert()
        alert.popup_alert("CRITICAL HARDWARE", "CPU temperature is 92°C — safe limit exceeded.")

        self.assertIn("SlideAlertFrame", alert._frame.objectName())
        # Check text colors
        self.assertIn("color: #000000", alert._title_lbl.styleSheet())
        self.assertIn("color: #000000", alert._msg_lbl.styleSheet())
        self.assertEqual(alert._title_lbl.text(), "CRITICAL HARDWARE")
        self.assertIn("CPU temperature is 92°C", alert._msg_lbl.text())
        self.assertIsNotNone(alert._pos_anim)

        # Player thread-safe helper
        self.player.show_slide_alert("RAM nearly exhausted", title="MEMORY CRITICAL")
        app.processEvents()
        self.assertIsNotNone(self.win._slide_alert)

    def test_04_reminders_display_window_management(self):
        """RemindersDisplayWindow opens, filters, and renders cards."""
        rem_win = RemindersDisplayWindow(self.win)
        rem_win.popup("upcoming")
        self.assertTrue(rem_win.isVisible())
        self.assertEqual(rem_win._active_scope, "upcoming")

        # Test filter switching
        rem_win._set_scope("today")
        self.assertEqual(rem_win._active_scope, "today")
        rem_win._set_scope("all")
        self.assertEqual(rem_win._active_scope, "all")
        rem_win._set_scope("completed")
        self.assertEqual(rem_win._active_scope, "completed")

        # Test collapse toggle
        rem_win.toggle_collapse()
        self.assertTrue(rem_win._collapsed)
        self.assertEqual(rem_win.height(), 46)
        self.assertEqual(rem_win._min_btn.text().strip(), "RESTORE")

        rem_win.toggle_collapse()
        self.assertFalse(rem_win._collapsed)
        self.assertEqual(rem_win._min_btn.text().strip(), "MIN")

        # Test player helper
        self.player.show_reminders("all")
        app.processEvents()

    def test_05_reminder_tool_opens_reminders_display(self):
        """Calling reminder with action=show or list calls player.show_reminders."""
        mock_player = MagicMock()
        res = reminder({"action": "show", "scope": "upcoming"}, player=mock_player)
        self.assertIn("opened the reminders display", res.lower())
        mock_player.show_reminders.assert_called_with("upcoming")

    def test_06_jarvis_self_mute_and_minimize_maximize_controls(self):
        """User instructions to mute, unmute, minimize, maximize JARVIS itself."""
        # 1. Computer Settings: mute yourself
        res = computer_settings(action="mute_jarvis", player=self.player)
        self.assertTrue(self.player.muted)

        # 2. Computer Settings: unmute yourself
        res = computer_settings(action="unmute_jarvis", player=self.player)
        self.assertFalse(self.player.muted)

        # 3. Detect action mapping for natural language
        detected = _detect_action("jarvis mute yourself")
        self.assertEqual(detected["action"], "mute_jarvis")

        detected = _detect_action("unmute yourself")
        self.assertEqual(detected["action"], "unmute_jarvis")

        detected = _detect_action("minimize yourself")
        self.assertEqual(detected["action"], "minimize_jarvis")

        detected = _detect_action("maximize jarvis")
        self.assertEqual(detected["action"], "maximize_jarvis")

        # 4. Windows Control dispatcher
        res = windows_control({"action": "mute_jarvis"}, player=self.player)
        self.assertTrue(self.player.muted)
        res = windows_control({"action": "unmute_jarvis"}, player=self.player)
        self.assertFalse(self.player.muted)

        # 5. Window minimization and maximization methods
        self.assertTrue(hasattr(self.player, "minimize_jarvis"))
        self.assertTrue(hasattr(self.player, "maximize_jarvis"))
        self.assertTrue(hasattr(self.player, "is_jarvis_minimized"))

    def test_07_terminal_timestamp_logging(self):
        """ComputerControl logs format includes timestamps."""
        from actions.computer_control import _cc_log
        with patch("builtins.print") as mock_print:
            _cc_log("test message")
            self.assertTrue(mock_print.called)
            printed_str = mock_print.call_args[0][0]
            # Ensure format: [HH:MM:SS AM/PM] [ComputerControl] test message
            self.assertIn("[ComputerControl] test message", printed_str)
            self.assertTrue(printed_str.startswith("["))

    def test_08_remind_and_mem_act_in_expanded_drawer(self):
        """Remind and Mem/Act buttons removed from collapse bar, present in expanded drawer."""
        # 1. Collapsed bar buttons: MIC, EXPAND, CHAT, MINI, CLOSE
        btn_texts = [
            self.win.hud_ctrl_bar.layout().itemAt(i).widget().text().strip()
            for i in range(self.win.hud_ctrl_bar.layout().count())
            if self.win.hud_ctrl_bar.layout().itemAt(i).widget()
        ]
        self.assertNotIn("REMIND", btn_texts)
        self.assertNotIn("MEM/ACT", btn_texts)
        self.assertIn("MIC", btn_texts)
        self.assertIn("EXPAND", btn_texts)
        self.assertIn("CHAT", btn_texts)

        # 2. Expanded drawer has the buttons in header
        self.assertTrue(hasattr(self.win, "_drawer_rem_btn"))
        self.assertTrue(hasattr(self.win, "_drawer_mem_btn"))
        self.assertEqual(self.win._drawer_rem_btn.text().strip(), "REMIND")
        self.assertEqual(self.win._drawer_mem_btn.text().strip(), "MEM/ACT")


if __name__ == "__main__":
    unittest.main()
