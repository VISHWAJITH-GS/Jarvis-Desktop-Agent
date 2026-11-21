"""
tests/test_audio_manager_comprehensive.py — Comprehensive Automated Test Suite
covering Scenarios 1–12 (Bluetooth Startup/Hot-Switch/Disconnect/Reconnect,
Independent I/O Switching, Sample-Rate Resampling, Crash Prevention, Gemini Live
Preservation, Graceful Shutdown), Confirmation Protocols, Voice Commands, and
nvidia-ml-py Warning-Free GPU Monitoring.
"""

from __future__ import annotations


import threading
import time
import unittest
import warnings
from unittest.mock import MagicMock, patch

import numpy as np

from core import audio_devices, confirm
from core.audio_manager import (
    AudioManager,
    STATUS_CONNECTED,
    STATUS_DISCONNECTED,
    STATUS_SYSTEM_DEFAULT,
    set_audio_manager,
)
from core.permissions import ActionPermissionManager, ActionRiskLevel


class _FakeStream:
    """Deterministic fake sounddevice InputStream / RawOutputStream."""

    def __init__(self, kind: str, samplerate: int, device, callback=None, fail_on_write=False):
        self.kind = kind
        self.samplerate = samplerate
        self.device = device
        self.callback = callback
        self.latency = 0.04
        self.started = False
        self.stopped = False
        self.closed = False
        self.writes: list[bytes] = []
        self.fail_on_write = fail_on_write

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    def write(self, data: bytes):
        if self.fail_on_write:
            raise OSError("PortAudioError: Bluetooth device disconnected mid-write")
        self.writes.append(data)


class _FakeAudioEnvironment:
    """Simulated Windows audio + Bluetooth hardware topology for deterministic testing."""

    def __init__(self, bt_connected: bool = False, supported_rates: dict | None = None):
        self.bt_connected = bt_connected
        self.supported_rates = supported_rates or {}
        self.created_streams: list[_FakeStream] = []
        self.portaudio_resets_while_active = 0
        self.portaudio_resets_total = 0
        self.fail_next_output_write = False

    def devices_table(self):
        devs = [
            {
                "name": "Microphone Array (Realtek(R) Audio)",
                "hostapi": 0,
                "max_input_channels": 2,
                "max_output_channels": 0,
                "default_samplerate": 48000.0,
            },
            {
                "name": "Speakers (Realtek(R) Audio)",
                "hostapi": 1,
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000.0,
            },
        ]
        if self.bt_connected:
            devs.append(
                {
                    "name": "Headset (OPPO Enco Buds2)",
                    "hostapi": 0,
                    "max_input_channels": 1,
                    "max_output_channels": 0,
                    "default_samplerate": 16000.0,
                }
            )
            devs.append(
                {
                    "name": "Headphones (OPPO Enco Buds2)",
                    "hostapi": 1,
                    "max_input_channels": 0,
                    "max_output_channels": 2,
                    "default_samplerate": 48000.0,
                }
            )
        return devs

    def hostapis(self):
        return [{"name": "Windows DirectSound"}, {"name": "MME"}]

    def list_devices(self, kind: str, refresh: bool = False):
        if kind == "input":
            base = ["Microphone Array (Realtek(R) Audio)"]
            if self.bt_connected:
                base.append("Headset (OPPO Enco Buds2)")
            return base
        base = ["Speakers (Realtek(R) Audio)"]
        if self.bt_connected:
            base.append("Headphones (OPPO Enco Buds2)")
        return base

    def is_bt(self, name: str) -> bool:
        low = (name or "").lower()
        return any(k in low for k in ("oppo", "enco", "bluetooth", "hands-free", "headset"))

    def default_endpoint(self, kind: str = "output"):
        if self.bt_connected:
            if kind == "output":
                return ("{bt-out}", "Headphones (OPPO Enco Buds2)")
            return ("{bt-in}", "Headset (OPPO Enco Buds2)")
        if kind == "output":
            return ("{spk-out}", "Speakers (Realtek(R) Audio)")
        return ("{mic-in}", "Microphone Array (Realtek(R) Audio)")

    def snapshot(self):
        snap = {"{spk-out}", "Speakers (Realtek(R) Audio)", "{mic-in}", "Microphone Array (Realtek(R) Audio)"}
        if self.bt_connected:
            snap |= {"{bt-out}", "Headphones (OPPO Enco Buds2)", "{bt-in}", "Headset (OPPO Enco Buds2)"}
        return snap

    def find_supported_rate(self, idx, kind: str, preferred_rate=None):
        devs = self.devices_table()
        if idx is None:
            dev_name = self.default_endpoint(kind)[1]
        elif 0 <= idx < len(devs):
            dev_name = devs[idx]["name"]
        else:
            return None
        if dev_name in self.supported_rates:
            return self.supported_rates[dev_name]
        return int(preferred_rate or (16000 if kind == "input" else 24000))

    def resolve_with_rate(self, name: str, kind: str):
        devs = self.devices_table()
        wanted = (name or "").strip().lower()
        if not wanted or wanted == audio_devices.DEFAULT_LABEL.lower():
            return None, self.find_supported_rate(None, kind)
        chan_key = "max_input_channels" if kind == "input" else "max_output_channels"
        for i, d in enumerate(devs):
            if d[chan_key] <= 0:
                continue
            dl = d["name"].lower()
            if dl == wanted or wanted in dl or dl in wanted:
                rate = self.find_supported_rate(i, kind)
                if rate is not None:
                    return i, rate
        return None, None

    def make_input_stream(self, **kwargs):
        st = _FakeStream("input", kwargs["samplerate"], kwargs.get("device"), callback=kwargs.get("callback"))
        self.created_streams.append(st)
        return st

    def make_output_stream(self, **kwargs):
        fail_w = self.fail_next_output_write
        self.fail_next_output_write = False
        st = _FakeStream("output", kwargs["samplerate"], kwargs.get("device"), fail_on_write=fail_w)
        self.created_streams.append(st)
        return st

    def reset_portaudio(self):
        self.portaudio_resets_total += 1
        if audio_devices._active_streams_count > 0:
            self.portaudio_resets_while_active += 1
            return False
        return True

    def open_streams(self):
        return [s for s in self.created_streams if not s.closed]


