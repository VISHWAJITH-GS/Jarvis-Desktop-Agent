"""
tests/test_audio_routing.py — Complete Unit and Real-Device Test Suite for
MARK LVI / JARVIS Audio Routing and Bluetooth Conflict Prevention.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from core import audio_devices
from core.audio_diagnostics import get_audio_diagnostics, generate_diagnostic_report


class TestAudioRoutingAndBluetooth(unittest.TestCase):
    """Test suite covering device enumeration, resolution, Bluetooth classification, and output routing."""

    def setUp(self):
        audio_devices.invalidate_cache()

    def test_directsound_output_never_selected(self):
        """Verify that DirectSound output devices are strictly rejected to prevent silent sink."""
        # Find any directsound output device if available
        import sounddevice as sd
        apis = [a.get("name", "").lower() for a in sd.query_hostapis()]
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_output_channels", 0) > 0:
                hostapi_id = dev.get("hostapi", -1)
                api_name = apis[hostapi_id] if 0 <= hostapi_id < len(apis) else ""
                if "directsound" in api_name:
                    usable = audio_devices._usable(idx, "output")
                    self.assertFalse(usable, f"DirectSound device #{idx} must NOT be marked usable for output!")

    def test_bluetooth_profile_classification(self):
        """Verify Bluetooth detection and profile warnings."""
        # OPPO Enco Buds 2 detection
        self.assertTrue(audio_devices.is_bluetooth_device("Headphones (OPPO Enco Buds2)"))
        self.assertTrue(audio_devices.is_bluetooth_device("Headset (OPPO Enco Buds2)"))
        self.assertFalse(audio_devices.is_bluetooth_device("Speakers (Realtek(R) Audio)"))
        self.assertFalse(audio_devices.is_bluetooth_device("Microphone Array (Realtek(R) Audio)"))

        # Status advisory
        bt_mic_status = audio_devices.get_device_status("Headset (OPPO Enco Buds2)", "input")
        self.assertTrue(bt_mic_status["is_bluetooth"])
        self.assertIn("HFP", bt_mic_status["profile"])
        self.assertIsNotNone(bt_mic_status["warning"])

        laptop_mic_status = audio_devices.get_device_status("Microphone Array (Realtek(R) Audio)", "input")
        self.assertFalse(laptop_mic_status["is_bluetooth"])
        self.assertIsNone(laptop_mic_status["warning"])

    def test_device_resolution_and_mme_selection_for_speakers(self):
        """Verify that output devices resolve to working MME indices and not silent sinks."""
        import sounddevice as sd
        apis = [a.get("name", "").lower() for a in sd.query_hostapis()]

        # Resolve Realtek speakers
        dev_spk = audio_devices.resolve("Speakers (Realtek(R) Audio)", "output")
        if dev_spk is not None:
            dev_info = sd.query_devices(dev_spk)
            api_name = apis[dev_info["hostapi"]]
            self.assertIn("mme", api_name, "Output device must use MME backend on Windows.")

        # Resolve OPPO earphones if present
        dev_oppo = audio_devices.resolve("Headphones (OPPO Enco Buds2)", "output")
        if dev_oppo is not None:
            dev_info = sd.query_devices(dev_oppo)
            api_name = apis[dev_info["hostapi"]]
            self.assertIn("mme", api_name, "OPPO earphones must use MME backend on Windows.")

    def test_play_tone_real_consumption(self):
        """Verify play_tone tests consumption and returns ok for active output."""
        res = audio_devices.play_tone("Speakers (Realtek(R) Audio)", seconds=0.3)
        self.assertTrue(res["ok"])
        self.assertIn("Chime played", res["verdict"])

    def test_diagnostics_report_generation(self):
        """Verify diagnostics report captures streams without raising exceptions."""
        diag = get_audio_diagnostics()
        self.assertIn("timestamp", diag)
        self.assertIn("resolved_streams", diag)
        self.assertIn("input", diag["resolved_streams"])
        self.assertIn("output", diag["resolved_streams"])

        report = generate_diagnostic_report()
        self.assertIn("# JARVIS Audio & Bluetooth Diagnostics Report", report)
        self.assertIn("## 1. Input Stream", report)
        self.assertIn("## 2. Output Stream", report)

    def test_cache_invalidation_rescans(self):
        """Verify invalidate_cache clears cached lists and chosen APIs."""
        audio_devices.list_devices("output")
        self.assertIsNotNone(audio_devices._cache)
        audio_devices.invalidate_cache()
        self.assertIsNone(audio_devices._cache)

    def test_bluetooth_output_default_input_auto_selects_non_bt_mic(self):
        """Bluetooth output + System Default input in auto mode -> a non-Bluetooth input is chosen."""
        with patch.object(audio_devices, "is_output_bluetooth", return_value=True), \
             patch.object(audio_devices, "list_devices", return_value=[
                 "Headset (OPPO Enco Buds2)",
                 "Microphone Array (Realtek(R) Audio)",
             ]):
            decision = audio_devices.select_best_input(
                requested_input="",
                output_name="Headphones (OPPO Enco Buds2)",
                input_mode="auto",
                allow_bluetooth_mic=False,
            )
            self.assertEqual(decision["device"], "Microphone Array (Realtek(R) Audio)")
            self.assertFalse(decision["is_bluetooth"])
            self.assertFalse(decision["quality_drop_warning"])
            self.assertIn("Auto-selected non-Bluetooth input", decision["reason"])

    def test_bluetooth_mic_never_chosen_in_auto_mode_unless_allowed(self):
        """Bluetooth mic never chosen in auto mode; chosen only when allow_bluetooth_mic is true."""
        with patch.object(audio_devices, "is_output_bluetooth", return_value=True), \
             patch.object(audio_devices, "list_devices", return_value=[
                 "Headset (OPPO Enco Buds2)",
                 "Microphone Array (Realtek(R) Audio)",
             ]):
            # 1. In auto mode with allow_bluetooth_mic=False -> overrides BT mic with non-BT mic
            dec_auto = audio_devices.select_best_input(
                requested_input="Headset (OPPO Enco Buds2)",
                output_name="Headphones (OPPO Enco Buds2)",
                input_mode="auto",
                allow_bluetooth_mic=False,
            )
            self.assertEqual(dec_auto["device"], "Microphone Array (Realtek(R) Audio)")
            self.assertFalse(dec_auto["is_bluetooth"])

            # 2. In auto mode with allow_bluetooth_mic=True -> permits BT mic
            dec_allowed = audio_devices.select_best_input(
                requested_input="Headset (OPPO Enco Buds2)",
                output_name="Headphones (OPPO Enco Buds2)",
                input_mode="auto",
                allow_bluetooth_mic=True,
            )
            self.assertEqual(dec_allowed["device"], "Headset (OPPO Enco Buds2)")
            self.assertTrue(dec_allowed["is_bluetooth"])
            self.assertTrue(dec_allowed["quality_drop_warning"])

    def test_explicit_non_bluetooth_user_choice_is_respected(self):
        """Explicit non-Bluetooth user choice is respected and never overridden."""
        with patch.object(audio_devices, "is_output_bluetooth", return_value=True):
            decision = audio_devices.select_best_input(
                requested_input="Microphone Array (Realtek(R) Audio)",
                output_name="Headphones (OPPO Enco Buds2)",
                input_mode="auto",
                allow_bluetooth_mic=False,
            )
            self.assertEqual(decision["device"], "Microphone Array (Realtek(R) Audio)")
            self.assertFalse(decision["is_bluetooth"])
            self.assertIn("Explicit non-Bluetooth user selection", decision["reason"])

    def test_fallback_path_when_no_non_bluetooth_mic_exists(self):
        """Fallback path when no non-Bluetooth mic exists falls back and warns."""
        with patch.object(audio_devices, "is_output_bluetooth", return_value=True), \
             patch.object(audio_devices, "list_devices", return_value=[
                 "Headset (OPPO Enco Buds2)"
             ]):
            decision = audio_devices.select_best_input(
                requested_input="",
                output_name="Headphones (OPPO Enco Buds2)",
                input_mode="auto",
                allow_bluetooth_mic=False,
            )
            self.assertTrue(decision["quality_drop_warning"])
            self.assertTrue(decision["is_bluetooth"])
            self.assertIn("falling back", decision["reason"])

    def test_bluetooth_detection_false_positives_and_negatives(self):
        """Verify reliable Bluetooth detection fixes false positives and negatives."""
        # Non-Bluetooth false positive candidates (USB wireless dongles, wired USB headsets)
        self.assertFalse(audio_devices.is_bluetooth_device("Logitech USB Wireless Headset"))
        self.assertFalse(audio_devices.is_bluetooth_device("HyperX Cloud II Wireless USB"))
        self.assertFalse(audio_devices.is_bluetooth_device("Corsair Void USB Headset"))
        self.assertFalse(audio_devices.is_bluetooth_device("Microphone Array (Realtek(R) Audio)"))

        # True Bluetooth devices
        self.assertTrue(audio_devices.is_bluetooth_device("Headphones (OPPO Enco Buds2)"))
        self.assertTrue(audio_devices.is_bluetooth_device("Headset (OPPO Enco Buds2)"))
        self.assertTrue(audio_devices.is_bluetooth_device("Hands-Free AG Audio"))
        self.assertTrue(audio_devices.is_bluetooth_device("AirPods Pro"))
        self.assertTrue(audio_devices.is_bluetooth_device("Galaxy Buds2 Pro"))


if __name__ == "__main__":
    unittest.main()

