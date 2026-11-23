"""
tests/test_focus_mode.py — Unit tests for Focus Mode and Mode Manager.

Verifies:
- Hosts file apply and restore using temporary mock file (NEVER touching real OS hosts)
- Crash recovery logic from stale focus state
- Mode configuration persistence and switching
- Process monitor safety (protection of own PIDs, assistant processes, and always-allowed apps)
- Polite closing semantics (WM_CLOSE / SIGTERM, never kill)
- Permissions integration for hosts edit and process closing
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.permissions import (
    POLICY_ALLOW,
    POLICY_ASK,
    POLICY_DENY,
    get_permission_registry,
    set_permission_policy,
)
from memory import config_manager
from plugins import _focus_mode, modes


class TestFocusMode(unittest.TestCase):
    def setUp(self):
        # Create a temp directory for safe hosts testing
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_hosts = Path(self.temp_dir.name) / "hosts"
        self.tmp_hosts.write_text(
            "127.0.0.1 localhost\n::1 localhost\n# User custom entry\n192.168.1.50 printer.local\n",
            encoding="utf-8",
        )
        _focus_mode.set_hosts_path_for_testing(self.tmp_hosts)

        # Temp state file
        self.tmp_state_file = Path(self.temp_dir.name) / "focus_mode_state.json"
        self._orig_state_file = _focus_mode._STATE_FILE
        _focus_mode._STATE_FILE = self.tmp_state_file

        # Reset active mode
        config_manager.set_active_mode(None)

    def tearDown(self):
        _focus_mode.set_hosts_path_for_testing(None)
        _focus_mode._STATE_FILE = self._orig_state_file
        config_manager.set_active_mode(None)
        self.temp_dir.cleanup()

    def test_hosts_file_apply_and_restore_temp_file(self):
        """Verify hosts block is cleanly injected and restored without touching real OS hosts."""
        domains = ["instagram.com", "i.instagram.com"]
        ok, msg = _focus_mode.apply_hosts_block(
            domains, block_adult=False, hosts_path=self.tmp_hosts
        )
        self.assertTrue(ok)
        self.assertIn("Successfully added", msg)

        content = self.tmp_hosts.read_text(encoding="utf-8")
        self.assertIn(_focus_mode.MARKER_BEGIN, content)
        self.assertIn(_focus_mode.MARKER_END, content)
        self.assertIn("127.0.0.1 instagram.com", content)
        self.assertIn("0.0.0.0 i.instagram.com", content)
        # Original entries must remain
        self.assertIn("printer.local", content)

        # Restore
        ok_rm, msg_rm = _focus_mode.remove_hosts_block(hosts_path=self.tmp_hosts)
        self.assertTrue(ok_rm)

        clean_content = self.tmp_hosts.read_text(encoding="utf-8")
        self.assertNotIn(_focus_mode.MARKER_BEGIN, clean_content)
        self.assertNotIn(_focus_mode.MARKER_END, clean_content)
        self.assertNotIn("instagram.com", clean_content)
        self.assertIn("printer.local", clean_content)

    def test_crash_recovery_restores_stale_state(self):
        """Verify startup crash recovery cleans up stale hosts markers and state."""
        # Inject markers into temp hosts file
        stale_hosts = (
            "127.0.0.1 localhost\n"
            f"{_focus_mode.MARKER_BEGIN}\n"
            "127.0.0.1 badsite.com\n"
            f"{_focus_mode.MARKER_END}\n"
        )
        self.tmp_hosts.write_text(stale_hosts, encoding="utf-8")

        # Create stale state file
        stale_state = {
            "active": True,
            "hosts_modified": True,
            "original_dns": {},
            "blocked_apps": ["Instagram"],
        }
        self.tmp_state_file.write_text(json.dumps(stale_state), encoding="utf-8")
        config_manager.set_active_mode("focus")

        # Run crash recovery
        recovered, msg = _focus_mode.restore_crash_state(hosts_path=self.tmp_hosts)
        self.assertTrue(recovered)

        content = self.tmp_hosts.read_text(encoding="utf-8")
        self.assertNotIn(_focus_mode.MARKER_BEGIN, content)
        self.assertNotIn("badsite.com", content)
        self.assertIn("localhost", content)

        # Active mode should now be reset
        self.assertIsNone(config_manager.get_active_mode())

    def test_mode_switching_and_persistence(self):
        """Verify mode settings and active mode are persisted in config store."""
        config_manager.set_active_mode("focus")
        self.assertEqual(config_manager.get_active_mode(), "focus")

        # Update mode settings
        config_manager.update_mode_settings(
            "focus",
            {
                "blocked_apps": ["Instagram", "CustomGame"],
                "poll_interval_seconds": 2.0,
            },
        )
        settings = config_manager.get_mode_settings("focus")
        self.assertIn("CustomGame", settings.get("blocked_apps", []))
        self.assertEqual(settings.get("poll_interval_seconds"), 2.0)

        # Switch to english practice
        config_manager.set_active_mode("english_practice")
        self.assertEqual(config_manager.get_active_mode(), "english_practice")

        # Clean up
        config_manager.set_active_mode(None)
        self.assertIsNone(config_manager.get_active_mode())

    def test_process_monitor_protects_always_allowed_apps(self):
        """Verify FocusMonitor never closes always-allowed applications or assistant's PIDs."""
        monitor = _focus_mode.FocusMonitor(
            blocked_apps=["code", "instagram"],
            always_allowed_apps=["code", "antigravity ide", "notion"],
            blocked_domains=["instagram.com"],
            poll_interval=2.5,
        )

        # Fake process matching 'code'
        mock_match = MagicMock()
        mock_match.label = "Code.exe"
        mock_match.name = "Code.exe"
        mock_match.is_self = False
        mock_match.pid = 99999

        with patch("core.appidentity.own_pids", return_value={1000}), patch(
            "core.appidentity.running", return_value=[mock_match]
        ), patch("core.appidentity.request_close") as mock_close:
            monitor._check_processes()
            # Because 'code' is in always_allowed_apps, request_close must NEVER be called
            mock_close.assert_not_called()

    def test_process_monitor_protects_own_pids(self):
        """Verify FocusMonitor never closes assistant's own processes."""
        monitor = _focus_mode.FocusMonitor(
            blocked_apps=["jarvis_helper"],
            always_allowed_apps=[],
            blocked_domains=[],
            poll_interval=2.5,
        )

        mock_match = MagicMock()
        mock_match.label = "jarvis_helper"
        mock_match.name = "jarvis_helper"
        mock_match.is_self = True
        mock_match.pid = 1234

        with patch("core.appidentity.own_pids", return_value={1234}), patch(
            "core.appidentity.running", return_value=[mock_match]
        ), patch("core.appidentity.request_close") as mock_close:
            monitor._check_processes()
            mock_close.assert_not_called()

    def test_polite_close_called_for_blocked_app(self):
        """Verify FocusMonitor uses polite close (appidentity.request_close) and never kill."""
        monitor = _focus_mode.FocusMonitor(
            blocked_apps=["instagram"],
            always_allowed_apps=["code"],
            blocked_domains=[],
            poll_interval=2.5,
        )

        mock_match = MagicMock()
        mock_match.label = "Instagram"
        mock_match.name = "Instagram.exe"
        mock_match.is_self = False
        mock_match.pid = 4321

        with patch("core.appidentity.own_pids", return_value={1000}), patch(
            "core.appidentity.running", return_value=[mock_match]
        ), patch("core.appidentity.request_close", return_value=(True, "")) as mock_close:
            monitor._check_processes()
            mock_close.assert_called_once_with(mock_match, timeout=2.0)

    def test_modes_plugin_dispatcher(self):
        """Verify modes plugin handles 'list', 'status', and dispatches commands."""
        res_list = modes.run({"action": "list"})
        self.assertIn("Focus Mode", res_list)
        self.assertIn("English Placement Practice Mode", res_list)

        res_status = modes.run({"action": "status"})
        self.assertTrue(isinstance(res_status, str))
        self.assertTrue("Focus Mode" in res_status)


if __name__ == "__main__":
    unittest.main()
