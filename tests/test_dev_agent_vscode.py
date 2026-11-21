"""
tests/test_dev_agent_vscode.py — Unit Tests for VS Code Launch Security in dev_agent
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from actions import dev_agent


class TestDevAgentVSCode(unittest.TestCase):
    def test_open_vscode_uses_shutil_which_and_shell_false(self):
        """Verify _open_vscode resolves via shutil.which and launches with shell=False."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # process is running happily

        with (
            patch("shutil.which", return_value="C:\\Program Files\\Microsoft VS Code\\bin\\code.cmd") as mock_which,
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("time.sleep"),
        ):
            test_dir = Path("D:/test_project")
            success = dev_agent._open_vscode(test_dir)

            self.assertTrue(success)
            mock_which.assert_called()

            # Ensure Popen was called with shell=False and argument list
            mock_popen.assert_called_once()
            args, kwargs = mock_popen.call_args
            self.assertEqual(kwargs.get("shell"), False)
            self.assertEqual(kwargs.get("stdout"), subprocess.DEVNULL)
            self.assertEqual(kwargs.get("stderr"), subprocess.DEVNULL)
            self.assertIn("code.cmd", args[0][0])
            self.assertEqual(args[0][1], str(test_dir))

    def test_open_vscode_fails_gracefully_when_not_found(self):
        """Verify _open_vscode returns False if executable is not found."""
        with patch("shutil.which", return_value=None):
            success = dev_agent._open_vscode(Path("D:/test_project"))
            self.assertFalse(success)


if __name__ == "__main__":
    unittest.main()
