"""
tests/test_visual_tables_screen_glow_and_dock_minimization.py

Unit tests for:
1. Visual interpretation of tables, diagrams, and bar charts in format_visual_content_html.
2. Red glowing screen edge overlay (ScreenGlowOverlay) when JARVIS observes display.
3. Search and Reminders displays minimizing into the left sidebar dock (MinimizedDisplaysDock).
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from ui import (
    JarvisUI,
    ScreenGlowOverlay,
    format_visual_content_html,
    MinimizedDisplayPill,
    MinimizedDisplaysDock,
)
from actions import screen_processor


class TestVisualContentFormatting(unittest.TestCase):
    def test_markdown_table_with_inline_bar_charts(self):
        sample = """
| Metric | Progress | Status |
|---|---|---|
| Engine | 85% | Nominal |
| Memory | 42% | Normal |
"""
        html = format_visual_content_html(sample)
        self.assertIn("<table", html)
        self.assertIn("Engine", html)
        self.assertIn("Nominal", html)
        # Inline bar chart rendered for 85% and 42%
        self.assertIn('bgcolor="#ff1744"', html)
        self.assertIn('width="85%"', html)
        self.assertIn('width="42%"', html)

    def test_standalone_metrics_bar_chart(self):
        sample = """
CPU Load: 78%
RAM Allocation = 60%
"""
        html = format_visual_content_html(sample)
        self.assertIn("CPU Load", html)
        self.assertIn("78%", html)
        self.assertIn('width="78%"', html)
        self.assertIn("RAM Allocation", html)
        self.assertIn('width="60%"', html)

    def test_flowchart_diagram_formatting(self):
        sample = "Audio Input -> Voice Recognition -> Reasoning -> Action Execution"
        html = format_visual_content_html(sample)
        self.assertIn("SYSTEM FLOW DIAGRAM", html)
        self.assertIn("Audio Input", html)
        self.assertIn("Action Execution", html)
        self.assertIn("➔", html)


class TestScreenGlowOverlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.player = JarvisUI(face_path="")
        cls.win = cls.player._win

    def test_screen_glow_overlay_trigger(self):
        overlay = ScreenGlowOverlay()
        self.assertIsNotNone(overlay)

        # Trigger glow
        overlay.trigger_glow(duration_ms=1000)
        self.assertTrue(overlay.isVisible())
        self.assertEqual(overlay._glow_alpha, 255)

        # Hide glow
        overlay.hide_glow()
        # Alpha should start decreasing on fade timer
        overlay._on_fade_step()
        self.assertLess(overlay._glow_alpha, 255)
        overlay.close()

    def test_player_thread_safe_screen_glow(self):
        self.player.show_screen_glow(duration_ms=1500)
        QApplication.processEvents()
        self.assertTrue(self.win._screen_glow_overlay.isVisible())
        self.assertEqual(self.win._screen_glow_overlay._glow_alpha, 255)

    def test_screen_processor_glow_callback(self):
        mock_cb = MagicMock()
        screen_processor.set_screen_glow_callback(mock_cb)
        screen_processor.trigger_screen_glow(2500)
        mock_cb.assert_called_with(2500)


class TestLeftDockMinimization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.player = JarvisUI(face_path="")
        cls.win = cls.player._win

    def test_minimized_pill_search_and_reminders(self):
        pill_search = MinimizedDisplayPill("search", "Intel Query")
        self.assertEqual(pill_search.text(), "🔍")

        pill_rem = MinimizedDisplayPill("reminders", "Schedule Matrix")
        self.assertEqual(pill_rem.text(), "⏰")

    def test_search_dock_minimizes_to_left_bar(self):
        # Open search dock
        self.win._show_content("Search Test", "| Col1 | Col2 |\n|---|---|\n| A | B |")
        QApplication.processEvents()
        self.assertTrue(self.win.left_search_dock.isVisible())

        # Minimize to left dock
        self.win.left_search_dock.minimize_to_dock()
        QApplication.processEvents()
        self.assertFalse(self.win.left_search_dock.isVisible())
        self.assertTrue(self.win._search_dock_minimized_to_bar)

        # Verify it appears in _dots_overlay
        self.assertIn("search", self.win._dots_overlay._buttons)

        # Restore from dock pill click
        self.win._on_restore_display("search")
        QApplication.processEvents()
        self.assertTrue(self.win.left_search_dock.isVisible())
        self.assertFalse(self.win._search_dock_minimized_to_bar)

    def test_reminders_dock_minimizes_to_left_bar(self):
        # Open reminders dock
        self.win._show_reminders("upcoming")
        QApplication.processEvents()
        self.assertTrue(self.win._reminders_dock.isVisible())

        # Minimize to left dock
        self.win._reminders_dock.minimize_to_dock()
        QApplication.processEvents()
        self.assertFalse(self.win._reminders_dock.isVisible())
        self.assertTrue(self.win._reminders_dock_minimized_to_bar)

        # Verify it appears in _dots_overlay
        self.assertIn("reminders", self.win._dots_overlay._buttons)

        # Restore from dock pill click
        self.win._on_restore_display("reminders")
        QApplication.processEvents()
        self.assertTrue(self.win._reminders_dock.isVisible())
        self.assertFalse(self.win._reminders_dock_minimized_to_bar)


if __name__ == "__main__":
    unittest.main()
