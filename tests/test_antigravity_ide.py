"""
tests/test_antigravity_ide.py — Unit and fixture tests for Antigravity IDE integration.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from actions import antigravity_launcher


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


if __name__ == "__main__":
    unittest.main()
