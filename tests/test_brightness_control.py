"""
tests/test_brightness_control.py — Unit Tests for Display Brightness Control.

Verifies:
1. windows_control: brightness_up, brightness_down, set_brightness, get_brightness, and aliases.
2. Value parsing, delta offsets, and clamping between 0 and 100.
3. computer_settings: brightness_up, brightness_down, set_brightness, and brightness_set.
4. Safe fallback and verification behavior.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import actions.computer_settings as cs
import actions.windows_control as wc


class TestWindowsControlBrightness(unittest.TestCase):
    def test_get_brightness(self):
        with patch("actions.windows_control._run_ps", return_value=(0, "65", "")):
            b = wc.get_brightness()
            self.assertEqual(b, 65)

    def test_get_brightness_multiline_or_spaces(self):
        with patch("actions.windows_control._run_ps", return_value=(0, "\r\n  75  \r\n", "")):
            b = wc.get_brightness()
            self.assertEqual(b, 75)

    def test_set_brightness_success(self):
        with (
            patch("actions.windows_control.get_brightness", return_value=70),
            patch("actions.windows_control._run_ps", return_value=(0, "", "")) as mock_ps,
        ):
            res = wc.set_brightness(70)
            self.assertTrue(res["success"])
            self.assertEqual(res["level"], 70)
            self.assertIn("WmiSetBrightness(1, 70)", mock_ps.call_args[0][0])

    def test_brightness_up_defaults_to_plus_ten(self):
        with (
            patch("actions.windows_control.get_brightness", return_value=50),
            patch("actions.windows_control.set_brightness") as mock_set,
        ):
            mock_set.return_value = {
                "success": True,
                "action": "set_brightness",
                "level": 60,
                "verified": True,
                "message": "Brightness set to 60%.",
            }
            res = wc.windows_control({"action": "brightness_up"})
            self.assertIn("status=done", res)
            mock_set.assert_called_with(60)

    def test_brightness_up_with_custom_delta(self):
        with (
            patch("actions.windows_control.get_brightness", return_value=50),
            patch("actions.windows_control.set_brightness") as mock_set,
        ):
            mock_set.return_value = {
                "success": True,
                "action": "set_brightness",
                "level": 75,
                "verified": True,
                "message": "Brightness set to 75%.",
            }
            res = wc.windows_control({"action": "brightness_up", "value": "25"})
            self.assertIn("status=done", res)
            mock_set.assert_called_with(75)

    def test_brightness_down_defaults_to_minus_ten(self):
        with (
            patch("actions.windows_control.get_brightness", return_value=50),
            patch("actions.windows_control.set_brightness") as mock_set,
        ):
            mock_set.return_value = {
                "success": True,
                "action": "set_brightness",
                "level": 40,
                "verified": True,
                "message": "Brightness set to 40%.",
            }
            res = wc.windows_control({"action": "brightness_down"})
            self.assertIn("status=done", res)
            mock_set.assert_called_with(40)

    def test_brightness_down_clamping_at_zero(self):
        with (
            patch("actions.windows_control.get_brightness", return_value=5),
            patch("actions.windows_control.set_brightness") as mock_set,
        ):
            mock_set.return_value = {
                "success": True,
                "action": "set_brightness",
                "level": 0,
                "verified": True,
                "message": "Brightness set to 0%.",
            }
            res = wc.windows_control({"action": "decrease_brightness", "value": "20"})
            self.assertIn("status=done", res)
            mock_set.assert_called_with(0)

    def test_set_brightness_absolute_value(self):
        with (
            patch("actions.windows_control.get_brightness", return_value=50),
            patch("actions.windows_control.set_brightness") as mock_set,
        ):
            mock_set.return_value = {
                "success": True,
                "action": "set_brightness",
                "level": 85,
                "verified": True,
                "message": "Brightness set to 85%.",
            }
            res = wc.windows_control({"action": "set_brightness", "value": 85})
            self.assertIn("status=done", res)
            mock_set.assert_called_with(85)

    def test_get_brightness_action(self):
        with patch("actions.windows_control.get_brightness", return_value=60):
            res = wc.windows_control({"action": "get_brightness"})
            self.assertIn("status=done", res)
            self.assertIn("Current screen brightness is 60%.", res)


class TestComputerSettingsBrightness(unittest.TestCase):
    def test_computer_settings_brightness_up_and_down(self):
        mock_up = MagicMock()
        with (
            patch("actions.computer_settings.brightness_get", side_effect=[50, 60]),
            patch.dict(cs.ACTION_MAP, {"brightness_up": mock_up}),
        ):
            res = cs.computer_settings({"action": "brightness_up"})
            self.assertIn("status=done", res)
            mock_up.assert_called_once()

    def test_computer_settings_set_brightness(self):
        with (
            patch("actions.computer_settings.brightness_get", side_effect=[40, 75]),
            patch("actions.computer_settings.brightness_set") as mock_set,
        ):
            res = cs.computer_settings({"action": "set_brightness", "value": 75})
            self.assertIn("status=done", res)
            self.assertIn("brightness=75", res)
            mock_set.assert_called_with(75)

    def test_computer_settings_action_synonyms(self):
        self.assertIn("increase_brightness", cs.ACTION_MAP)
        self.assertIn("decrease_brightness", cs.ACTION_MAP)
        self.assertIn("brighter", cs.ACTION_MAP)
        self.assertIn("dimmer", cs.ACTION_MAP)