class TestAudioManagerAll12Scenarios(unittest.TestCase):
    """Implements all 12 required audio/Bluetooth reliability & crash-prevention tests."""

    def setUp(self):
        audio_devices._active_streams_count = 0
        set_audio_manager(None)

    def tearDown(self):
        set_audio_manager(None)
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

    def test_01_bluetooth_connected_before_startup(self):
        """Test 1 (Scenario A): OPPO Enco Buds2 connected before launch initializes both input & output."""
        env = _FakeAudioEnvironment(bt_connected=True)
        status_updates = []
        with (
            self._patched_env(env),
            patch(
                "sounddevice.query_devices",
                side_effect=lambda idx=None: env.devices_table()[idx] if idx is not None else env.devices_table(),
            ),
            patch("sounddevice.InputStream", side_effect=env.make_input_stream),
            patch("sounddevice.RawOutputStream", side_effect=env.make_output_stream),
        ):
            mgr = AudioManager(on_status_change=status_updates.append, bt_settle_s=0.0)
            st = mgr.initialize(
                preferred_input="Headset (OPPO Enco Buds2)",
                preferred_output="Headphones (OPPO Enco Buds2)",
                start_monitor=False,
            )
            self.assertEqual(st["input_device"], "Headset (OPPO Enco Buds2)")
            self.assertEqual(st["output_device"], "Headphones (OPPO Enco Buds2)")
            self.assertEqual(st["input_status"], STATUS_CONNECTED)
            self.assertEqual(st["output_status"], STATUS_CONNECTED)
            self.assertTrue(st["bluetooth_connected"])
            self.assertEqual(len(env.open_streams()), 2)
            mgr.shutdown()

    def test_02_bluetooth_connected_after_startup(self):
        """Test 2 (Scenario B): Launch with laptop audio, then connect OPPO Enco Buds2 -> hot-switches automatically."""
        env = _FakeAudioEnvironment(bt_connected=False)
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
            st0 = mgr.initialize(preferred_input="", preferred_output="", start_monitor=False)
            self.assertIn("Realtek", st0["input_device"])
            self.assertIn("Realtek", st0["output_device"])
            self.assertFalse(st0["bluetooth_connected"])

            # Connect OPPO Enco Buds2 while JARVIS is running!
            env.bt_connected = True
            switched = mgr._poll_device_changes_once()
            self.assertTrue(switched)

            st1 = mgr.get_status()
            self.assertIn("OPPO Enco Buds2", st1["output_device"])
            self.assertIn("OPPO Enco Buds2", st1["input_device"])
            self.assertTrue(st1["bluetooth_connected"])
            # Critical crash-prevention check: PortAudio was NEVER reset while streams were active!
            self.assertGreaterEqual(env.portaudio_resets_total, 1)
            self.assertEqual(env.portaudio_resets_while_active, 0)
            # Zero duplicate streams
            self.assertEqual(len(env.open_streams()), 2)
            mgr.shutdown()

    def test_03_bluetooth_disconnected_while_running(self):
        """Test 3: Disconnecting Bluetooth mid-run gracefully falls back to Windows default laptop audio."""
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
            mgr.initialize(
                preferred_input="Headset (OPPO Enco Buds2)",
                preferred_output="Headphones (OPPO Enco Buds2)",
                start_monitor=False,
            )

            # Disconnect Bluetooth
            env.bt_connected = False
            mgr._poll_device_changes_once()

            st = mgr.get_status()
            self.assertIn("Realtek", st["output_device"])
            self.assertIn("Realtek", st["input_device"])
            self.assertEqual(st["output_status"], STATUS_SYSTEM_DEFAULT)
            self.assertEqual(st["input_status"], STATUS_SYSTEM_DEFAULT)
            self.assertFalse(st["bluetooth_connected"])
            self.assertEqual(len(env.open_streams()), 2)
            mgr.shutdown()

    def test_04_bluetooth_reconnected_restores_preferred_device(self):
        """Test 4: When OPPO Enco Buds2 reconnects, preferred device is automatically restored."""
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
            mgr.initialize(
                preferred_input="Headset (OPPO Enco Buds2)",
                preferred_output="Headphones (OPPO Enco Buds2)",
                start_monitor=False,
            )

            # Disconnect -> falls back to Realtek
            env.bt_connected = False
            mgr._poll_device_changes_once()
            self.assertIn("Realtek", mgr.active_output_name)

            # Reconnect -> restores OPPO Enco Buds2 automatically!
            env.bt_connected = True
            mgr._poll_device_changes_once()
            st = mgr.get_status()
            self.assertEqual(st["output_device"], "Headphones (OPPO Enco Buds2)")
            self.assertEqual(st["input_device"], "Headset (OPPO Enco Buds2)")
            self.assertEqual(st["output_status"], STATUS_CONNECTED)
            self.assertTrue(st["bluetooth_connected"])
            mgr.shutdown()

    def test_05_switch_microphone_independently(self):
        """Test 5: Switching microphone independently leaves speaker untouched."""
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
            mgr.initialize(
                preferred_input="Headset (OPPO Enco Buds2)",
                preferred_output="Headphones (OPPO Enco Buds2)",
                start_monitor=False,
            )
            out_before = mgr._out_stream

            res = mgr.switch_input("laptop microphone", persist=False)
            self.assertTrue(res["success"])
            self.assertTrue(res["verified"])
            self.assertIn("Realtek", mgr.active_input_name)
            # Speaker stream remained untouched!
            self.assertIs(mgr._out_stream, out_before)
            self.assertEqual(mgr.active_output_name, "Headphones (OPPO Enco Buds2)")
            mgr.shutdown()

    def test_06_switch_speaker_independently(self):
        """Test 6: Switching speaker independently leaves microphone untouched."""
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
            mgr.initialize(
                preferred_input="Microphone Array (Realtek(R) Audio)",
                preferred_output="Speakers (Realtek(R) Audio)",
                start_monitor=False,
            )
            in_before = mgr._in_stream

            res = mgr.switch_output("OPPO Enco Buds2", persist=False)
            self.assertTrue(res["success"])
            self.assertTrue(res["verified"])
            self.assertEqual(mgr.active_output_name, "Headphones (OPPO Enco Buds2)")
            # Mic stream remained untouched!
            self.assertIs(mgr._in_stream, in_before)
            self.assertIn("Realtek", mgr.active_input_name)
            mgr.shutdown()

    def test_07_switch_both_and_reset_default(self):
        """Test 7: Switch both microphone & speaker, then reset to Windows default."""
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
            mgr.initialize(
                preferred_input="Microphone Array (Realtek(R) Audio)",
                preferred_output="Speakers (Realtek(R) Audio)",
                start_monitor=False,
            )

            res = mgr.switch_devices("OPPO Enco Buds2", "OPPO Enco Buds2", persist=False)
            self.assertTrue(res["success"])
            self.assertIn("OPPO Enco Buds2", mgr.active_input_name)
            self.assertIn("OPPO Enco Buds2", mgr.active_output_name)

            reset = mgr.reset_to_default(persist=False)
            self.assertTrue(reset["success"])
            self.assertEqual(len(env.open_streams()), 2)
            mgr.shutdown()

    def test_08_selected_device_unavailable_during_init(self):
        """Test 8: Saved OPPO Enco Buds2 unavailable at startup -> falls back cleanly, never blocks boot."""
        env = _FakeAudioEnvironment(bt_connected=False)
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
                preferred_input="Headset (OPPO Enco Buds2)",
                preferred_output="Headphones (OPPO Enco Buds2)",
                start_monitor=False,
            )
            self.assertTrue(mgr.initialized)
            self.assertIn("Realtek", st["input_device"])
            self.assertIn("Realtek", st["output_device"])
            self.assertEqual(st["input_status"], STATUS_SYSTEM_DEFAULT)
            self.assertEqual(st["output_status"], STATUS_SYSTEM_DEFAULT)
            # Explicit switch to unavailable device MUST fail honestly (never falsely claim success!)
            bad = mgr.switch_output("NonExistent Headset 9000", persist=False)
            self.assertFalse(bad["success"])
            self.assertFalse(bad["verified"])
            mgr.shutdown()

    def test_09_unsupported_sample_rate_resampling(self):
        """Test 9: Bluetooth device only supports 48000 Hz -> negotiates 48 kHz and resamples cleanly to/from 16/24 kHz."""
        env = _FakeAudioEnvironment(
            bt_connected=True,
            supported_rates={
                "Headset (OPPO Enco Buds2)": 48000,
                "Headphones (OPPO Enco Buds2)": 48000,
            },
        )
        received_frames = []
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
                input_callback=lambda indata, frames, *_: received_frames.append(len(indata)),
                preferred_input="Headset (OPPO Enco Buds2)",
                preferred_output="Headphones (OPPO Enco Buds2)",
                start_monitor=False,
            )
            self.assertEqual(st["input_sample_rate"], 48000)
            self.assertEqual(st["output_sample_rate"], 48000)

            # Simulate 48 kHz mic callback (3072 samples) -> resampled to 16 kHz (1024 samples)!
            fake_in_pcm = np.zeros(3072, dtype=np.int16)
            mgr._in_stream.callback(fake_in_pcm, 3072, None, None)
            self.assertEqual(received_frames, [1024])

            # Write 24 kHz Gemini PCM (1000 samples = 2000 bytes) -> resampled to 48 kHz (2000 samples = 4000 bytes)!
            fake_out_pcm = np.zeros(1000, dtype=np.int16).tobytes()
            ok = mgr.write_output(fake_out_pcm)
            self.assertTrue(ok)
            self.assertEqual(len(mgr._out_stream.writes[0]), 4000)
            mgr.shutdown()

    def test_10_repeated_and_concurrent_switching_no_duplicates(self):
        """Test 10: Repeated & concurrent audio switching creates zero duplicate streams or deadlocks."""
        env = _FakeAudioEnvironment(bt_connected=True)
        errors = []
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
            mgr.initialize(start_monitor=False)

            def _toggle_worker(n: int):
                try:
                    for i in range(n):
                        target = "OPPO Enco Buds2" if i % 2 == 0 else "laptop speakers"
                        mgr.switch_output(target, persist=False)
                        mic_target = "laptop microphone" if i % 2 == 0 else "OPPO Enco Buds2"
                        mgr.switch_input(mic_target, persist=False)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=_toggle_worker, args=(4,)) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

            self.assertEqual(errors, [])
            # Exactly 2 streams open (1 input + 1 output) — NEVER duplicated!
            self.assertEqual(len(env.open_streams()), 2)
            self.assertEqual(audio_devices._active_streams_count, 2)
            mgr.shutdown()

    def test_11_gemini_live_session_preserved_during_hot_switch(self):
        """Test 11: Audio device hot-switching preserves the Gemini Live session (no reconnect called!)."""
        from main import JarvisLive

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
            live = JarvisLive.__new__(JarvisLive)
            live.ui = MagicMock()
            live.request_reconnect = MagicMock()
            live.audio_manager = AudioManager(bt_settle_s=0.0)
            live.audio_manager.initialize(
                preferred_input="Microphone Array (Realtek(R) Audio)",
                preferred_output="Speakers (Realtek(R) Audio)",
                start_monitor=False,
            )

            with (
                patch("main.get_input_device", return_value="Headset (OPPO Enco Buds2)"),
                patch("main.get_output_device", return_value="Headphones (OPPO Enco Buds2)"),
                patch("memory.config_manager.get_input_device", return_value="Headset (OPPO Enco Buds2)"),
                patch("memory.config_manager.get_output_device", return_value="Headphones (OPPO Enco Buds2)"),
            ):
                live._on_audio_device_change()
                time.sleep(0.15)

            # Gemini Live session was NEVER asked to reconnect!
            live.request_reconnect.assert_not_called()
            self.assertEqual(live.audio_manager.active_output_name, "Headphones (OPPO Enco Buds2)")
            live.audio_manager.shutdown()

    def test_12_graceful_shutdown_closes_all_streams_and_threads(self):
        """Test 12: Graceful shutdown stops monitor thread and closes all streams cleanly."""
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
            mgr = AudioManager(poll_interval_s=0.05, bt_settle_s=0.0)
            mgr.initialize(start_monitor=True)
            self.assertIsNotNone(mgr._monitor_thread)
            self.assertTrue(mgr._monitor_thread.is_alive())
            self.assertEqual(len(env.open_streams()), 2)

            mgr.shutdown()
            self.assertIsNone(mgr._monitor_thread)
            self.assertEqual(len(env.open_streams()), 0)
            self.assertEqual(audio_devices._active_streams_count, 0)
            st = mgr.get_status()
            self.assertEqual(st["input_status"], STATUS_DISCONNECTED)
            self.assertEqual(st["output_status"], STATUS_DISCONNECTED)


