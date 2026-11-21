"""
tests/test_config_template_sync.py — Test synchronization and safety of config/api_keys.template.json
"""

import json
import unittest
from pathlib import Path

from memory import config_manager


class TestConfigTemplateSync(unittest.TestCase):
    def setUp(self):
        self.template_path = Path("config/api_keys.template.json")
        self.assertTrue(self.template_path.exists(), "config/api_keys.template.json must exist")
        self.template = json.loads(self.template_path.read_text(encoding="utf-8"))

    def test_required_core_keys_present(self):
        """Verify essential identity, voice, audio, and safety keys exist in template."""
        essential_keys = [
            "gemini_api_key",
            "assistant_name",
            "user_name",
            "voice_name",
            "os_system",
            "trust_level",
            "wake_word_enabled",
            "wake_words",
            "wake_word_threshold",
            "auto_sleep_enabled",
            "idle_sleep_timeout",
            "push_to_talk_enabled",
            "audio_input_mode",
            "audio_routing_mode",
            "allow_bluetooth_mic",
            "ducking_enabled",
            "ducking_level",
            "dashboard_allow_lan",
            "provider_mode",
            "provider_fallback_enabled",
        ]
        for key in essential_keys:
            with self.subTest(key=key):
                self.assertIn(
                    key,
                    self.template,
                    f"Configuration template must include '{key}'",
                )

    def test_no_actual_secrets_in_template(self):
        """Ensure template contains placeholders rather than active credentials."""
        gemini_key = self.template.get("gemini_api_key", "")
        self.assertTrue(
            gemini_key == "" or "YOUR_GEMINI_API_KEY_HERE" in gemini_key,
            "Template must not contain real API keys",
        )
        self.assertFalse(
            gemini_key.startswith("AIzaSy"),
            "Template must never contain a live Google API key prefix",
        )

    def test_shortcut_structures(self):
        """Verify shortcut definitions are lists of string modifiers and keys."""
        shortcut_keys = [
            "global_listening_shortcut",
            "global_mute_shortcut",
            "global_force_listening_shortcut",
            "global_emergency_stop_shortcut",
        ]
        for sk in shortcut_keys:
            if sk in self.template:
                val = self.template[sk]
                self.assertIsInstance(val, list, f"'{sk}' must be a list of keys")
                for key_part in val:
                    self.assertIsInstance(key_part, str, f"Key part in '{sk}' must be a string")


if __name__ == "__main__":
    unittest.main()
