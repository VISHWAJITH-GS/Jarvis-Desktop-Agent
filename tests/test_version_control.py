"""
tests/test_version_control.py — Unit tests for Git/GitHub version control operations and tool declaration.
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from actions import version_control
from core import outcome


class TestVersionControl(unittest.TestCase):
    def test_tool_declaration_valid(self):
        self.assertIn("name", version_control.TOOL)
        self.assertEqual(version_control.TOOL["name"], "version_control")
        self.assertEqual(version_control.TOOL["parameters"]["type"], "OBJECT")
        self.assertIn("action", version_control.TOOL["parameters"]["required"])

    def test_git_status_clean_and_modified(self):
        # 1. Clean
        mock_clean = MagicMock(returncode=0, stdout="## main...origin/main\n", stderr="")
        with (
            patch("actions.version_control._resolve_repo_dir", return_value=(Path("D:/repo"), None)),
            patch("subprocess.run", return_value=mock_clean),
        ):
            res = version_control.git_status()
            self.assertEqual(res.status, outcome.STATUS_DONE)
            self.assertTrue(res.data["clean"])

        # 2. Modified
        mock_dirty = MagicMock(
            returncode=0,
            stdout="## main\nM  actions/test.py\n?? untracked.txt\n",
            stderr="",
        )
        with (
            patch("actions.version_control._resolve_repo_dir", return_value=(Path("D:/repo"), None)),
            patch("subprocess.run", return_value=mock_dirty),
        ):
            res_dirty = version_control.git_status()
            self.assertEqual(res_dirty.status, outcome.STATUS_DONE)
            self.assertFalse(res_dirty.data["clean"])
            self.assertEqual(len(res_dirty.data["staged"]), 1)
            self.assertEqual(len(res_dirty.data["untracked"]), 1)

    def test_git_commit_success(self):
        mock_add = MagicMock(returncode=0, stdout="", stderr="")
        mock_diff = MagicMock(returncode=0, stdout="actions/test.py\n", stderr="")
        mock_commit = MagicMock(returncode=0, stdout="[main 1234567] Commit msg\n", stderr="")
        mock_sha = MagicMock(returncode=0, stdout="1234567\n", stderr="")

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return mock_add
            if args[:3] == ["git", "diff", "--cached"]:
                return mock_diff
            if args[:2] == ["git", "commit"]:
                return mock_commit
            if args[:3] == ["git", "rev-parse", "--short"]:
                return mock_sha
            return MagicMock(returncode=0, stdout="")

        with (
            patch("actions.version_control._resolve_repo_dir", return_value=(Path("D:/repo"), None)),
            patch("subprocess.run", side_effect=fake_run),
        ):
            res = version_control.git_commit("Add feature test")
            self.assertEqual(res.status, outcome.STATUS_DONE)
            self.assertTrue(res.verified)
            self.assertEqual(res.data["commit_sha"], "1234567")
            self.assertIn("1234567", res.message)

    def test_git_commit_empty_working_tree(self):
        mock_add = MagicMock(returncode=0, stdout="", stderr="")
        mock_diff = MagicMock(returncode=0, stdout="", stderr="")  # Nothing staged

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "add"]:
                return mock_add
            if args[:3] == ["git", "diff", "--cached"]:
                return mock_diff
            return MagicMock(returncode=0, stdout="")

        with (
            patch("actions.version_control._resolve_repo_dir", return_value=(Path("D:/repo"), None)),
            patch("subprocess.run", side_effect=fake_run),
        ):
            res = version_control.git_commit("No changes here")
            self.assertEqual(res.status, outcome.STATUS_DONE)
            self.assertFalse(res.data["committed"])
            self.assertIn("clean", res.message)

    def test_git_commit_empty_message_error(self):
        res = version_control.git_commit("")
        self.assertEqual(res.status, outcome.STATUS_FAILED)
        self.assertIn("empty", res.message.lower())

    def test_git_push_success(self):
        mock_br = MagicMock(returncode=0, stdout="feature-branch\n", stderr="")
        mock_push = MagicMock(returncode=0, stdout="Everything up-to-date\n", stderr="")

        def fake_run(args, **kwargs):
            if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return mock_br
            if args[:2] == ["git", "push"]:
                return mock_push
            return MagicMock(returncode=0, stdout="")

        with (
            patch("actions.version_control._resolve_repo_dir", return_value=(Path("D:/repo"), None)),
            patch("subprocess.run", side_effect=fake_run),
        ):
            res = version_control.git_push(remote="origin")
            self.assertEqual(res.status, outcome.STATUS_DONE)
            self.assertTrue(res.verified)
            self.assertEqual(res.data["branch"], "feature-branch")
            self.assertIn("feature-branch", res.message)

    def test_git_push_failure_capture(self):
        mock_br = MagicMock(returncode=0, stdout="main\n", stderr="")
        mock_push = MagicMock(returncode=1, stdout="", stderr="error: failed to push some refs to remote\n")

        def fake_run(args, **kwargs):
            if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return mock_br
            if args[:2] == ["git", "push"]:
                return mock_push
            return MagicMock(returncode=0, stdout="")

        with (
            patch("actions.version_control._resolve_repo_dir", return_value=(Path("D:/repo"), None)),
            patch("subprocess.run", side_effect=fake_run),
        ):
            res = version_control.git_push(remote="origin")
            self.assertEqual(res.status, outcome.STATUS_FAILED)
            self.assertIn("failed to push", res.message)

    def test_git_push_timeout(self):
        mock_br = MagicMock(returncode=0, stdout="main\n", stderr="")
        with (
            patch("actions.version_control._resolve_repo_dir", return_value=(Path("D:/repo"), None)),
            patch("subprocess.run", side_effect=[mock_br, subprocess.TimeoutExpired(cmd="git push", timeout=45)]),
        ):
            res = version_control.git_push()
            self.assertEqual(res.status, outcome.STATUS_FAILED)
            self.assertIn("timed out", res.message)

    def test_git_pull_success(self):
        mock_pull = MagicMock(returncode=0, stdout="Already up to date.\n", stderr="")
        with (
            patch("actions.version_control._resolve_repo_dir", return_value=(Path("D:/repo"), None)),
            patch("subprocess.run", return_value=mock_pull),
        ):
            res = version_control.git_pull()
            self.assertEqual(res.status, outcome.STATUS_DONE)
            self.assertIn("Already up to date", res.message)

    def test_not_a_git_repo_fails(self):
        with patch("actions.version_control.is_allowed_root", return_value=True), patch("subprocess.run", return_value=MagicMock(returncode=128, stdout="fatal: not a git repository")):
            res = version_control.git_status()
            self.assertEqual(res.status, outcome.STATUS_FAILED)
            self.assertIn("not a Git repository", res.message)

    def test_version_control_dispatch(self):
        with patch("actions.version_control.git_status", return_value=outcome.ActionResult(
            status=outcome.STATUS_DONE, action="git_status", success=True, verified=True, message="Status ok"
        )) as mock_st:
            res = version_control.version_control({"action": "status"})
            self.assertEqual(res.status, outcome.STATUS_DONE)
            mock_st.assert_called_once()


if __name__ == "__main__":
    unittest.main()
