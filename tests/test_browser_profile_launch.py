"""
tests/test_browser_profile_launch.py — Unit tests for dedicated Jarvis browser profile,
configuration, launch flags, identity verification, default-profile refusal, and attach semantics.
"""

from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from actions.browser_session import (
    DEFAULT_CHROME_PORT,
    DEFAULT_EDGE_PORT,
    BrowserSessionManager,
    CDPConnectionError,
    build_browser_launch_command,
    get_default_cdp_port,
    get_default_cdp_url,
    locate_browser_executable,
    verify_browser_identity,
)
from memory import config_manager


class TestBrowserProfileLaunch(unittest.TestCase):
    def test_01_profile_dir_defaults_per_os(self):
        """Verify sensible dedicated profile defaults per OS for Chrome and Edge."""
        with patch("platform.system", return_value="Windows"), patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\Test\AppData\Local"}):
            chrome_win = config_manager.get_default_browser_profile_dir("chrome")
            self.assertEqual(chrome_win, Path(r"C:\Users\Test\AppData\Local\Google\Chrome\JarvisProfile"))

            edge_win = config_manager.get_default_browser_profile_dir("edge")
            self.assertEqual(edge_win, Path(r"C:\Users\Test\AppData\Local\Microsoft\Edge\JarvisProfile"))

        with patch("platform.system", return_value="Darwin"), patch.object(Path, "home", return_value=Path("/Users/test")):
            chrome_mac = config_manager.get_default_browser_profile_dir("chrome")
            self.assertEqual(chrome_mac, Path("/Users/test/Library/Application Support/Google/Chrome-Jarvis"))

            edge_mac = config_manager.get_default_browser_profile_dir("edge")
            self.assertEqual(edge_mac, Path("/Users/test/Library/Application Support/Microsoft Edge-Jarvis"))

        with patch("platform.system", return_value="Linux"), patch.object(Path, "home", return_value=Path("/home/test")):
            chrome_linux = config_manager.get_default_browser_profile_dir("chrome")
            self.assertEqual(chrome_linux, Path("/home/test/.config/google-chrome-jarvis"))

            edge_linux = config_manager.get_default_browser_profile_dir("edge")
            self.assertEqual(edge_linux, Path("/home/test/.config/microsoft-edge-jarvis"))

    def test_02_default_profile_refusal(self):
        """Verify that attempting to configure or launch against default user data dir is strictly refused."""
        with patch("platform.system", return_value="Windows"), patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\Test\AppData\Local"}):
            default_dir = Path(r"C:\Users\Test\AppData\Local\Google\Chrome\User Data")
            valid, err = config_manager.validate_browser_profile_dir(default_dir, engine="chrome")
            self.assertFalse(valid)
            self.assertIn("Refusing to launch against user's default browser data directory", err)

            # Test Default profile under User Data
            valid_def, err_def = config_manager.validate_browser_profile_dir(default_dir / "Default", engine="chrome")
            self.assertFalse(valid_def)
            self.assertIn("Refusing to launch", err_def)

            # Valid dedicated dir passes
            dedicated_dir = Path(r"C:\Users\Test\AppData\Local\Google\Chrome\JarvisProfile")
            valid_ok, err_ok = config_manager.validate_browser_profile_dir(dedicated_dir, engine="chrome")
            self.assertTrue(valid_ok)
            self.assertEqual(err_ok, "")

    def test_03_port_separation_and_no_collision(self):
        """Verify Chrome and Edge use distinct CDP ports so they never collide."""
        self.assertEqual(get_default_cdp_port("chrome"), 9222)
        self.assertEqual(get_default_cdp_port("edge"), 9223)
        self.assertNotEqual(get_default_cdp_port("chrome"), get_default_cdp_port("edge"))

        self.assertEqual(get_default_cdp_url("chrome"), "http://127.0.0.1:9222")
        self.assertEqual(get_default_cdp_url("edge"), "http://127.0.0.1:9223")

    def test_04_launch_flags_and_security(self):
        """Verify launch flags bind to 127.0.0.1 strictly and eliminate wildcard origin."""
        profile = Path(r"C:\TestJarvis\JarvisProfile")
        with patch("memory.config_manager.validate_browser_profile_dir", return_value=(True, "")):
            cmd = build_browser_launch_command(
                exe=r"C:\chrome.exe",
                profile_dir=profile,
                port=9222,
                engine="chrome",
                headless=False,
            )

            # Check required flags
            self.assertIn("--remote-debugging-port=9222", cmd)
            self.assertIn("--remote-debugging-address=127.0.0.1", cmd)
            self.assertIn(f"--user-data-dir={profile.resolve()}", cmd)
            self.assertIn("--no-first-run", cmd)
            self.assertIn("--no-default-browser-check", cmd)
            self.assertIn("--profile-directory=Default", cmd)

            # Ensure NO wildcard origin
            self.assertNotIn("--remote-allow-origins=*", cmd)
            # Ensure restricted origin
            self.assertIn("--remote-allow-origins=http://127.0.0.1:9222,http://localhost:9222", cmd)

    def test_05_refusal_when_building_launch_cmd_for_default_dir(self):
        """Verify build_browser_launch_command raises ValueError when given a default profile dir."""
        default_dir = Path(r"C:\Users\Test\AppData\Local\Google\Chrome\User Data")
        with patch("memory.config_manager.validate_browser_profile_dir", return_value=(False, "Refusing default dir")):
            with self.assertRaises(ValueError) as ctx:
                build_browser_launch_command(
                    exe=r"C:\chrome.exe",
                    profile_dir=default_dir,
                    port=9222,
                    engine="chrome",
                )
            self.assertIn("Refusing default dir", str(ctx.exception))

    def test_06_identity_verification(self):
        """Verify identity verification accepts matching dedicated profile and refuses wrong/default profile."""
        dedicated_dir = Path(r"C:\Jarvis\ChromeProfile")

        # Case 1: Matching process cmdline
        mock_proc_ok = MagicMock()
        mock_proc_ok.pid = 1234
        mock_proc_ok.info = {
            "cmdline": [
                "chrome.exe",
                "--remote-debugging-port=9222",
                f"--user-data-dir={dedicated_dir.resolve()}",
            ]
        }

        with patch("psutil.process_iter", return_value=[mock_proc_ok]):
            verified, msg = verify_browser_identity(9222, dedicated_dir, engine="chrome")
            self.assertTrue(verified)
            self.assertIn("Verified process 1234", msg)

        # Case 2: Process running on port 9222 with WRONG profile dir
        mock_proc_wrong = MagicMock()
        mock_proc_wrong.pid = 5678
        mock_proc_wrong.info = {
            "cmdline": [
                "chrome.exe",
                "--remote-debugging-port=9222",
                "--user-data-dir=C:\\SomeOtherProfile",
            ]
        }

        with patch("psutil.process_iter", return_value=[mock_proc_wrong]):
            verified, msg = verify_browser_identity(9222, dedicated_dir, engine="chrome")
            self.assertFalse(verified)
            self.assertIn("does not match expected Jarvis profile", msg)

    def test_07_attach_instead_of_launch_when_already_running(self):
        """Verify that when browser is already connected and verified, attach does not re-launch."""
        mgr = BrowserSessionManager(cdp_url="http://127.0.0.1:9222", engine="chrome")

        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_context = MagicMock()
        mock_context.pages = [mock_page]
        mock_browser.contexts = [mock_context]

        mgr._browser = mock_browser
        mgr._context = mock_context
        mgr._active_page = mock_page

        with patch.object(mgr, "check_connection", return_value=(True, "Connected to Chrome")), \
             patch.object(mgr, "verify_profile_identity", return_value=(True, "Verified")), \
             patch.object(mgr, "_try_auto_launch") as mock_launch:

            mgr.run_coroutine(mgr._ensure_attached())
            # Should NOT attempt to auto-launch because it was already running and verified!
            mock_launch.assert_not_called()
            self.assertEqual(mgr.active_page, mock_page)


if __name__ == "__main__":
    unittest.main()