class TestConfirmationProtocolAndNvidiaFix(unittest.TestCase):
    """Tests voice-only confirmation for low/medium tasks vs voice+visual for high-impact,
    sleep/shutdown permissions, and warning-free nvidia-ml-py import."""

    def tearDown(self):
        confirm.discard_pending("test cleanup")

    def test_confirmation_protocol_voice_only_vs_voice_and_visual(self):
        show_mock = MagicMock()
        hide_mock = MagicMock()
        confirm.bind(show=show_mock, hide=hide_mock)

        # 1. Medium-level task ('confirmation') -> voice-only, NO visual popup!
        prompt_med = confirm.request_action(
            action="windows_control",
            parameters={"action": "set_audio_output", "target": "OPPO Enco Buds2"},
            risk_level="confirmation",
            title="Change Audio Output",
            detail="Switch speaker to OPPO Enco Buds2",
            run=lambda: "Switched",
        )
        self.assertIn("[CONFIRMATION_PENDING]", prompt_med)
        show_mock.assert_not_called()
        self.assertFalse(confirm.get_pending().visual_prompt)

        # 2. High-level task ('high_impact') -> BOTH voice AND visual click popup!
        prompt_hi = confirm.request_action(
            action="shutdown_jarvis",
            parameters={},
            risk_level="high_impact",
            title="Shut Down JARVIS",
            detail="Completely close JARVIS.",
            run=lambda: "Shutdown",
        )
        self.assertIn("[CONFIRMATION_PENDING]", prompt_hi)
        show_mock.assert_called_once_with("Shut Down JARVIS", "Completely close JARVIS.")
        self.assertTrue(confirm.get_pending().visual_prompt)

        # 3. Replacing a high-impact visual prompt with a medium voice-only prompt hides the stale banner!
        confirm.request_action(
            action="file_controller",
            parameters={"action": "rename"},
            risk_level="medium",
            title="Rename File",
            detail="Rename a.txt to b.txt",
            run=lambda: "Renamed",
        )
        hide_mock.assert_called()
        self.assertEqual(show_mock.call_count, 1)

    def test_sleep_and_shutdown_permissions(self):
        self.assertEqual(ActionPermissionManager.get_risk_level("sleep_jarvis", {}), ActionRiskLevel.IMMEDIATE)
        self.assertEqual(ActionPermissionManager.get_risk_level("shutdown_jarvis", {}), ActionRiskLevel.HIGH_IMPACT)
        self.assertEqual(
            ActionPermissionManager.get_risk_level("windows_control", {"action": "list_audio_input_devices"}),
            ActionRiskLevel.IMMEDIATE,
        )
        self.assertEqual(
            ActionPermissionManager.get_risk_level("windows_control", {"action": "list_audio_output_devices"}),
            ActionRiskLevel.IMMEDIATE,
        )
        self.assertEqual(
            ActionPermissionManager.get_risk_level(
                "windows_control", {"action": "set_audio_input", "target": "OPPO Enco Buds2"}
            ),
            ActionRiskLevel.CONFIRMATION_REQUIRED,
        )

    def test_nvidia_ml_py_has_no_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", FutureWarning)
            try:
                import importlib
                import pynvml

                importlib.reload(pynvml)
            except ImportError:
                self.skipTest("nvidia-ml-py not installed")
        dep_warnings = [w for w in caught if "deprecated" in str(w.message).lower()]
        self.assertEqual(dep_warnings, [], f"Unexpected deprecation warnings: {dep_warnings}")


if __name__ == "__main__":
    unittest.main()
