"""
tests/test_english_mode.py — Unit tests for English Placement Practice Mode.

Verifies:
- Prompt overlay generation for all 6 scenarios
- Feedback limits: <= 2 corrections rule in prompt overlay
- Feedback style variations (after_answer, end_of_session, on_request)
- Honest declared limits (pronunciation limits, human interview replacement disclaimer)
- Mistake logging into memory/english_mistakes.json
- Filler word analytics and turn metrics tracking
- Flashcard generation compatible with player.show_quiz and spaced repetition
- Prompt integration into core/prompt_builder._load_system_prompt()
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import prompt_builder
from memory import config_manager
from plugins import _english_mode, modes


class TestEnglishPracticeMode(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_mistakes_file = Path(self.temp_dir.name) / "english_mistakes.json"
        self._orig_file = _english_mode._MISTAKES_FILE
        _english_mode._MISTAKES_FILE = self.tmp_mistakes_file

        # Reset active mode
        config_manager.set_active_mode(None)

    def tearDown(self):
        _english_mode._MISTAKES_FILE = self._orig_file
        config_manager.set_active_mode(None)
        self.temp_dir.cleanup()

    def test_all_six_scenarios_exist(self):
        """Verify all 6 placement scenarios are defined with guidelines."""
        expected_scenarios = [
            "self_intro",
            "hr_round",
            "project_explain",
            "code_walkthrough",
            "group_discussion",
            "presentation",
        ]
        for scen in expected_scenarios:
            self.assertIn(scen, _english_mode.SCENARIOS)
            self.assertTrue(len(_english_mode.SCENARIOS[scen]["title"]) > 0)
            self.assertTrue(len(_english_mode.SCENARIOS[scen]["guidelines"]) > 0)

    def test_prompt_overlay_strict_rules(self):
        """Verify prompt overlay contains <=2 corrections limit, English-only policy, and honest limits."""
        overlay = _english_mode.get_prompt_overlay(scenario="hr_round", feedback_style="after_answer")

        # 1. English policy
        self.assertIn("ENGLISH ONLY", overlay)

        # 2. Max 2 corrections rule
        self.assertIn("AT MOST TWO", overlay)
        self.assertIn("<= 2", overlay)

        # 3. STAR method in HR round
        self.assertIn("STAR", overlay)

        # 4. Honest limits
        self.assertIn("pronunciation", overlay.lower())
        self.assertIn("not reliable", overlay.lower())
        self.assertIn("does not replace", overlay.lower())

    def test_feedback_styles_reflected_in_prompt(self):
        """Verify after_answer, end_of_session, and on_request alter the prompt instructions."""
        overlay_after = _english_mode.get_prompt_overlay(feedback_style="after_answer")
        self.assertIn("IMMEDIATELY after each answer", overlay_after)

        overlay_end = _english_mode.get_prompt_overlay(feedback_style="end_of_session")
        self.assertIn("Do NOT interrupt with corrections after each turn", overlay_end)

        overlay_req = _english_mode.get_prompt_overlay(feedback_style="on_request")
        self.assertIn("ONLY when the user explicitly asks", overlay_req)

    def test_mistake_logging_and_retrieval(self):
        """Verify mistake logging persists wrong phrase, correction, reason, and date."""
        entry = _english_mode.log_mistake(
            wrong_phrase="I am having 3 years experience in Python",
            corrected_phrase="I have 3 years of experience with Python",
            reason="State verbs such as 'have' are not used in continuous tense.",
            scenario="self_intro",
        )
        self.assertIsNotNone(entry.get("id"))
        self.assertEqual(entry["scenario"], "self_intro")

        history = _english_mode.get_mistake_history(limit=10)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["wrong_phrase"], "I am having 3 years experience in Python")

    def test_filler_words_and_metrics_tracking(self):
        """Verify filler words count and metrics update over turns."""
        sample_text = (
            "Um, basically I like designed the microservice architecture and you know, "
            "it was actually very scalable."
        )
        fillers = _english_mode.count_filler_words(sample_text)
        self.assertGreaterEqual(fillers.get("um", 0), 1)
        self.assertGreaterEqual(fillers.get("basically", 0), 1)
        self.assertGreaterEqual(fillers.get("like", 0), 1)
        self.assertGreaterEqual(fillers.get("you know", 0), 1)
        self.assertGreaterEqual(fillers.get("actually", 0), 1)

        metrics = _english_mode.record_answer_metrics(sample_text)
        self.assertGreater(metrics["words"], 10)

        summary = _english_mode.get_analytics_summary()
        self.assertEqual(summary["total_answers"], 1)
        self.assertGreater(summary["total_words"], 10)
        self.assertTrue(len(summary["top_fillers"]) > 0)

    def test_flashcard_generation_for_show_quiz(self):
        """Verify flashcards adhere to player.show_quiz schema."""
        _english_mode.log_mistake(
            wrong_phrase="He do not know",
            corrected_phrase="He does not know",
            reason="Third person singular requires 'does not'.",
        )
        cards = _english_mode.generate_flashcards()
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertIn("question", card)
        self.assertIn("options", card)
        self.assertIn("answer", card)
        self.assertIn("note", card)
        self.assertEqual(card["answer"], "He does not know")
        self.assertEqual(card["options"], [])  # Typed / spoken

    def test_interactive_mistake_quiz_integration(self):
        """Verify show_mistake_quiz invokes player.show_quiz."""
        _english_mode.log_mistake("Me and him went", "He and I went", "Subject pronoun rule")
        mock_player = MagicMock()

        msg = _english_mode.show_mistake_quiz(player=mock_player)
        self.assertIn("Loaded", msg)
        mock_player.show_quiz.assert_called_once()
        args, kwargs = mock_player.show_quiz.call_args
        topic = kwargs.get("topic", args[0] if args else "")
        self.assertIn("English", topic)
        self.assertIn("Placement", topic)

    def test_modes_plugin_english_actions(self):
        """Verify modes plugin handles scenario switching, feedback style, and mistake logging."""
        # Set scenario
        res_scen = modes.run({"action": "set_scenario", "scenario": "hr_round"})
        self.assertIn("hr_round", res_scen)
        self.assertEqual(config_manager.get_mode_settings("english_practice").get("scenario"), "hr_round")

        # Set feedback style
        res_fb = modes.run({"action": "set_feedback_style", "feedback_style": "on_request"})
        self.assertIn("on_request", res_fb)
        self.assertEqual(config_manager.get_mode_settings("english_practice").get("feedback_style"), "on_request")

        # Log mistake via plugin
        res_log = modes.run(
            {
                "action": "log_mistake",
                "wrong_phrase": "I done the testing",
                "corrected_phrase": "I performed the testing",
                "reason": "Use past simple 'did' or stronger technical verb.",
            }
        )
        self.assertIn("Logged mistake", res_log)

    def test_prompt_builder_injects_english_overlay_when_active(self):
        """Verify _load_system_prompt() in core.prompt_builder includes English overlay when mode is active."""
        config_manager.set_active_mode(None)
        base_prompt = prompt_builder._load_system_prompt()
        self.assertNotIn("[MODE ACTIVE: ENGLISH PLACEMENT PRACTICE MODE", base_prompt)

        # Activate English practice
        _english_mode.activate_english_mode(scenario="project_explain")
        active_prompt = prompt_builder._load_system_prompt()
        self.assertIn("[MODE ACTIVE: ENGLISH PLACEMENT PRACTICE MODE", active_prompt)
        self.assertIn("Technical Project Deep-Dive", active_prompt)

        # Deactivate
        _english_mode.deactivate_english_mode()
        deactive_prompt = prompt_builder._load_system_prompt()
        self.assertNotIn("[MODE ACTIVE: ENGLISH PLACEMENT PRACTICE MODE", deactive_prompt)


if __name__ == "__main__":
    unittest.main()
