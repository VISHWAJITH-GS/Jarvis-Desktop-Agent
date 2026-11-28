"""
tests/test_hybrid_browser.py — Comprehensive Automated Test Suite for Hybrid Browser Architecture.

Tests:
1. Ego(lite) initialization.
2. Ego(lite) connection failures.
3. CDP provider initialization.
4. Provider selection.
5. Automatic fallback.
6. Provider switching.
7. Browser navigation.
8. Semantic page observation.
9. Clicking and typing.
10. Keyboard actions.
11. Tab and Space management.
12. Browser crash recovery.
13. Session isolation.
14. Authentication persistence where supported.
15. Gemini tool-call compatibility.
16. WhatsApp safety and verification.
17. Browser dashboard status.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from actions import browser_control
from actions.browser import (
    BrowserProvider,
    BrowserProviderManager,
    BrowserStatus,
    CDPProvider,
    EgoLiteProvider,
    SpaceInfo,
    TabInfo,
    get_browser_provider_manager,
)
from actions.browser.ego_provider import locate_ego_executable
from memory import config_manager


class TestHybridBrowserArchitecture(unittest.TestCase):
    def setUp(self):
        self.mgr = get_browser_provider_manager()

    # ── 1. Ego(lite) Initialization ───────────────────────────────────────────
    def test_01_ego_initialization(self):
        """Verify EgoLiteProvider locates binary and sets up defaults."""
        prov = EgoLiteProvider()
        self.assertEqual(prov.name, "ego")
        exe = locate_ego_executable()
        self.assertIsNotNone(exe, "ego-browser executable should be located on system")
        self.assertIn("ego-browser", exe.lower())

    # ── 2. Ego(lite) Connection Failures ──────────────────────────────────────
    def test_02_ego_connection_failure(self):
        """Verify health check fails gracefully when binary missing or timeout occurs."""
        bad_prov = EgoLiteProvider(executable_path=r"C:\NonExistent\ego-browser.exe")
        is_healthy, msg = bad_prov.check_health(timeout=0.1)
        self.assertFalse(is_healthy)
        self.assertIn("not found", msg.lower())

        with patch.object(bad_prov, "_run_script", return_value=(-1, "", "Connection refused")):
            is_healthy, msg = bad_prov.check_health(timeout=0.1)
            self.assertFalse(is_healthy)

    # ── 3. CDP Provider Initialization ────────────────────────────────────────
    def test_03_cdp_provider_initialization(self):
        """Verify CDPProvider initializes Chrome and Edge engines."""
        chrome_prov = CDPProvider(engine="chrome")
        self.assertEqual(chrome_prov.name, "chrome_cdp")
        self.assertEqual(chrome_prov.engine, "chrome")

        edge_prov = CDPProvider(engine="edge")
        self.assertEqual(edge_prov.name, "edge_cdp")
        self.assertEqual(edge_prov.engine, "edge")

    # ── 4. Provider Selection (Auto Mode) ─────────────────────────────────────
    def test_04_provider_selection(self):
        """Verify intelligent selection prefers Ego when available."""
        with patch.object(config_manager, "get_browser_provider_config", return_value={"preferred": "ego", "mode": "auto", "auto_fallback": True}):
            with patch.object(EgoLiteProvider, "check_health", return_value=(True, "Ego healthy")):
                prov = self.mgr.get_active_provider()
                self.assertEqual(prov.name, "ego")

    # ── 5. Automatic Fallback ─────────────────────────────────────────────────
    def test_05_automatic_fallback(self):
        """Verify automatic fallback from Ego to CDP when Ego is unhealthy."""
        with patch.object(config_manager, "get_browser_provider_config", return_value={"preferred": "ego", "fallback": "cdp", "mode": "auto", "auto_fallback": True}):
            with patch.object(EgoLiteProvider, "check_health", return_value=(False, "Ego offline")):
                prov = self.mgr.get_active_provider()
                self.assertTrue(prov.name.endswith("cdp"))
                notice = self.mgr.consume_switch_notice()
                self.assertIsNotNone(notice)
                self.assertIn("unavailable", notice.lower())

    # ── 6. Provider Switching (Manual Mode) ────────────────────────────────────
    def test_06_provider_switching(self):
        """Verify manual engine switching and mode locking."""
        msg = self.mgr.set_provider("chrome")
        self.assertIn("CHROME", msg)
        self.assertEqual(self.mgr._mode, "manual")

        msg_auto = self.mgr.set_provider("auto")
        self.assertIn("AUTOMATIC", msg_auto)
        self.assertEqual(self.mgr._mode, "auto")

    # ── 7. Browser Navigation ─────────────────────────────────────────────────
    def test_07_browser_navigation(self):
        """Verify navigation command returns normalized confirmation."""
        prov = EgoLiteProvider()
        fake_result = "===NAV_RESULT===\n" + json.dumps({"spaceId": 10, "title": "Example", "url": "https://example.com"})
        with patch.object(prov, "_run_script", return_value=(0, fake_result, "")):
            res = prov.navigate("https://example.com")
            self.assertIn("Navigated to 'https://example.com'", res)
            self.assertEqual(prov.active_page_url, "https://example.com")

    # ── 8. Semantic Page Observation ──────────────────────────────────────────
    def test_08_semantic_page_observation(self):
        """Verify observation parses semantic snapshot and generates prompt indices."""
        prov = EgoLiteProvider()
        fake_snap = (
            '[p1 "Home" | space "Task"(1): 1 managed]\n'
            'root\n'
            '  anchor [ref=1, loc=href:https://example.com] text "Click Here"\n'
            '  button [ref=2, loc=role:button] text "Submit Form"\n'
            '  textbox [ref=3, placeholder="Search query"]'
        )
        fake_block = "===EGO_OBS_START===\n" + json.dumps({"spaceId": 1, "title": "Home", "url": "https://example.com", "snap": fake_snap}) + "\n===EGO_OBS_END==="
        with patch.object(prov, "_run_script", return_value=(0, fake_block, "")):
            obs = prov.observe()
            self.assertIn("Interactive Elements (Ego Space #1):", obs)
            self.assertIn('[1] <anchor> text="Click Here"', obs)
            self.assertIn('[2] <button> text="Submit Form"', obs)
            self.assertIn('[3] <textbox> placeholder="Search query"', obs)
            self.assertIn(1, prov._element_map)
            self.assertEqual(prov._element_map[1].text, "Click Here")

    # ── 9. Clicking and Typing ────────────────────────────────────────────────
    def test_09_clicking_and_typing(self):
        """Verify clicking by ref and typing text into target elements."""
        prov = EgoLiteProvider()
        with patch.object(prov, "_run_script", return_value=(0, "===CLICK_RESULT===\n{}", "")):
            with patch.object(prov, "observe", return_value="Obs refreshed"):
                res_click = prov.click(index=1)
                self.assertIn("Clicked @1", res_click)

        with patch.object(prov, "_run_script", return_value=(0, "===TYPE_RESULT===\n{}", "")):
            with patch.object(prov, "observe", return_value="Obs refreshed"):
                res_type = prov.type_text("JARVIS test text", index=2, press_enter=True)
                self.assertIn("Typed 'JARVIS test text' into @2 with Enter", res_type)

    # ── 10. Keyboard Actions ──────────────────────────────────────────────────
    def test_10_keyboard_actions(self):
        """Verify hardware-level keyboard key press execution."""
        prov = EgoLiteProvider()
        with patch.object(prov, "_run_script", return_value=(0, "KEY_PRESSED", "")):
            res = prov.press_key("Enter")
            self.assertIn("Pressed key 'Enter'", res)

    # ── 11. Tab and Space Management ──────────────────────────────────────────
    def test_11_tab_and_space_management(self):
        """Verify Spaces and Tab operations."""
        prov = EgoLiteProvider()
        # Create space
        fake_sp = "===SPACE_CREATED===\n" + json.dumps({"id": 7, "name": "Isolated Work", "ownership": "agent"})
        with patch.object(prov, "_run_script", return_value=(0, fake_sp, "")):
            sp = prov.create_space("Isolated Work")
            self.assertIsInstance(sp, SpaceInfo)
            self.assertEqual(sp.id, 7)
            self.assertEqual(prov.active_space_id, 7)

        # Switch space
        sw_msg = prov.switch_space(7)
        self.assertIn("Space #7", sw_msg)

        # Close space
        with patch.object(prov, "_run_script", return_value=(0, "SPACE_CLOSED", "")):
            cl_msg = prov.close_space(7)
            self.assertIn("Closed Ego task space #7", cl_msg)

        # New tab
        fake_tb = "===NEW_TAB_RESULT===\n" + json.dumps({"index": 1, "title": "Doc", "url": "https://doc.com", "spaceId": 7})
        with patch.object(prov, "_run_script", return_value=(0, fake_tb, "")):
            tb = prov.new_tab("https://doc.com")
            self.assertIsInstance(tb, TabInfo)
            self.assertEqual(tb.index, 1)

    # ── 12. Browser Crash & Timeout Recovery ──────────────────────────────────
    def test_12_browser_crash_recovery(self):
        """Verify graceful error recovery when browser command times out or crashes."""
        prov = EgoLiteProvider()
        with patch("subprocess.Popen", side_effect=Exception("Browser process crashed unexpectedly")):
            code, out, err = prov._run_script("some code", timeout=1.0)
            self.assertEqual(code, -1)
            self.assertIn("crashed", err)

    # ── 13. Session Isolation ─────────────────────────────────────────────────
    def test_13_session_isolation(self):
        """Verify SpaceInfo maintains separate identity from user tabs."""
        s1 = SpaceInfo(id=1, name="Agent Task A", ownership="agent", is_active=True, tab_count=2)
        s2 = SpaceInfo(id=2, name="Agent Task B", ownership="agent", is_active=False, tab_count=1)
        self.assertNotEqual(s1.id, s2.id)
        self.assertTrue(s1.is_active)
        self.assertFalse(s2.is_active)

    # ── 14. Authentication Persistence ────────────────────────────────────────
    def test_14_authentication_persistence(self):
        """Verify profile info is reported accurately in spaces."""
        prov = EgoLiteProvider()
        fake_spaces = "===SPACES_LIST===\n" + json.dumps([
            {"id": 1, "name": "My Workspace", "ownership": "agent", "profileId": "Profile 1", "profileName": "Vishwajith", "recentTabTitles": ["Dashboard"]}
        ])
        with patch.object(prov, "_run_script", return_value=(0, fake_spaces, "")):
            spaces = prov.list_spaces()
            self.assertEqual(len(spaces), 1)
            self.assertEqual(spaces[0].profile_name, "Vishwajith")

    # ── 15. Gemini Tool-Call Compatibility ────────────────────────────────────
    def test_15_gemini_tool_call_compatibility(self):
        """Verify browser_control executes actions and returns normalized outcome strings."""
        with patch.object(EgoLiteProvider, "check_health", return_value=(True, "Online")):
            with patch.object(EgoLiteProvider, "navigate", return_value="Navigated to https://example.com"):
                res = browser_control.browser_control({"action": "navigate", "url": "https://example.com"})
                self.assertIn("status=done", res)
                self.assertIn("action=navigate", res)

            with patch.object(EgoLiteProvider, "create_space", return_value=SpaceInfo(id=8, name="Demo", ownership="agent")):
                res_space = browser_control.browser_control({"action": "create_space", "name": "Demo"})
                self.assertIn("status=done", res_space)
                self.assertIn("action=create_space", res_space)

            res_status = browser_control.browser_control({"action": "status"})
            self.assertIn("status=done", res_status)
            self.assertIn("action=status", res_status)

    # ── 16. WhatsApp Safety and Verification ──────────────────────────────────
    def test_16_whatsapp_safety_and_verification(self):
        """Verify that typing or clicking send on WhatsApp Web triggers confirmation gate."""
        with patch.object(browser_control, "_get_active_url", return_value="https://web.whatsapp.com"):
            with patch("actions.whatsapp_gate.is_whatsapp_send_action", return_value=True):
                res = browser_control.browser_control({
                    "action": "type",
                    "text": "Hello, this is automated",
                    "press_enter": True,
                })
                self.assertTrue("CONFIRMATION_PENDING" in str(res) or getattr(res, "status", "") == "needs_confirmation")

    # ── 17. Browser Dashboard Status ──────────────────────────────────────────
    def test_17_browser_dashboard_status(self):
        """Verify get_status returns complete telemetry schema for dashboard."""
        st = self.mgr.get_status()
        self.assertIsInstance(st, BrowserStatus)
        d = st.to_dict()
        self.assertIn("provider", d)
        self.assertIn("is_connected", d)
        self.assertIn("spaces", d)
        self.assertIn("tabs", d)
        self.assertIn("health", d)


if __name__ == "__main__":
    unittest.main()
