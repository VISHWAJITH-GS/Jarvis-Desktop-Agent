"""
tests/test_fuzzy_search_whatsapp_and_memory_platform.py

Comprehensive tests verifying:
1. File Controller: Fuzzy matching, token similarity, and fallback search for file names like 'Cat 1 and 2 solutions_text'.
2. WhatsApp: Explicit search bar clearing before searching contacts by name in send_message and whatsapp_core.
3. Interactive Memory & Actions Platform: MemoryActionsDisplayWindow, loading memory entries, adding/editing/forgetting, and discovering action risk levels.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PyQt6.QtWidgets import QApplication

# Ensure QApplication is initialized for UI tests
app = QApplication.instance() or QApplication(sys.argv)

from actions.file_controller import (
    _score_file_match,
    find_files,
)
from core import outcome
from memory import memory_manager
from ui import JarvisUI, MemoryActionsDisplayWindow


class TestFuzzySearchAndFallback(unittest.TestCase):
    def test_score_file_match_exact_and_fuzzy(self):
        # 1. Exact match
        score, is_exact = _score_file_match("report_final.pdf", "report_final.pdf")
        self.assertTrue(is_exact)
        self.assertAlmostEqual(score, 1.0)

        # 2. User asked "Cat 1 and 2 solutions_text" - match against "Cat 1 and 2 solutions.txt"
        score, is_exact = _score_file_match("Cat 1 and 2 solutions.txt", "Cat 1 and 2 solutions_text")
        self.assertGreater(score, 0.85)

        # 3. Match against "cat1_and_2_solutions.docx"
        score2, is_exact2 = _score_file_match("cat1_and_2_solutions.docx", "Cat 1 and 2 solutions_text")
        self.assertGreater(score2, 0.70)

        # 4. Irrelevant file gets low score
        score_irrelevant, _ = _score_file_match("tax_records_2024.xlsx", "Cat 1 and 2 solutions_text")
        self.assertLess(score_irrelevant, 0.35)

    def test_find_files_fuzzy_fallback_in_temp_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            target_file = tmp_path / "Cat 1 and 2 solutions.txt"
            target_file.write_text("Solution 1 and 2 details", encoding="utf-8")

            # Search with slightly different query
            result = find_files(name="Cat 1 and 2 solutions_text", path=str(tmp_path))
            self.assertIn("Cat 1 and 2 solutions.txt", result)
            self.assertTrue("Found" in result or "similar" in result)


class TestWhatsAppSearchBarEmptying(unittest.TestCase):
    @patch("actions.send_message.pyautogui")
    def test_search_in_app_empties_search_bar_before_typing(self, mock_pyautogui):
        from actions import send_message

        # Mock require_pyautogui
        with patch.object(send_message, "_require_pyautogui", return_value=None):
            with patch.object(send_message, "_paste_text") as mock_paste:
                send_message._search_in_app("Mom")

                # Verify hotkey was pressed to focus search
                mock_pyautogui.hotkey.assert_any_call("ctrl", "f")

                # Verify select all was executed
                mock_pyautogui.hotkey.assert_any_call("ctrl", "a")

                # Verify backspace and delete were pressed to empty the bar
                mock_pyautogui.press.assert_any_call("backspace")
                mock_pyautogui.press.assert_any_call("delete")

                # Verify contact name was pasted after emptying
                mock_paste.assert_called_with("Mom")

    def test_whatsapp_core_clears_search_bar_before_typing(self):
        from plugins import _whatsapp_core as wa

        driver = wa.WhatsAppDriver()
        mock_res = outcome.result_done("resolve_contact", "Found", verified=True, data={"contact": "Rahul"})
        with patch.object(driver._web, "resolve_contact", return_value=mock_res):
            ok, chosen, candidates = driver.resolve_contact("Rahul")
            self.assertTrue(ok)
            self.assertEqual(chosen, "Rahul")


class TestMemoryAndActionsPlatform(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.player = JarvisUI(face_path="")
        cls.win = cls.player._win

    def test_memory_actions_window_instantiation_and_popup(self):
        mem_act_win = MemoryActionsDisplayWindow(self.win)
        self.assertIsNotNone(mem_act_win)

        # Popup memory tab
        mem_act_win.popup("memory")
        self.assertEqual(mem_act_win._active_tab, "memory")
        self.assertEqual(mem_act_win._stack.currentIndex(), 0)

        # Popup actions tab
        mem_act_win.popup("actions")
        self.assertEqual(mem_act_win._active_tab, "actions")
        self.assertEqual(mem_act_win._stack.currentIndex(), 1)

    def test_memory_crud_operations(self):
        # Remember test key
        res_add = memory_manager.remember("test_unit_matrix_key", "active_quantum_core", category="notes")
        self.assertIn("Remembered", res_add)

        # Verify entry is present in all_entries_for_ui
        entries = memory_manager.all_entries_for_ui()
        matching = [e for e in entries if e.get("key") == "test_unit_matrix_key"]
        self.assertTrue(len(matching) > 0)
        self.assertEqual(matching[0].get("value"), "active_quantum_core")

        # Edit key
        memory_manager.remember("test_unit_matrix_key", "upgraded_core_v2", category="notes")
        entries = memory_manager.all_entries_for_ui()
        matching = [e for e in entries if e.get("key") == "test_unit_matrix_key"]
        self.assertEqual(matching[0].get("value"), "upgraded_core_v2")

        # Forget key
        res_del = memory_manager.forget("test_unit_matrix_key", category="notes")
        self.assertIn("Forgotten", res_del)
        entries = memory_manager.all_entries_for_ui()
        matching = [e for e in entries if e.get("key") == "test_unit_matrix_key"]
        self.assertEqual(len(matching), 0)

    def test_action_risk_level_discovery(self):
        from core.permissions import ActionPermissionManager, ActionRiskLevel

        # Immediate tools
        risk_search = ActionPermissionManager.get_risk_level("web_search")
        self.assertEqual(risk_search, ActionRiskLevel.IMMEDIATE)

        # Confirmation tools
        risk_connect_wifi = ActionPermissionManager.get_risk_level("windows_control", {"action": "connect_wifi"})
        self.assertEqual(risk_connect_wifi, ActionRiskLevel.CONFIRMATION_REQUIRED)

        # Forbidden tools
        risk_format = ActionPermissionManager.get_risk_level("file_controller", {"action": "format_drive"})
        self.assertEqual(risk_format, ActionRiskLevel.FORBIDDEN)

    def test_player_thread_safe_show_memory_actions(self):
        self.player.show_memory_actions_display(tab="memory")
        QApplication.processEvents()
        self.assertIsNotNone(self.win._memory_actions_dock)
        self.assertTrue(self.win._memory_actions_dock.isVisible())

    def test_memory_actions_minimizes_to_left_dock(self):
        self.player.show_memory_actions_display(tab="memory")
        QApplication.processEvents()
        mem_win = self.win._memory_actions_dock

        # Minimize to left dock
        mem_win.minimize_to_dock()
        QApplication.processEvents()
        self.assertFalse(mem_win.isVisible())
        self.assertTrue(self.win._memory_actions_dock_minimized_to_bar)
        self.assertIn("memory_actions", self.win._dots_overlay._buttons)
        self.assertEqual(self.win._dots_overlay._buttons["memory_actions"].text(), "")
        self.assertFalse(self.win._dots_overlay._buttons["memory_actions"].icon().isNull())

        # Restore from left dock
        self.win._on_restore_display("memory_actions")
        QApplication.processEvents()
        self.assertTrue(mem_win.isVisible())
        self.assertFalse(self.win._memory_actions_dock_minimized_to_bar)


if __name__ == "__main__":
    unittest.main()
