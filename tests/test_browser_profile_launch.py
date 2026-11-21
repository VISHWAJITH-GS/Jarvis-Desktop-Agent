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

    def test_08_no_focus_stealing_in_background_mode(self):
        """Verify switch_tab does not call bring_to_front when background_mode is active."""
        mgr = BrowserSessionManager(cdp_url="http://127.0.0.1:9222", engine="chrome")
        mgr.background_mode = True

        mock_page_0 = MagicMock()
        mock_page_0.title = MagicMock(return_value="Tab 0")
        mock_page_1 = MagicMock()
        mock_page_1.title = MagicMock(return_value="Tab 1")
        mock_page_1.url = "https://example.com/tab1"

        async def fake_bring_to_front():
            pass

        async def fake_title():
            return "Tab 1"

        mock_page_1.bring_to_front = MagicMock(side_effect=fake_bring_to_front)
        mock_page_1.title = fake_title

        mock_context = MagicMock()
        mock_context.pages = [mock_page_0, mock_page_1]
        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True

        mgr._browser = mock_browser
        mgr._context = mock_context
        mgr._active_page = mock_page_0

        with patch.object(mgr, "_ensure_attached", return_value=None):
            tab = mgr.switch_tab(1)
            self.assertEqual(tab.index, 1)
            # bring_to_front MUST NOT have been called in background mode!
            mock_page_1.bring_to_front.assert_not_called()

    def test_09_signed_out_detection(self):
        """Verify accurate signed-out state detection across web services."""
        from actions.signed_out_detector import check_signed_out_status

        # Gmail
        gmail_out = check_signed_out_status("https://accounts.google.com/signin/v2/identifier", "Sign in - Google Accounts")
        self.assertIsNotNone(gmail_out)
        self.assertIn("Gmail login expired", gmail_out)

        # WhatsApp Web
        wa_out = check_signed_out_status("https://web.whatsapp.com", html_text="To use WhatsApp on your computer, open WhatsApp")
        self.assertIsNotNone(wa_out)
        self.assertIn("WhatsApp Web session expired or unlinked", wa_out)

        # Spotify
        spotify_out = check_signed_out_status("https://open.spotify.com/login", "Log in to Spotify")
        self.assertIsNotNone(spotify_out)
        self.assertIn("Spotify Web login expired", spotify_out)

        # Logged in page should return None
        logged_in = check_signed_out_status("https://mail.google.com/mail/u/0/#inbox", "Inbox (12) - test@gmail.com")
        self.assertIsNone(logged_in)

    def test_10_failure_recovery_classification(self):
        """Verify browser failure classifier provides specific cause and next steps."""
        from actions.browser_session import classify_browser_failure

        f_exec = classify_browser_failure("Could not locate executable for browser engine 'chrome'")
        self.assertEqual(f_exec["category"], "chrome_not_installed")
        self.assertIn("Google Chrome", f_exec["user_action"])

        f_def = classify_browser_failure("Refusing to launch against user's default browser data directory")
        self.assertEqual(f_def["category"], "daily_profile_blocked")

        f_lock = classify_browser_failure("SingletonLock failed: profile in use by another instance")
        self.assertEqual(f_lock["category"], "profile_locked")

        f_cdp = classify_browser_failure("connect ECONNREFUSED 127.0.0.1:9222")
        self.assertEqual(f_cdp["category"], "cdp_timeout")

    def test_11_gmail_read_only_and_financial_safety(self):
        """Verify Gmail write actions are forbidden and financial checkout requires HIGH_IMPACT."""
        from core.permissions import ActionPermissionManager, ActionRiskLevel

        mgr = ActionPermissionManager()

        # 1. Financial page requires HIGH_IMPACT regardless of sub-action
        fin_risk = mgr.get_risk_level(
            "browser_control",
            {"action": "click", "url": "https://store.example.com/checkout/payment"},
        )
        self.assertEqual(fin_risk, ActionRiskLevel.HIGH_IMPACT)

        # 2. Gmail read-only guard blocks compose/send/delete
        gmail_send_risk = mgr.get_risk_level(
            "browser_control",
            {"action": "click", "url": "https://mail.google.com/mail/u/0/", "text": "Send"},
        )
        self.assertEqual(gmail_send_risk, ActionRiskLevel.FORBIDDEN)

        gmail_del_risk = mgr.get_risk_level(
            "browser_control",
            {"action": "click", "url": "https://mail.google.com/mail/u/0/", "text": "Delete message"},
        )
        self.assertEqual(gmail_del_risk, ActionRiskLevel.FORBIDDEN)

        # 3. Reading/observing Gmail is permitted
        gmail_read_risk = mgr.get_risk_level(
            "browser_control",
            {"action": "observe", "url": "https://mail.google.com/mail/u/0/"},
        )
        self.assertEqual(gmail_read_risk, ActionRiskLevel.IMMEDIATE)


if __name__ == "__main__":
    unittest.main()
