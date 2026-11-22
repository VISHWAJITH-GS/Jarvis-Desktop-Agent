"""
tests/test_live_session_decls.py — Verify tool declaration deduplication and live session payload integrity.
"""

import unittest
from unittest.mock import MagicMock

from core.action_loader import get_default_registry
from core.prompt_builder import TOOL_DECLARATIONS
from core.schema_utils import strip_meta


class TestLiveSessionToolDeclarations(unittest.TestCase):
    def test_no_duplicate_function_declarations_in_registry_and_inline_tools(self):
        """Ensure that concatenating TOOL_DECLARATIONS and ActionRegistry tool declarations does not produce duplicates."""
        registry = get_default_registry()
        action_decls = registry.get_tool_declarations()

        combined_raw = TOOL_DECLARATIONS + action_decls
        processed = strip_meta(combined_raw)

        names = [d["name"] for d in processed if isinstance(d, dict) and "name" in d]
        duplicates = [n for n in set(names) if names.count(n) > 1]
        self.assertEqual(duplicates, [], f"Found duplicate function declarations: {duplicates}")

        # Specifically verify screen_process appears exactly once
        screen_process_count = names.count("screen_process")
        self.assertEqual(screen_process_count, 1, "screen_process must appear exactly once in processed declarations")

    def test_strip_meta_deduplication(self):
        """Verify strip_meta strips meta keys and removes duplicate declarations."""
        decls = [
            {"name": "tool_a", "description": "First instance", "tier": "core"},
            {"name": "tool_b", "description": "Other tool"},
            {"name": "tool_a", "description": "Duplicate instance", "tier": "extra"},
        ]
        result = strip_meta(decls)
        self.assertEqual(len(result), 2)
        names = [d["name"] for d in result]
        self.assertEqual(names, ["tool_a", "tool_b"])
        self.assertEqual(result[0]["description"], "First instance")
        self.assertNotIn("tier", result[0])


if __name__ == "__main__":
    unittest.main()
