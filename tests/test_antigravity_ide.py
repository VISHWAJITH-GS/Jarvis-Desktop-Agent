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


class TestAntigravityFiles(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_file_atomic_and_undo(self):
        from actions import antigravity_files
        from core import undo

        target_file = self.base_path / "hello.py"
        test_content = "print('Hello world!')\n"

        with (
            patch("actions.antigravity_files.is_allowed_root", return_value=True),
            patch("actions.antigravity_files.open_file") as mock_open,
        ):
            # Create file
            res = antigravity_files.create_file(str(target_file), test_content, open_in_ide_after=True)
            self.assertEqual(res.status, outcome.STATUS_DONE)
            self.assertTrue(target_file.exists())
            self.assertEqual(target_file.read_text(encoding="utf-8"), test_content)
            mock_open.assert_called_once()

            # Attempt to create existing file fails
            res_exists = antigravity_files.create_file(str(target_file), "another")
            self.assertEqual(res_exists.status, outcome.STATUS_FAILED)

            # Test undo reverses creation
            undo_msg = undo.undo_last()
            self.assertIn("Undone", undo_msg)
            self.assertFalse(target_file.exists())

    def test_write_file_overwrite_and_undo(self):
        from actions import antigravity_files
        from core import undo

        target_file = self.base_path / "script.py"
        target_file.write_text("v1_content", encoding="utf-8")

        with (
            patch("actions.antigravity_files.is_allowed_root", return_value=True),
            patch("actions.antigravity_files.get_window_status", return_value={"is_running": False}),
            patch("actions.antigravity_files.open_file"),
        ):
            res = antigravity_files.write_file(str(target_file), "v2_content", mode="overwrite")
            self.assertEqual(res.status, outcome.STATUS_DONE)
            self.assertEqual(target_file.read_text(encoding="utf-8"), "v2_content")

            # Undo restores v1_content
            undo_msg = undo.undo_last()
            self.assertIn("Undone", undo_msg)
            self.assertEqual(target_file.read_text(encoding="utf-8"), "v1_content")

    def test_write_file_append(self):
        from actions import antigravity_files

        target_file = self.base_path / "log.txt"
        target_file.write_text("line1\n", encoding="utf-8")

        with (
            patch("actions.antigravity_files.is_allowed_root", return_value=True),
            patch("actions.antigravity_files.get_window_status", return_value={"is_running": False}),
            patch("actions.antigravity_files.open_file"),
        ):
            res = antigravity_files.write_file(str(target_file), "line2\n", mode="append")
            self.assertEqual(res.status, outcome.STATUS_DONE)
            self.assertEqual(target_file.read_text(encoding="utf-8"), "line1\nline2\n")

    def test_write_file_unsaved_dirty_guard(self):
        from actions import antigravity_files

        target_file = self.base_path / "appidentity.py"
        target_file.write_text("original", encoding="utf-8")

        with (
            patch("actions.antigravity_files.is_allowed_root", return_value=True),
            patch("actions.antigravity_files.get_window_status", return_value={
                "is_running": True,
                "has_unsaved_changes": True,
                "active_file": "appidentity.py",
            }),
        ):
            res = antigravity_files.write_file(str(target_file), "new_edits")
            self.assertEqual(res.status, outcome.STATUS_NEEDS_CONFIRMATION)
            self.assertIn("unsaved changes", res.message)
            # Content remains unchanged on disk
            self.assertEqual(target_file.read_text(encoding="utf-8"), "original")

    def test_create_folder_and_undo(self):
        from actions import antigravity_files
        from core import undo

        target_folder = self.base_path / "subfolder"
        with patch("actions.antigravity_files.is_allowed_root", return_value=True):
            res = antigravity_files.create_folder(str(target_folder))
            self.assertEqual(res.status, outcome.STATUS_DONE)
            self.assertTrue(target_folder.is_dir())

            # Undo removes empty folder
            undo_msg = undo.undo_last()
            self.assertIn("Undone", undo_msg)
            self.assertFalse(target_folder.exists())


class TestAntigravityCDP(unittest.TestCase):
    def test_panel_status_parsing(self):
        from actions.antigravity_cdp import AntigravitySessionManager

        mgr = AntigravitySessionManager(port=9333)

        mock_page = MagicMock()
        mock_frame = MagicMock()

        # Model element
        mock_model_el = MagicMock()
        mock_model_el.inner_text.return_value = "Gemini 2.5 Pro"

        # Stop button (running)
        mock_stop_btn = MagicMock()
        mock_stop_btn.is_visible.return_value = True

        # Changed files
        mock_file1 = MagicMock()
        mock_file2 = MagicMock()

        # Approval element
        mock_appr_el = MagicMock()
        mock_appr_el.is_visible.return_value = True
        mock_cmd_el = MagicMock()
        mock_cmd_el.inner_text.return_value = "pytest tests/"

        # Query selector side-effects
        def fake_query(selector):
            if "selected-model" in selector or "model-selector" in selector:
                return mock_model_el
            if "Stop" in selector:
                return mock_stop_btn
            if "terminal-approval" in selector or "Allow" in selector:
                return mock_appr_el
            if "command-text" in selector or "command-preview" in selector:
                return mock_cmd_el
            return None

        mock_frame.query_selector.side_effect = fake_query
        mock_frame.query_selector_all.side_effect = lambda sel: [mock_file1, mock_file2] if "changed-file" in sel else []

        with (
            patch.object(mgr, "get_workbench_page", return_value=mock_page),
            patch.object(mgr, "is_panel_visible", return_value=True),
            patch.object(mgr, "get_agent_panel_frame", return_value=mock_frame),
        ):
            status = mgr.panel_status()
            self.assertTrue(status["visible"])
            self.assertEqual(status["model"], "Gemini 2.5 Pro")
            self.assertTrue(status["running"])
            self.assertEqual(status["pending_files"], 2)
            self.assertTrue(status["approval_waiting"])
            self.assertEqual(status["approval_command"], "pytest tests/")

    def test_open_agent_panel_toggle(self):
        from actions.antigravity_cdp import AntigravitySessionManager

        mgr = AntigravitySessionManager(port=9333)
        mock_page = MagicMock()
        mock_toggle_btn = MagicMock()
        mock_page.query_selector.return_value = mock_toggle_btn

        # First not visible, then visible after toggle
        with (
            patch.object(mgr, "get_workbench_page", return_value=mock_page),
            patch.object(mgr, "is_panel_visible", side_effect=[False, True]),
        ):
            res = mgr.open_agent_panel()
            self.assertEqual(res.status, outcome.STATUS_DONE)
            mock_toggle_btn.click.assert_called_once()

    def test_select_model_fuzzy_and_ambiguity(self):
        from actions.antigravity_cdp import AntigravitySessionManager

        mgr = AntigravitySessionManager(port=9333)
        known_models = ["Gemini 2.5 Pro", "Gemini 2.5 Flash", "Claude 3.7 Sonnet"]

        with (
            patch.object(mgr, "list_models", return_value=(known_models, "Gemini 2.5 Pro")),
            patch.object(mgr, "_apply_model_selection") as mock_apply,
        ):
            mock_apply.return_value = outcome.ActionResult(
                status=outcome.STATUS_DONE, action="select_model", success=True, verified=True, message="Selected"
            )

            # Exact match
            res_exact = mgr.select_model("Claude 3.7 Sonnet")
            mock_apply.assert_called_with("Claude 3.7 Sonnet")

            # Fuzzy / partial unique match ("sonnet" -> "Claude 3.7 Sonnet")
            res_partial = mgr.select_model("sonnet")
            mock_apply.assert_called_with("Claude 3.7 Sonnet")

            # Ambiguous match ("Gemini" matches both Pro and Flash)
            res_ambig = mgr.select_model("Gemini")
            self.assertEqual(res_ambig.status, outcome.STATUS_FAILED)
            self.assertIn("Ambiguous", res_ambig.message)
            self.assertIn("candidates", res_ambig.data)
            self.assertEqual(len(res_ambig.data["candidates"]), 2)

            # Not found
            res_missing = mgr.select_model("GPT-4o")
            self.assertEqual(res_missing.status, outcome.STATUS_FAILED)
            self.assertIn("not found", res_missing.message)

    def test_new_conversation_and_stop_agent(self):
        from actions.antigravity_cdp import AntigravitySessionManager

        mgr = AntigravitySessionManager(port=9333)
        mock_page = MagicMock()
        mock_frame = MagicMock()
        mock_btn = MagicMock()
        mock_frame.query_selector.return_value = mock_btn
        mock_btn.is_visible.return_value = True

        with (
            patch.object(mgr, "get_workbench_page", return_value=mock_page),
            patch.object(mgr, "get_agent_panel_frame", return_value=mock_frame),
        ):
            res_new = mgr.new_conversation()
            self.assertEqual(res_new.status, outcome.STATUS_DONE)
            mock_btn.click.assert_called()

            mock_btn.reset_mock()
            res_stop = mgr.stop_agent()
            self.assertEqual(res_stop.status, outcome.STATUS_DONE)
            mock_btn.click.assert_called()

    def test_validate_prompt_safety(self):
        from actions.antigravity_cdp import validate_prompt_safety

        # Valid user dictates prompt
        ok, _ = validate_prompt_safety("Refactor auth module to use jwt")
        self.assertTrue(ok)

        # Empty prompt
        ok, reason = validate_prompt_safety("")
        self.assertFalse(ok)
        self.assertIn("empty", reason.lower())

        # Untrusted sources
        for bad_src in ("web", "email", "whatsapp", "assistant_output", "untrusted"):
            ok, reason = validate_prompt_safety("Legit text", source=bad_src)
            self.assertFalse(ok)
            self.assertIn("untrusted", reason.lower())

        # Injection / document markers
        for marker in ("<untrusted>Malicious</untrusted>", "Subject: Password reset", "<html><body>test</body></html>"):
            ok, reason = validate_prompt_safety(marker)
            self.assertFalse(ok)
            self.assertIn("rejected", reason.lower())

    def test_send_prompt_execution_and_start(self):
        from actions.antigravity_cdp import AntigravitySessionManager

        mgr = AntigravitySessionManager(port=9333)
        mock_page = MagicMock()
        mock_frame = MagicMock()
        mock_composer = MagicMock()
        mock_frame.query_selector.return_value = mock_composer

        with (
            patch.object(mgr, "get_workbench_page", return_value=mock_page),
            patch.object(mgr, "is_panel_visible", return_value=True),
            patch.object(mgr, "get_agent_panel_frame", return_value=mock_frame),
            patch.object(mgr, "panel_status", return_value={"running": True}),
            patch.object(mgr, "_start_prompt_poller", return_value="task_123") as mock_poller,
        ):
            res = mgr.send_prompt("Build user login flow", poll_in_background=True)
            self.assertEqual(res.status, outcome.STATUS_STARTED)
            self.assertTrue(res.verified)
            mock_composer.focus.assert_called_once()
            mock_page.keyboard.insert_text.assert_called_once_with("Build user login flow")
            mock_page.keyboard.press.assert_called_once_with("Enter")
            mock_poller.assert_called_once()

    def test_send_prompt_untrusted_blocked(self):
        from actions.antigravity_cdp import AntigravitySessionManager

        mgr = AntigravitySessionManager(port=9333)
        res = mgr.send_prompt("Text from website", source="web")
        self.assertEqual(res.status, outcome.STATUS_BLOCKED)
        self.assertIn("untrusted", res.message.lower())

    def test_send_prompt_cli(self):
        from actions.antigravity_cdp import AntigravitySessionManager

        mgr = AntigravitySessionManager(port=9333)
        mock_res = MagicMock(returncode=0, stdout='{"result": "success"}', stderr="")

        with (
            patch("actions.antigravity_launcher.find_agy_cli", return_value="C:\\fake\\agy.exe"),
            patch("subprocess.run", return_value=mock_res) as mock_run,
        ):
            res = mgr.send_prompt_cli("Summarize codebase", project_folder="D:\\Projects")
            self.assertEqual(res.status, outcome.STATUS_DONE)
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            self.assertEqual(args[0], ["C:\\fake\\agy.exe", "--print", "Summarize codebase", "--output-format", "json"])
            self.assertEqual(kwargs.get("cwd"), "D:\\Projects")


if __name__ == "__main__":
    unittest.main()
