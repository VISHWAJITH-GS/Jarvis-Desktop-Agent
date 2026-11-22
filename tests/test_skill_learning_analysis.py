"""
tests/test_skill_learning_analysis.py — Verification of Part 2 (Intent-First Analysis & SkillSpec).

Tests:
1. Real ActionRegistry catalog fed to DemonstrationAnalyzer (not hardcoded tools).
2. Strict SkillSpec output structure (parameters with examples/defaults, preconditions, steps with expected_outcome, postconditions).
3. Preference for high-level tools (page_to_file over 15 clicks/paste, whatsapp_send over click-search-type-send).
4. Browser independence using config_manager.get_browser_engine().
5. 'Learn from description' mode without screen demonstrations.
"""

import unittest
from unittest.mock import MagicMock, patch

from actions.skill_learner import skill_learner
from core.action_loader import get_default_registry
from core.skill_learning.demonstration_analyzer import DemonstrationAnalyzer
from core.skill_learning.learning_manager import LearningManager
from core.skill_learning.models import UIEvent
from core.skill_learning.workflow_builder import WorkflowBuilder
from memory.config_manager import save_browser_engine


class TestRealCatalogFeeding(unittest.TestCase):
    def test_analyzer_uses_real_action_registry(self):
        reg = get_default_registry()
        analyzer = DemonstrationAnalyzer(action_registry=reg)
        catalog = analyzer._format_action_catalog()

        # Must contain real tools from repository ActionRegistry
        self.assertIn("page_to_file", catalog)
        self.assertIn("send_message", catalog)
        self.assertIn("browser_control", catalog)
        self.assertIn("open_app", catalog)


class TestHighLevelToolsPreference(unittest.TestCase):
    def setUp(self):
        self.analyzer = DemonstrationAnalyzer()

    def test_prefers_page_to_file_over_raw_clicks_for_chat_export(self):
        events = [
            UIEvent(timestamp=1.0, event_type="click", app_name="Chrome", window_title="ChatGPT", element_name="Select All"),
            UIEvent(timestamp=2.0, event_type="clipboard", app_name="Chrome", window_title="ChatGPT", action="copy", char_count=2000),
            UIEvent(timestamp=3.0, event_type="window_activate", app_name="Notepad", window_title="Untitled - Notepad"),
            UIEvent(timestamp=4.0, event_type="clipboard", app_name="Notepad", window_title="Untitled - Notepad", action="paste", char_count=2000),
        ]
        res = self.analyzer._deterministic_fallback_analysis("save chat to file", events)
        
        # Must be synthesized into page_to_file, NOT raw clicks/pastes
        step_tools = [s["tool"] for s in res["steps"]]
        self.assertIn("page_to_file", step_tools)
        self.assertNotIn("clipboard", step_tools)
        
        # Verify SkillSpec fields
        self.assertIn("parameters", res)
        self.assertIn("postconditions", res)
        step = res["steps"][0]
        self.assertIn("expected_outcome", step)
        self.assertIn("verification", step)

    def test_prefers_whatsapp_send_over_raw_clicks(self):
        events = [
            UIEvent(timestamp=1.0, event_type="click", app_name="Chrome", window_title="WhatsApp Web", element_name="Search"),
            UIEvent(timestamp=2.0, event_type="type_text", app_name="Chrome", window_title="WhatsApp Web", input_value="Ram Nath TCE"),
            UIEvent(timestamp=3.0, event_type="click", app_name="Chrome", window_title="WhatsApp Web", element_name="Ram Nath TCE"),
            UIEvent(timestamp=4.0, event_type="type_text", app_name="Chrome", window_title="WhatsApp Web", input_value="Meeting at 3 PM"),
            UIEvent(timestamp=5.0, event_type="click", app_name="Chrome", window_title="WhatsApp Web", element_name="Send"),
        ]
        res = self.analyzer._deterministic_fallback_analysis("send whatsapp message to Ram Nath TCE", events)
        step_tools = [s["tool"] for s in res["steps"]]
        self.assertEqual(step_tools, ["whatsapp_send"])
        self.assertEqual(res["steps"][0]["tool"], "whatsapp_send")


class TestBrowserIndependence(unittest.TestCase):
    def test_uses_configured_browser_engine(self):
        analyzer = DemonstrationAnalyzer()
        try:
            save_browser_engine("edge")
            events = [
                UIEvent(timestamp=1.0, event_type="window_activate", app_name="Browser", window_title="Portal"),
            ]
            res = analyzer._deterministic_fallback_analysis("open portal", events)
            self.assertEqual(res["application"], "Edge")
            self.assertEqual(res["steps"][0]["parameters"]["app_name"], "Edge")
        finally:
            save_browser_engine("chrome")


class TestLearnFromDescriptionMode(unittest.TestCase):
    def setUp(self):
        self.manager = LearningManager.get_instance()

    def test_learn_from_description_creates_valid_skill(self):
        res = self.manager.learn_from_description(
            name="Summarize Active Chat",
            description="Copy the active chat from the browser and save it to a file on desktop named chat.txt",
        )
        self.assertTrue(res["success"], f"Expected success, got: {res}")
        self.assertIn("skill_id", res)
        self.assertTrue(len(res["steps"]) >= 1)
        self.assertEqual(res["steps"][0]["tool"], "page_to_file")

    def test_skill_learner_action_routes_description(self):
        with patch("actions.skill_learner.get_default_learning_manager") as mock_mgr_getter:
            mock_mgr = MagicMock()
            mock_mgr.learn_from_description.return_value = {
                "success": True,
                "skill_id": "test_skill",
                "message": "Skill learned",
                "steps": [],
            }
            mock_mgr_getter.return_value = mock_mgr

            res = skill_learner({
                "action": "learn_from_description",
                "goal": "Export page to file",
                "description": "Extract active browser page text and save to file",
            })
            self.assertIn("status=done", str(res))
            mock_mgr.learn_from_description.assert_called_once()


if __name__ == "__main__":
    unittest.main()
