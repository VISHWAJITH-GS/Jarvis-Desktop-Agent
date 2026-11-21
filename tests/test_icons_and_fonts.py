"""
tests/test_icons_and_fonts.py — Unit test suite for Flaticon UIcons and Montserrat font integration.
"""

import unittest
import sys
from pathlib import Path
from PyQt6.QtWidgets import QApplication, QPushButton
from PyQt6.QtGui import QFont, QIcon

from gui import icons_manager
from gui.icons_manager import get_uicon, _SVG_TEMPLATES


class TestFlaticonUIconsAndFonts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_all_user_specified_icons_present(self):
        """Verify all 11 user-specified Flaticon UIcons render valid QIcons."""
        expected_icons = [
            "minus",                # fi-rr-minus
            "check-circle",         # fi-rr-check-circle
            "shield-exclamation",   # fi-rr-shield-exclamation
            "shield",               # fi-rr-shield
            "settings",             # fi-rr-settings
            "sun",                  # fi-rr-sun
            "circle-microphone",    # fi-ss-circle-microphone
            "angle-small-down",     # fi-sc-angle-small-down
            "angle-small-up",       # fi-rr-angle-small-up
            "search",               # fi-rr-search
            "database",             # fi-rr-database
        ]
        for icon_name in expected_icons:
            with self.subTest(icon=icon_name):
                self.assertIn(icon_name, _SVG_TEMPLATES, f"Missing template for {icon_name}")
                icon = get_uicon(icon_name, color="#ff1744", size=16)
                self.assertIsInstance(icon, QIcon)
                self.assertFalse(icon.isNull(), f"QIcon for {icon_name} is null")

    def test_complementary_icons_present(self):
        """Verify complementary UI icons render properly."""
        complements = ["calendar", "comment", "refresh", "cross", "mini", "restore", "microphone-slash"]
        for icon_name in complements:
            with self.subTest(icon=icon_name):
                icon = get_uicon(icon_name, color="#00e676", size=14)
                self.assertFalse(icon.isNull())

    def test_icons_cached(self):
        """Verify get_uicon caches identical requests."""
        icon1 = get_uicon("search", "#ff1744", 18)
        icon2 = get_uicon("search", "#ff1744", 18)
        self.assertIs(icon1, icon2)

    def test_montserrat_fonts_exist_in_root(self):
        """Verify Montserrat fonts are located in the fonts directory."""
        fonts_dir = Path(__file__).resolve().parent.parent / "fonts"
        self.assertTrue(fonts_dir.exists(), "fonts directory does not exist")
        self.assertTrue((fonts_dir / "Montserrat-Regular.ttf").exists(), "Montserrat-Regular.ttf missing")
        self.assertTrue((fonts_dir / "Montserrat-Bold.ttf").exists(), "Montserrat-Bold.ttf missing")

    def test_button_with_uicon(self):
        """Verify a QPushButton can be configured with a Flaticon UIcon."""
        btn = QPushButton("TEST")
        icon = get_uicon("shield-exclamation", color="#ff1744", size=14)
        btn.setIcon(icon)
        self.assertFalse(btn.icon().isNull())

    def test_icons_have_transparent_background(self):
        """Verify icons do not have a solid white background and are transparent."""
        icon = get_uicon("settings", color="#ff1744", size=24)
        pix = icon.pixmap(24, 24)
        img = pix.toImage()
        corner = img.pixelColor(0, 0)
        self.assertEqual(corner.alpha(), 0, "Icon background must be transparent (alpha == 0)")


if __name__ == "__main__":
    unittest.main()
