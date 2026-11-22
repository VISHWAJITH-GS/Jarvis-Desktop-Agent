"""
tests/test_antigravity_terminal.py — Unit tests for Antigravity terminal management and editor control.
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from actions import antigravity_terminal
from core import outcome


class TestAntigravityTerminal(unittest.TestCase):
    def test_close_all_files_clean(self):
        mock_page = MagicMock()
        with (
            patch("actions.antigravity_terminal.get_window_status", return_value={"is_running": True, "has_unsaved_changes": False}),
            patch("actions.antigravity_terminal.get_antigravity_cdp_manager") as mock_mgr,
        ):
            mock_mgr.return_value.get_workbench_page.return_value = mock_page
            res = antigravity_terminal.close_all_files()
            self.assertEqual(res.status, outcome.STATUS_DONE)
            mock_page.keyboard.press.assert_any_call("w")

    def test_close_all_files_dirty_guard(self):
        with patch("actions.antigravity_terminal.get_window_status", return_value={"is_running": True, "has_unsaved_changes": True, "active_file": "doc.py"}):
            # Without force or save_first, requests confirmation
            res = antigravity_terminal.close_all_files(save_first=False, force=False)
            self.assertEqual(res.status, outcome.STATUS_NEEDS_CONFIRMATION)
            self.assertIn("unsaved changes", res.message)

    def test_close_all_files_dirty_with_save_first(self):
        mock_page = MagicMock()
        with (
            patch("actions.antigravity_terminal.get_window_status", return_value={"is_running": True, "has_unsaved_changes": True}),
            patch("actions.antigravity_terminal.save_file") as mock_save,
            patch("actions.antigravity_terminal.get_antigravity_cdp_manager") as mock_mgr,
        ):
            mock_mgr.return_value.get_workbench_page.return_value = mock_page
            res = antigravity_terminal.close_all_files(save_first=True)
            self.assertEqual(res.status, outcome.STATUS_DONE)
            mock_save.assert_called_once_with(save_all=True)

    def test_close_all_files_dirty_with_force(self):
        mock_page = MagicMock()
        with (
            patch("actions.antigravity_terminal.get_window_status", return_value={"is_running": True, "has_unsaved_changes": True}),
            patch("actions.antigravity_terminal.get_antigravity_cdp_manager") as mock_mgr,
        ):
            mock_mgr.return_value.get_workbench_page.return_value = mock_page
            res = antigravity_terminal.close_all_files(force=True)
            self.assertEqual(res.status, outcome.STATUS_DONE)

    def test_new_terminal_execution(self):
        mock_page = MagicMock()
        with patch("actions.antigravity_terminal.get_antigravity_cdp_manager") as mock_mgr:
            mock_mgr.return_value.get_workbench_page.return_value = mock_page
            res = antigravity_terminal.new_terminal()
            self.assertEqual(res.status, outcome.STATUS_DONE)
            mock_page.keyboard.press.assert_called_once_with("Control+Shift+Backquote")

    def test_new_terminal_refuses_outside_allowed_roots(self):
        with (
            patch("actions.antigravity_terminal.resolve_ide_path", return_value=(Path(r"C:\Windows\System32"), None)),
            patch("actions.antigravity_terminal.is_allowed_root", return_value=False),
        ):
            res = antigravity_terminal.new_terminal(cwd=r"C:\Windows\System32")
            self.assertEqual(res.status, outcome.STATUS_BLOCKED)

    def test_run_terminal_command_success(self):
        mock_proc = MagicMock(returncode=0, stdout="test passed\n", stderr="")
        with (
            patch("actions.antigravity_terminal.is_allowed_root", return_value=True),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
        ):
            res = antigravity_terminal.run_terminal_command("pytest", cwd="D:\\test")
            self.assertEqual(res.status, outcome.STATUS_DONE)
            self.assertTrue(res.success)
            self.assertEqual(res.data["exit_code"], 0)
            self.assertIn("test passed", res.message)

    def test_run_terminal_command_failure_exit_code(self):
        mock_proc = MagicMock(returncode=1, stdout="", stderr="SyntaxError: invalid syntax\n")
        with (
            patch("actions.antigravity_terminal.is_allowed_root", return_value=True),
            patch("subprocess.run", return_value=mock_proc),
        ):
            res = antigravity_terminal.run_terminal_command("python bad.py")
            self.assertEqual(res.status, outcome.STATUS_FAILED)
            self.assertFalse(res.success)
            self.assertEqual(res.data["exit_code"], 1)

    def test_run_terminal_command_timeout(self):
        with (
            patch("actions.antigravity_terminal.is_allowed_root", return_value=True),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="sleep 100", timeout=5)),
        ):
            res = antigravity_terminal.run_terminal_command("sleep 100", timeout=5)
            self.assertEqual(res.status, outcome.STATUS_FAILED)
            self.assertIn("timed out", res.message)

    def test_run_terminal_command_forbidden_blocked(self):
        res = antigravity_terminal.run_terminal_command("format c: /fs:NTFS")
        self.assertEqual(res.status, outcome.STATUS_BLOCKED)
        self.assertIn("prohibited", res.message)


if __name__ == "__main__":
    unittest.main()
