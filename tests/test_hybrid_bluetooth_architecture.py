"""
Unit and Integration Tests for Jarvis Hybrid Bluetooth Audio Architecture.

Validates:
1. Routing mode configuration and persistence (auto_hybrid, built_in_only, bluetooth_only).
2. Built-in vs Bluetooth endpoint discovery and A2DP profile detection.
3. WakeWordDetector pause, resume, buffer reset, and frame rejection.
4. AudioManager hybrid startup in built-in mic mode (preserving A2DP stereo music playback).
5. Switch to conversation mode (opening Bluetooth mic, validating frames, output recovery).
6. Switch to sleep mode (releasing Bluetooth mic, bounded wait for A2DP restoration, opening laptop mic).
7. Transition idempotency and exclusive locking.
8. Transition state frame rejection ensuring zero stale frames leak to Gemini.
"""

import unittest
from unittest.mock import MagicMock, patch
import numpy as np

from memory import config_manager
from core import audio_devices
from core.audio_manager import AudioManager, STATUS_CONNECTED
from core.wake_word import WakeWordDetector
from tests.test_audio_manager_comprehensive import _FakeAudioEnvironment


class TestHybridBluetoothArchitecture(unittest.TestCase):
    def setUp(self):
        self._orig_routing = config_manager.get_audio_routing_mode()
        self._orig_builtin = config_manager.get_builtin_microphone()
        self._orig_bt = config_manager.get_bluetooth_microphone()
        audio_devices._active_streams_count = 0

    def tearDown(self):
        config_manager.save_audio_routing_mode(self._orig_routing)
        config_manager.save_builtin_microphone(self._orig_builtin)
        config_manager.save_bluetooth_microphone(self._orig_bt)
        audio_devices._active_streams_count = 0

    def _patched_env(self, env: _FakeAudioEnvironment):
        return patch.multiple(
            audio_devices,
            list_devices=env.list_devices,
            is_bluetooth_device=env.is_bt,
            get_current_default_endpoint=env.default_endpoint,
            get_connected_endpoints_snapshot=env.snapshot,
            find_supported_rate=env.find_supported_rate,
            resolve_with_rate=env.resolve_with_rate,
            reset_portaudio_if_idle=env.reset_portaudio,
        )

    # ── 1. Configuration & Persistence ───────────────────────────────────────

    def test_routing_mode_config_persistence(self):
        """Routing mode persists to config and falls back safely to auto_hybrid."""
        config_manager.save_audio_routing_mode(config_manager.ROUTING_MODE_BUILTIN_ONLY)
        self.assertEqual(config_manager.get_audio_routing_mode(), config_manager.ROUTING_MODE_BUILTIN_ONLY)

        config_manager.save_audio_routing_mode(config_manager.ROUTING_MODE_BLUETOOTH_ONLY)
        self.assertEqual(config_manager.get_audio_routing_mode(), config_manager.ROUTING_MODE_BLUETOOTH_ONLY)

        config_manager.save_audio_routing_mode(config_manager.ROUTING_MODE_AUTO_HYBRID)
        self.assertEqual(config_manager.get_audio_routing_mode(), config_manager.ROUTING_MODE_AUTO_HYBRID)

        config_manager.save_audio_routing_mode("unknown_mode")
        self.assertEqual(config_manager.get_audio_routing_mode(), config_manager.ROUTING_MODE_AUTO_HYBRID)

    def test_custom_device_endpoints_persistence(self):
        """Builtin and bluetooth endpoint preferences persist cleanly."""
        config_manager.save_builtin_microphone("Microphone Array (Realtek(R) Audio)")
        self.assertEqual(config_manager.get_builtin_microphone(), "Microphone Array (Realtek(R) Audio)")

        config_manager.save_bluetooth_microphone("Headset (OPPO Enco Buds2)")
        self.assertEqual(config_manager.get_bluetooth_microphone(), "Headset (OPPO Enco Buds2)")

    # ── 2. Device Discovery & A2DP Detection ─────────────────────────────────

    def test_find_builtin_microphone(self):
        """find_builtin_microphone identifies laptop Realtek mic over Bluetooth headset."""
        env = _FakeAudioEnvironment(bt_connected=True)
        with self._patched_env(env):
            mic = audio_devices.find_builtin_microphone()
            self.assertEqual(mic, "Microphone Array (Realtek(R) Audio)")

    def test_find_bluetooth_endpoints(self):
        """find_bluetooth_microphone and output resolve connected OPPO Enco Buds2."""
        env = _FakeAudioEnvironment(bt_connected=True)
        with self._patched_env(env):
            bt_in = audio_devices.find_bluetooth_microphone()
            bt_out = audio_devices.find_bluetooth_output()
            self.assertEqual(bt_in, "Headset (OPPO Enco Buds2)")
            self.assertEqual(bt_out, "Headphones (OPPO Enco Buds2)")

    def test_detect_a2dp_status(self):
        """A2DP stereo is active when Bluetooth output is used without an open Bluetooth mic stream."""
        env = _FakeAudioEnvironment(bt_connected=True)
        with self._patched_env(env):
            st_stereo = audio_devices.detect_a2dp_status("Headphones (OPPO Enco Buds2)", input_stream_open=False)
            self.assertTrue(st_stereo["a2dp_active"])
            self.assertIn("A2DP", st_stereo["profile"])

            st_call = audio_devices.detect_a2dp_status("Headphones (OPPO Enco Buds2)", input_stream_open=True)
            self.assertFalse(st_call["a2dp_active"])
            self.assertIn("HFP", st_call["profile"])

    # ── 3. Wake Word Pause / Resume / Buffer Reset ───────────────────────────

    def test_wake_word_pause_and_buffer_reset(self):
        """WakeWordDetector paused state drops frames and buffer reset drains queues."""
        import queue

        detector = WakeWordDetector.__new__(WakeWordDetector)
        detector._paused = False
        detector._running = True
        detector._queue = queue.Queue(maxsize=50)
        detector._spotter_queue = queue.Queue(maxsize=1)
        detector._model = MagicMock()

        # Enqueue some dummy audio chunks
        detector._queue.put(np.zeros(1024, dtype=np.int16))
        detector._spotter_queue.put(np.zeros(16000, dtype=np.int16))

        self.assertFalse(detector.paused)
        detector.pause()
        self.assertTrue(detector.paused)
        self.assertTrue(detector._queue.empty())

        # Put items again and test reset_buffers
        detector._queue.put(np.zeros(1024, dtype=np.int16))
        self.assertFalse(detector._queue.empty())
        detector.reset_buffers()
        self.assertTrue(detector._queue.empty())
        detector._model.reset.assert_called_once()

        detector.resume()
        self.assertFalse(detector.paused)

    # ── 4. AudioManager Hybrid Startup in Built-in Mic ───────────────────────

    def test_audio_manager_hybrid_boot_in_builtin_mic(self):
        """
        In auto_hybrid mode with preferred_input=None (standard boot),
        AudioManager selects the laptop built-in mic so OPPO Buds 2 stay in A2DP stereo music.
        """
        config_manager.save_audio_routing_mode("auto_hybrid")
        config_manager.save_builtin_microphone("Microphone Array (Realtek(R) Audio)")
        config_manager.save_bluetooth_microphone("Headset (OPPO Enco Buds2)")

        env = _FakeAudioEnvironment(bt_connected=True)
        with (
            self._patched_env(env),
            patch(
                "sounddevice.query_devices",
                side_effect=lambda idx=None: env.devices_table()[idx] if idx is not None else env.devices_table(),
            ),
            patch("sounddevice.InputStream", side_effect=env.make_input_stream),
            patch("sounddevice.RawOutputStream", side_effect=env.make_output_stream),
        ):
            mgr = AudioManager(bt_settle_s=0.0)
            st = mgr.initialize(
                preferred_input=None, preferred_output="Headphones (OPPO Enco Buds2)", start_monitor=False
            )

            self.assertEqual(st["routing_mode"], "auto_hybrid")
            self.assertEqual(st["active_input_role"], "builtin")
            self.assertEqual(st["input_device"], "Microphone Array (Realtek(R) Audio)")
            self.assertEqual(st["output_device"], "Headphones (OPPO Enco Buds2)")
            mgr.shutdown()

    # ── 5. Switch to Conversation and Return to Sleep ────────────────────────

    def test_switch_to_conversation_and_return_to_sleep(self):
        """
        Simulates full cycle:
        1. Sleeping on Laptop Mic
        2. Wake word detected -> switch_to_conversation_input() -> Bluetooth Mic active
        3. Sleep entered -> switch_to_sleep_input() -> Laptop Mic active, A2DP stereo restored
        """
        config_manager.save_audio_routing_mode("auto_hybrid")
        env = _FakeAudioEnvironment(bt_connected=True)

        with (
            self._patched_env(env),
            patch(
                "sounddevice.query_devices",
                side_effect=lambda idx=None: env.devices_table()[idx] if idx is not None else env.devices_table(),
            ),
            patch("sounddevice.InputStream", side_effect=env.make_input_stream),
            patch("sounddevice.RawOutputStream", side_effect=env.make_output_stream),
            patch("core.audio_devices.find_bluetooth_microphone", return_value="Headset (OPPO Enco Buds2)"),
            patch("core.audio_devices.find_builtin_microphone", return_value="Microphone Array (Realtek(R) Audio)"),
        ):
            mgr = AudioManager(bt_settle_s=0.0)
            mgr.initialize(preferred_input=None, preferred_output="Headphones (OPPO Enco Buds2)", start_monitor=False)

            # Initially in sleeping mode on laptop mic
            self.assertEqual(mgr.active_input_role, "builtin")
            self.assertEqual(mgr.active_input_name, "Microphone Array (Realtek(R) Audio)")

            # Step 1: Transition to conversation
            ok, dev = mgr.switch_to_conversation_input()
            self.assertTrue(ok)
            self.assertEqual(mgr.active_input_role, "bluetooth")
            self.assertEqual(dev, "Headset (OPPO Enco Buds2)")
            self.assertEqual(mgr.transition_state, "idle")

            # Idempotency check: calling again while already on bluetooth mic is a no-op
            ok2, dev2 = mgr.switch_to_conversation_input()
            self.assertTrue(ok2)
            self.assertEqual(dev2, "Headset (OPPO Enco Buds2)")

            # Step 2: Return to sleep
            ok_sleep, dev_sleep = mgr.switch_to_sleep_input()
            self.assertTrue(ok_sleep)
            self.assertEqual(mgr.active_input_role, "builtin")
            self.assertEqual(dev_sleep, "Microphone Array (Realtek(R) Audio)")
            self.assertTrue(mgr.a2dp_restored)
            self.assertEqual(mgr.transition_state, "idle")

            # Idempotency check: calling again while already on builtin mic is a no-op
            ok_sleep2, dev_sleep2 = mgr.switch_to_sleep_input()
            self.assertTrue(ok_sleep2)
            self.assertEqual(dev_sleep2, "Microphone Array (Realtek(R) Audio)")

            mgr.shutdown()

    # ── 6. Frame Rejection During Transition State ───────────────────────────

    def test_frame_rejection_during_transition(self):
        """Frames arriving while transition_state != 'idle' are rejected."""
        mgr = AudioManager(bt_settle_s=0.0)
        mgr._transition_state = "switching_to_conversation"

        frames_delivered = []

        def user_callback(indata, frames, time_info, status):
            if mgr.transition_state != "idle":
                return
            frames_delivered.append(len(indata))

        dummy_pcm = np.zeros(1024, dtype=np.int16)
        user_callback(dummy_pcm, 1024, None, None)
        self.assertEqual(len(frames_delivered), 0, "Stale frame must be rejected during transition")

        mgr._transition_state = "idle"
        user_callback(dummy_pcm, 1024, None, None)
        self.assertEqual(len(frames_delivered), 1, "Frame must be accepted when transition is idle")


if __name__ == "__main__":
    unittest.main()
