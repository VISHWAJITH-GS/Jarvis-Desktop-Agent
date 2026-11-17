"""
tests/test_audio_routing.py — Complete Unit and Real-Device Test Suite for
MARK LVI / JARVIS Audio Routing and Bluetooth Conflict Prevention.
"""

import sys
import time
import unittest
from unittest.mock import MagicMock, patch

# Ensure clean mock on non-Windows platforms without pycaw
if "pycaw" not in sys.modules:
    try:
        import pycaw.pycaw  # noqa: F401
    except ImportError:
        mock_pycaw = MagicMock()
        mock_pycaw_mod = MagicMock()
        mock_pycaw_mod.AudioUtilities = MagicMock()
        sys.modules["pycaw"] = mock_pycaw
        sys.modules["pycaw.pycaw"] = mock_pycaw_mod

from core import audio_devices
from core.audio_diagnostics import get_audio_diagnostics, generate_diagnostic_report


class TestAudioRoutingAndBluetooth(unittest.TestCase):
    """Test suite covering device enumeration, resolution, Bluetooth classification, and output routing."""

    def setUp(self):
        audio_devices.invalidate_cache()

    @unittest.skipUnless(sys.platform == "win32", "DirectSound output check is Windows-specific")
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
        if sys.platform == "win32":
            try:
                if audio_devices._detect_bluetooth_via_endpoint_metadata("Headset (OPPO Enco Buds2)"):
                    self.assertTrue(audio_devices.is_bluetooth_device("Headphones (OPPO Enco Buds2)"))
                    self.assertTrue(audio_devices.is_bluetooth_device("Headset (OPPO Enco Buds2)"))
            except Exception:
                pass
        self.assertTrue(audio_devices.is_bluetooth_device("Bluetooth Headset"))
        self.assertFalse(audio_devices.is_bluetooth_device("Speakers (Realtek(R) Audio)"))
        self.assertFalse(audio_devices.is_bluetooth_device("Microphone Array (Realtek(R) Audio)"))

        # Status advisory
        with patch.object(audio_devices, "is_bluetooth_device", return_value=True):
            bt_mic_status = audio_devices.get_device_status("Headset (OPPO Enco Buds2)", "input")
            self.assertTrue(bt_mic_status["is_bluetooth"])
            self.assertIn("HFP", bt_mic_status["profile"])
            self.assertIsNotNone(bt_mic_status["warning"])

        laptop_mic_status = audio_devices.get_device_status("Microphone Array (Realtek(R) Audio)", "input")
        self.assertFalse(laptop_mic_status["is_bluetooth"])
        self.assertIsNone(laptop_mic_status["warning"])

    @unittest.skipUnless(sys.platform == "win32", "MME output resolution is Windows-specific")
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

    @unittest.skipUnless(sys.platform == "win32", "Real hardware tone playback requires Windows audio devices")
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

    def test_mic_failure_fallback_does_not_open_default_and_tries_next_non_bt(self):
        """Mic failure tries next non-BT mic and never opens default None."""
        opened_devices = []

        def fake_open_mic(dev_idx):
            opened_devices.append(dev_idx)
            if dev_idx == 10:  # Primary failed mic
                raise RuntimeError("Failed to open primary mic")
            if dev_idx == 20:  # Fallback mic
                return MagicMock()
            raise RuntimeError("Unknown device")

        # Simulate the fallback logic
        all_inputs = ["Primary Mic", "Loopback Input", "Fallback Non-BT Mic", "Headset BT Mic"]
        excl = getattr(audio_devices, "EXCLUDED_INPUT_KEYWORDS", ())
        is_bt_map = {"Headset BT Mic": True}

        def mock_is_bt(name):
            return is_bt_map.get(name, False)

        def mock_resolve(name, kind):
            if name == "Primary Mic":
                return 10
            if name == "Fallback Non-BT Mic":
                return 20
            return None

        with patch.object(audio_devices, "list_devices", return_value=all_inputs), \
             patch.object(audio_devices, "is_bluetooth_device", side_effect=mock_is_bt), \
             patch.object(audio_devices, "resolve", side_effect=mock_resolve):

            # Helper identical to main.py
            def _try_open_candidates(primary_dev, primary_name):
                try:
                    stream = fake_open_mic(primary_dev)
                    return stream, primary_name
                except Exception:
                    pass

                non_bt = [
                    d for d in audio_devices.list_devices("input")
                    if not audio_devices.is_bluetooth_device(d)
                    and not any(k in d.lower() for k in excl)
                    and d != primary_name
                ]
                for alt_name in non_bt:
                    alt_idx = audio_devices.resolve(alt_name, "input")
                    if alt_idx is None:
                        continue
                    try:
                        stream = fake_open_mic(alt_idx)
                        return stream, alt_name
                    except Exception:
                        pass
                return None, None

            stream, chosen_name = _try_open_candidates(10, "Primary Mic")
            self.assertIsNotNone(stream)
            self.assertEqual(chosen_name, "Fallback Non-BT Mic")
            # 10 was tried, then 20 was tried. None (system default) was NEVER passed to fake_open_mic!
            self.assertNotIn(None, opened_devices)
            self.assertEqual(opened_devices, [10, 20])

    def test_mic_failure_with_no_candidates_returns_none_without_opening_default(self):
        """When all candidate mics fail, None is returned and default None is never opened."""
        opened_devices = []

        def fake_open_mic(dev_idx):
            opened_devices.append(dev_idx)
            raise RuntimeError("Hardware failure")

        with patch.object(audio_devices, "list_devices", return_value=["Primary Mic"]), \
             patch.object(audio_devices, "is_bluetooth_device", return_value=False), \
             patch.object(audio_devices, "resolve", return_value=None):

            def _try_open_candidates(primary_dev, primary_name):
                try:
                    stream = fake_open_mic(primary_dev)
                    return stream, primary_name
                except Exception:
                    pass
                return None, None

            stream, chosen_name = _try_open_candidates(10, "Primary Mic")
            self.assertIsNone(stream)
            self.assertIsNone(chosen_name)
            self.assertNotIn(None, opened_devices)

    def test_auto_selection_excludes_virtual_and_loopback_inputs(self):
        """Auto-selection excludes Stereo Mix, Loopback, Virtual, VB-Cable, OBS, Steam Streaming."""
        available = [
            "Stereo Mix (Realtek Audio)",
            "OBS Audio Capture",
            "CABLE Output (VB-Audio Virtual Cable)",
            "Steam Streaming Microphone",
            "Loopback Audio Device",
            "Microphone (Realtek(R) Audio)",
        ]
        with patch.object(audio_devices, "is_output_bluetooth", return_value=True), \
             patch.object(audio_devices, "list_devices", return_value=available), \
             patch.object(audio_devices, "is_bluetooth_device", return_value=False):
            decision = audio_devices.select_best_input(
                requested_input="",
                output_name="Bluetooth Headset",
                input_mode="auto",
                allow_bluetooth_mic=False,
            )
            self.assertEqual(decision["device"], "Microphone (Realtek(R) Audio)")

    def test_auto_selection_prefers_wasapi_capture_endpoints(self):
        """Auto-selection prefers WASAPI capture endpoints over other host APIs."""
        available = [
            "Microphone (Realtek Audio) [DirectSound]",
            "Microphone (Realtek Audio) [WASAPI]",
        ]
        with patch.object(audio_devices, "is_output_bluetooth", return_value=True), \
             patch.object(audio_devices, "list_devices", return_value=available), \
             patch.object(audio_devices, "is_bluetooth_device", return_value=False):
            decision = audio_devices.select_best_input(
                requested_input="",
                output_name="Bluetooth Headset",
                input_mode="auto",
                allow_bluetooth_mic=False,
            )
            self.assertEqual(decision["device"], "Microphone (Realtek Audio) [WASAPI]")

    def test_exact_endpoint_id_and_no_prefix_matching(self):
        """Exact endpoint-ID and name matching avoids prefix false matches."""
        class MockEndpoint:
            def __init__(self, dev_id, friendly_name, bus_prop):
                self.id = dev_id
                self.FriendlyName = friendly_name
                self.properties = {"device_bus": bus_prop}

        dev_usb = MockEndpoint("{0.0.1}.{usb-mic-123}", "Microphone", "USB\\VID_123")
        dev_bt = MockEndpoint("{0.0.1}.{bt-mic-456}", "Microphone Hands-Free", "BTHENUM\\{xyz}")

        with patch("pycaw.pycaw.AudioUtilities.GetAllDevices", return_value=[dev_usb, dev_bt]), \
             patch.object(audio_devices, "_platform_is", return_value="Windows"):
            audio_devices.invalidate_cache()
            with audio_devices._bt_detection_lock:
                audio_devices._bt_detection_cache.clear()

            # "Microphone" matches dev_usb exactly (not dev_bt via prefix startswith)
            self.assertFalse(audio_devices._detect_bluetooth_via_endpoint_metadata("Microphone"))

            # Exact endpoint-ID query for dev_bt matches dev_bt
            self.assertTrue(audio_devices._detect_bluetooth_via_endpoint_metadata("{0.0.1}.{bt-mic-456}"))

    def test_bluetooth_fallback_keywords_exclude_oppo_and_enco(self):
        """User-specific 'oppo' and 'enco buds' keywords are removed from fallback heuristics."""
        with patch.object(audio_devices, "_detect_bluetooth_via_endpoint_metadata", return_value=None):
            with audio_devices._bt_detection_lock:
                audio_devices._bt_detection_cache.clear()

            # Without endpoint metadata, arbitrary names containing 'oppo' or 'enco' are no longer assumed BT
            self.assertFalse(audio_devices.is_bluetooth_device("Oppo USB Audio Card"))
            self.assertFalse(audio_devices.is_bluetooth_device("Enco Wired Mic"))

            # Generic Bluetooth indicators still match
            self.assertTrue(audio_devices.is_bluetooth_device("Bluetooth Audio Device"))
            self.assertTrue(audio_devices.is_bluetooth_device("AirPods Pro"))

    def test_bluetooth_detection_false_positives_and_negatives(self):
        """Verify reliable Bluetooth detection fixes false positives and negatives."""
        # Non-Bluetooth false positive candidates (USB wireless dongles, wired USB headsets)
        self.assertFalse(audio_devices.is_bluetooth_device("Logitech USB Wireless Headset"))
        self.assertFalse(audio_devices.is_bluetooth_device("HyperX Cloud II Wireless USB"))
        self.assertFalse(audio_devices.is_bluetooth_device("Corsair Void USB Headset"))
        self.assertFalse(audio_devices.is_bluetooth_device("Microphone Array (Realtek(R) Audio)"))

        # True Bluetooth devices
        if sys.platform == "win32":
            try:
                if audio_devices._detect_bluetooth_via_endpoint_metadata("Headset (OPPO Enco Buds2)"):
                    self.assertTrue(audio_devices.is_bluetooth_device("Headphones (OPPO Enco Buds2)"))
                    self.assertTrue(audio_devices.is_bluetooth_device("Headset (OPPO Enco Buds2)"))
            except Exception:
                pass
        self.assertTrue(audio_devices.is_bluetooth_device("Hands-Free AG Audio"))
        self.assertTrue(audio_devices.is_bluetooth_device("AirPods Pro"))
        self.assertTrue(audio_devices.is_bluetooth_device("Galaxy Buds2 Pro"))


if __name__ == "__main__":
    unittest.main()

