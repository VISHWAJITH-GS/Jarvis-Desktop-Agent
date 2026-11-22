"""
tests/test_antigravity_ide.py — Unit and fixture tests for Antigravity IDE integration.
"""

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from actions import antigravity_launcher
from core import outcome


class TestAntigravityLauncher(unittest.TestCase):
    def test_find_antigravity_launcher_from_which(self):
        with patch("shutil.which", return_value="C:\\Fake\\antigravity-ide.cmd"), patch("os.path.isfile", return_value=True):
            launcher = antigravity_launcher.find_antigravity_launcher()
            self.assertIsNotNone(launcher)
            self.assertIn("antigravity-ide.cmd", launcher)

    def test_find_antigravity_launcher_missing(self):
        with patch("shutil.which", return_value=None), patch("os.path.isfile", return_value=False):
            launcher = antigravity_launcher.find_antigravity_launcher()
            self.assertIsNone(launcher)

    def test_find_agy_cli(self):
        with patch("shutil.which", return_value="C:\\Fake\\agy.exe"), patch("os.path.isfile", return_value=True):
            cli = antigravity_launcher.find_agy_cli()
            self.assertIsNotNone(cli)
            self.assertIn("agy.exe", cli)

    def test_get_ide_version(self):
        mock_res = MagicMock(returncode=0, stdout="1.107.0\ncommit_hash\nx64\n")
        with patch("subprocess.run", return_value=mock_res):
            ver = antigravity_launcher.get_ide_version("fake-ide")
            self.assertEqual(ver, "1.107.0")

    def test_get_cli_version(self):
        mock_res = MagicMock(returncode=0, stdout="1.5.14\n")
        with patch("subprocess.run", return_value=mock_res):
            ver = antigravity_launcher.get_cli_version("fake-agy")
            self.assertEqual(ver, "1.5.14")

    def test_list_extensions(self):
        mock_res = MagicMock(returncode=0, stdout="ms-python.python\nms-python.debugpy\n")
        with patch("subprocess.run", return_value=mock_res):
            exts = antigravity_launcher.list_extensions("fake-ide")
            self.assertEqual(exts, ["ms-python.python", "ms-python.debugpy"])

    def test_allowed_roots(self):
        user_home = Path.home()
        self.assertTrue(antigravity_launcher.is_allowed_root(user_home / "Projects"))
        self.assertFalse(antigravity_launcher.is_allowed_root(Path(r"C:\Windows\System32\cmd.exe")))
        self.assertFalse(antigravity_launcher.is_allowed_root(Path("/etc/passwd")))

    def test_parse_status_output_clean(self):
        sample_output = """
Version: Antigravity IDE 1.107.0
CPU %	Mem MB	PID	Process
    0	  100	123	antigravity-ide
    0	  200	456	window [1] (Mark-LVI-main - Antigravity IDE - appidentity.py)
Workspace Stats:
|  Window (Mark-LVI-main - Antigravity IDE - appidentity.py)
|    Folder (Mark-LVI-main): 50 files
"""
        st = antigravity_launcher.parse_status_output(sample_output)
        self.assertTrue(st["is_running"])
        self.assertEqual(st["open_folder"], "Mark-LVI-main")
        self.assertEqual(st["active_file"], "appidentity.py")
        self.assertFalse(st["has_unsaved_changes"])

    def test_parse_status_output_dirty(self):
        sample_output = """
window [1] (● appidentity.py - Mark-LVI-main - Antigravity IDE)
"""
        st = antigravity_launcher.parse_status_output(sample_output)
        self.assertTrue(st["is_running"])
        self.assertTrue(st["has_unsaved_changes"])
        self.assertEqual(st["active_file"], "appidentity.py")

    def test_open_folder_argument_construction(self):
        fake_folder = Path.home() / "TestProject"
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_dir", return_value=True),
            patch("actions.antigravity_launcher.is_allowed_root", return_value=True),
            patch("actions.antigravity_launcher.find_antigravity_launcher", return_value="C:\\fake\\antigravity-ide.cmd"),
            patch("actions.antigravity_launcher.get_window_status", return_value={"is_running": True, "open_folder": "TestProject"}),
            patch("subprocess.Popen") as mock_popen,
        ):
            # Test default (reuse window)
            res = antigravity_launcher.open_folder(str(fake_folder), new_window=False)
            self.assertEqual(res.status, outcome.STATUS_DONE)
            mock_popen.assert_called_once()
            args, kwargs = mock_popen.call_args
            self.assertFalse(kwargs.get("shell", True))
            self.assertEqual(args[0], ["C:\\fake\\antigravity-ide.cmd", str(fake_folder.resolve())])

            # Test new window flag
            mock_popen.reset_mock()
            res_new = antigravity_launcher.open_folder(str(fake_folder), new_window=True)
            self.assertEqual(res_new.status, outcome.STATUS_DONE)
            args_new, _ = mock_popen.call_args
            self.assertEqual(args_new[0], ["C:\\fake\\antigravity-ide.cmd", "-n", str(fake_folder.resolve())])

    def test_open_folder_verification_failure_returns_unverified(self):
        fake_folder = Path.home() / "TestProject"
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_dir", return_value=True),
            patch("actions.antigravity_launcher.is_allowed_root", return_value=True),
            patch("actions.antigravity_launcher.find_antigravity_launcher", return_value="C:\\fake\\antigravity-ide.cmd"),
            patch("actions.antigravity_launcher.get_window_status", return_value={"is_running": False, "open_folder": None}),
            patch("subprocess.Popen"),
        ):
            res = antigravity_launcher.open_folder(str(fake_folder), verify_timeout=0.2)
            self.assertEqual(res.status, outcome.STATUS_UNVERIFIED)

    def test_open_folder_outside_allowed_roots(self):
        fake_bad_path = Path(r"C:\Windows\System32")
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_dir", return_value=True),
            patch("actions.antigravity_launcher.is_allowed_root", return_value=False),
        ):
            res = antigravity_launcher.open_folder(str(fake_bad_path))
            self.assertEqual(res.status, outcome.STATUS_BLOCKED)
            self.assertIn("Access denied", res.message)

    def test_open_folder_missing_launcher(self):
        fake_folder = Path.home() / "TestProject"
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_dir", return_value=True),
            patch("actions.antigravity_launcher.is_allowed_root", return_value=True),
            patch("actions.antigravity_launcher.find_antigravity_launcher", return_value=None),
        ):
            res = antigravity_launcher.open_folder(str(fake_folder))
            self.assertEqual(res.status, outcome.STATUS_FAILED)
            self.assertIn("not installed", res.message)

    def test_open_file_with_line_and_column(self):
        fake_file = Path.home() / "TestProject" / "main.py"
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_file", return_value=True),
            patch("actions.antigravity_launcher.is_allowed_root", return_value=True),
            patch("actions.antigravity_launcher.find_antigravity_launcher", return_value="C:\\fake\\antigravity-ide.cmd"),
            patch("subprocess.Popen") as mock_popen,
        ):
            res = antigravity_launcher.open_file(str(fake_file), line=42, column=10)
            self.assertEqual(res.status, outcome.STATUS_DONE)
            mock_popen.assert_called_once()
            args, _ = mock_popen.call_args
            expected_goto = f"{str(fake_file.resolve())}:42:10"
            self.assertEqual(args[0], ["C:\\fake\\antigravity-ide.cmd", "-g", expected_goto])

    def test_open_diff(self):
        file_a = Path.home() / "a.py"
        file_b = Path.home() / "b.py"
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_file", return_value=True),
            patch("actions.antigravity_launcher.is_allowed_root", return_value=True),
            patch("actions.antigravity_launcher.find_antigravity_launcher", return_value="C:\\fake\\antigravity-ide.cmd"),
            patch("subprocess.Popen") as mock_popen,
        ):
            res = antigravity_launcher.open_diff(str(file_a), str(file_b))
            self.assertEqual(res.status, outcome.STATUS_DONE)
            mock_popen.assert_called_once()
            args, _ = mock_popen.call_args
            self.assertEqual(args[0], ["C:\\fake\\antigravity-ide.cmd", "-d", str(file_a.resolve()), str(file_b.resolve())])

    def test_add_folder(self):
        folder = Path.home() / "extra_folder"
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "is_dir", return_value=True),
            patch("actions.antigravity_launcher.is_allowed_root", return_value=True),
            patch("actions.antigravity_launcher.find_antigravity_launcher", return_value="C:\\fake\\antigravity-ide.cmd"),
            patch("subprocess.Popen") as mock_popen,
        ):
            res = antigravity_launcher.add_folder(str(folder))
            self.assertEqual(res.status, outcome.STATUS_DONE)
            mock_popen.assert_called_once()
            args, _ = mock_popen.call_args
            self.assertEqual(args[0], ["C:\\fake\\antigravity-ide.cmd", "-a", str(folder.resolve())])


if __name__ == "__main__":
    unittest.main()
