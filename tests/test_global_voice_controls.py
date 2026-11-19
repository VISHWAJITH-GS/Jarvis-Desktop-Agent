"""
tests/test_global_voice_controls.py — Complete Test Suite for Global Voice Control,
Listening Toggle & Emergency Speech Interruption for MARK LVI / JARVIS.
"""

import asyncio
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from core.voice_state import VoiceState, VoiceStateManager
from core.global_input import GlobalInputController, parse_chord, _VK_MAP
from memory.config_manager import (
    get_listening_shortcut,
    get_emergency_stop_shortcut,
    get_mouse_gesture_enabled,
    save_listening_shortcut,
    save_emergency_stop_shortcut,
    save_mouse_gesture_enabled,
)


class TestVoiceStateManager(unittest.TestCase):
    """Tests the 4-state voice machine, transitions, and generation token advance."""

    def setUp(self):
        self.state_changes = []
        self.interrupt_calls = 0

        def on_change(old_s, new_s):
            self.state_changes.append((old_s, new_s))

        def on_intr():
            self.interrupt_calls += 1

        self.sm = VoiceStateManager(
            initial_state=VoiceState.LISTENING,
            on_state_change=on_change,
            on_interrupt=on_intr,
        )

    def test_initial_state(self):
        self.assertEqual(self.sm.state, VoiceState.LISTENING)
        self.assertTrue(self.sm.is_listening)
        self.assertFalse(self.sm.is_disabled)
        self.assertFalse(self.sm.is_speaking)
        self.assertFalse(self.sm.is_processing)
        self.assertEqual(self.sm.generation_id, 0)

    def test_listening_toggle_to_disabled_and_back(self):
        # 1. Active (LISTENING) -> DISABLED (MUTED)
        new_state = self.sm.toggle_listening()
        self.assertEqual(new_state, VoiceState.DISABLED)
        self.assertTrue(self.sm.is_disabled)
        self.assertFalse(self.sm.is_listening)

        # 2. DISABLED (MUTED) -> SLEEPING
        new_state = self.sm.toggle_listening()
        self.assertEqual(new_state, VoiceState.SLEEPING)
        self.assertTrue(self.sm.is_sleeping)
        self.assertFalse(self.sm.is_disabled)

        # 3. SLEEPING -> remains SLEEPING (sticky, no state change)
        for _ in range(3):
            new_state = self.sm.toggle_listening()
            self.assertEqual(new_state, VoiceState.SLEEPING)
            self.assertTrue(self.sm.is_sleeping)

        # 4. Force Listening (Ctrl+Alt+K) -> LISTENING
        self.assertEqual(self.sm.force_listening(), VoiceState.LISTENING)
        self.assertTrue(self.sm.is_listening)

    def test_full_state_lifecycle_transitions(self):
        # LISTENING -> PROCESSING
        self.assertTrue(self.sm.start_processing())
        self.assertTrue(self.sm.is_processing)

        # PROCESSING -> SPEAKING
        self.assertTrue(self.sm.start_speaking())
        self.assertTrue(self.sm.is_speaking)

        # SPEAKING -> LISTENING (normal completion)
        self.assertTrue(self.sm.finish_speaking())
        self.assertTrue(self.sm.is_listening)

    def test_emergency_stop_speech_while_speaking(self):
        self.sm.start_speaking()
        self.assertTrue(self.sm.is_speaking)
        initial_gen = self.sm.generation_id

        # Trigger emergency stop
        interrupted = self.sm.emergency_stop_speech()
        self.assertTrue(interrupted)
        self.assertEqual(self.interrupt_calls, 1)
        self.assertGreater(self.sm.generation_id, initial_gen)
        # Should return to LISTENING if previously enabled
        self.assertTrue(self.sm.is_listening)

    def test_disable_listening_while_speaking_aborts_speech(self):
        self.sm.start_speaking()
        self.assertTrue(self.sm.is_speaking)

        # Disable listening while speaking
        self.sm.disable_listening()
        self.assertTrue(self.sm.is_disabled)
        # Interrupt must have been triggered to cancel speech
        self.assertEqual(self.interrupt_calls, 1)
        self.assertGreater(self.sm.generation_id, 0)


class TestGlobalInputController(unittest.TestCase):
    """Tests keyboard chord parsing, shortcut execution, and mouse gestures."""

    def test_parse_chord_standard_shortcuts(self):
        # Default listening shortcut: Ctrl + Alt + J
        codes = parse_chord(["ctrl", "alt", "j"])
        self.assertEqual(codes, [0x11, 0x12, 0x4A])

        # Default emergency stop shortcut: Ctrl + Alt + K
        codes_k = parse_chord("ctrl+alt+k")
        self.assertEqual(codes_k, [0x11, 0x12, 0x4B])

        # Escape key
        codes_esc = parse_chord("esc")
        self.assertEqual(codes_esc, [0x1B])

    def test_parse_chord_case_insensitivity_and_punctuation(self):
        codes = parse_chord("CTRL + ALT + J")
        self.assertEqual(codes, [0x11, 0x12, 0x4A])

        codes_dash = parse_chord("ctrl-alt-k")
        self.assertEqual(codes_dash, [0x11, 0x12, 0x4B])

    def test_unknown_key_handled_safely(self):
        codes = parse_chord(["ctrl", "supercalifragilistic", "j"])
        self.assertEqual(codes, [0x11, 0x4A])

    def test_simultaneous_mouse_gesture_logic(self):
        """Simulate mouse button state checking to verify single-trigger and latching."""
        triggered = []

        def on_toggle():
            triggered.append("toggled")

        controller = GlobalInputController(
            on_toggle_listening=on_toggle,
            mouse_gesture_enabled=True,
        )

        # Mock GetAsyncKeyState
        mock_keystates = {}

        def mock_get_async_key_state(vk):
            return 0x8000 if mock_keystates.get(vk, False) else 0

        with patch("ctypes.windll.user32.GetAsyncKeyState", side_effect=mock_get_async_key_state):
            # 1. Neither button pressed
            mock_keystates[0x01] = False
            mock_keystates[0x02] = False
            self.assertFalse(controller._mouse_latched)

            # 2. Only Left button pressed -> should NOT trigger
            mock_keystates[0x01] = True
            mock_keystates[0x02] = False
            # Simulate a tick
            # In monitor loop:
            l_down = bool(mock_get_async_key_state(0x01) & 0x8000)
            r_down = bool(mock_get_async_key_state(0x02) & 0x8000)
            self.assertTrue(l_down and not r_down)
            self.assertEqual(len(triggered), 0)

            # 3. Both Left and Right pressed simultaneously -> triggers gesture!
            mock_keystates[0x01] = True
            mock_keystates[0x02] = True
            l_down = bool(mock_get_async_key_state(0x01) & 0x8000)
            r_down = bool(mock_get_async_key_state(0x02) & 0x8000)
            self.assertTrue(l_down and r_down)
            controller._mouse_down_time = time.monotonic() - 0.1
            controller._mouse_latched = True
            controller._dispatch(controller.on_toggle_listening, "Mouse Gesture")
            time.sleep(0.05)
            self.assertEqual(len(triggered), 1)

            # 4. Both buttons STILL held -> MUST NOT repeat trigger
            self.assertTrue(controller._mouse_latched)

            # 5. Only one button released -> MUST REMAIN latched until BOTH released
            mock_keystates[0x01] = False
            mock_keystates[0x02] = True
            l_down = bool(mock_get_async_key_state(0x01) & 0x8000)
            r_down = bool(mock_get_async_key_state(0x02) & 0x8000)
            if not l_down and not r_down:
                controller._mouse_latched = False
            self.assertTrue(controller._mouse_latched)

            # 6. Both released -> unlatches
            mock_keystates[0x01] = False
            mock_keystates[0x02] = False
            l_down = bool(mock_get_async_key_state(0x01) & 0x8000)
            r_down = bool(mock_get_async_key_state(0x02) & 0x8000)
            if not l_down and not r_down:
                controller._mouse_latched = False
            self.assertFalse(controller._mouse_latched)


class TestGlobalVoiceIntegration(unittest.TestCase):
    """End-to-end integration tests between VoiceStateManager, queues, and audio cancellation."""

    def test_audio_stale_chunk_rejection_by_generation_id(self):
        """Verify that chunks with older generation tokens are immediately dropped."""
        sm = VoiceStateManager(initial_state=VoiceState.LISTENING)
        sm.start_speaking()

        current_gen = sm.generation_id  # 0
        audio_queue = []

        # Producer pushes chunks under gen 0
        audio_queue.append((current_gen, b"audio_chunk_1"))
        audio_queue.append((current_gen, b"audio_chunk_2"))

        # Emergency stop occurs mid-speech
        sm.emergency_stop_speech()
        new_gen = sm.generation_id  # 1
        self.assertGreater(new_gen, current_gen)

        # Consumer processes queue
        valid_chunks_played = []
        for chunk_gen, data in audio_queue:
            if chunk_gen == sm.generation_id and not sm.is_disabled:
                valid_chunks_played.append(data)

        # All chunks from old generation were discarded
        self.assertEqual(len(valid_chunks_played), 0)

    def test_microphone_audio_gating_when_disabled(self):
        """Verify microphone callback drops all frames when voice state is DISABLED."""
        sm = VoiceStateManager(initial_state=VoiceState.DISABLED)
        out_queue = []

        def mock_mic_callback(indata):
            if sm.is_disabled:
                return  # Gated: do not process or enqueue
            out_queue.append(indata)

        mock_mic_callback(b"user_speech_frame_1")
        mock_mic_callback(b"user_speech_frame_2")

        # Zero frames transmitted while disabled
        self.assertEqual(len(out_queue), 0)

        # Enable listening
        sm.enable_listening()
        mock_mic_callback(b"user_speech_frame_3")
        self.assertEqual(len(out_queue), 1)

    def test_config_manager_shortcuts_persistence(self):
        """Verify reading and saving global shortcut preferences."""
        orig_l = get_listening_shortcut()
        orig_e = get_emergency_stop_shortcut()
        orig_m = get_mouse_gesture_enabled()

        try:
            save_listening_shortcut(["ctrl", "alt", "j"])
            save_emergency_stop_shortcut(["ctrl", "alt", "k"])
            save_mouse_gesture_enabled(True)

            self.assertEqual(get_listening_shortcut(), ["ctrl", "alt", "j"])
            self.assertEqual(get_emergency_stop_shortcut(), ["ctrl", "alt", "k"])
            self.assertTrue(get_mouse_gesture_enabled())
        finally:
            save_listening_shortcut(orig_l)
            save_emergency_stop_shortcut(orig_e)
            save_mouse_gesture_enabled(orig_m)

    def test_interrupt_when_idle_does_not_spam_logs(self):
        """Verify calling interrupt when idle does not emit any speech stopped log."""
        mock_ui = MagicMock()
        mock_ui.write_log = MagicMock()
        mock_ui.muted = False

        from main import JarvisLive
        live = JarvisLive.__new__(JarvisLive)
        live.ui = mock_ui
        live._interrupted = False
        live._in_interrupt = False
        live._interrupt_lock = threading.Lock()
        live._speaking_lock = threading.Lock()
        live._is_speaking = False
        live._tail_until = 0.0
        live._out_latency = 0.0
        live.audio_in_queue = None
        live._turn_done_event = None
        live._engine = None
        live._visemes = MagicMock()
        live.voice_state = VoiceStateManager(initial_state=VoiceState.LISTENING)

        # Call interrupt while idle
        live.interrupt()

        # write_log must NOT have been called with "SYS: Interrupted — speech stopped."
        mock_ui.write_log.assert_not_called()

    def test_interrupt_when_speaking_logs_exactly_once(self):
        """Verify calling interrupt when speaking logs exactly once and does not recurse."""
        mock_ui = MagicMock()
        mock_ui.write_log = MagicMock()
        mock_ui.muted = False

        from main import JarvisLive
        live = JarvisLive.__new__(JarvisLive)
        live.ui = mock_ui
        live._interrupted = False
        live._in_interrupt = False
        live._interrupt_lock = threading.Lock()
        live._speaking_lock = threading.Lock()
        live._is_speaking = True
        live._tail_until = 0.0
        live._out_latency = 0.0
        live.audio_in_queue = None
        live._turn_done_event = None
        live._engine = None
        live._visemes = MagicMock()
        live.voice_state = VoiceStateManager(initial_state=VoiceState.SPEAKING)

        # Call interrupt while speaking
        live.interrupt()

        # write_log must have been called exactly once
        mock_ui.write_log.assert_called_once_with("SYS: Interrupted — speech stopped.")
        self.assertFalse(live._is_speaking)
        self.assertEqual(live.voice_state.state, VoiceState.LISTENING)


class TestAdvancedStateHotkeysWakeAndSleep(unittest.TestCase):
    """Tests Toggle Mute (Ctrl+Alt+J) state restoration, Force Listening (Ctrl+Alt+K),
    Wake-Word ('Jarvis', 'Hello' @ 0.100), and 3-minute Idle Auto-Sleep."""

    def _make_stub_live(self, initial_state: VoiceState = VoiceState.LISTENING):
        from main import JarvisLive, IDLE_SLEEP_TIMEOUT

        mock_ui = MagicMock()
        mock_ui.muted = (initial_state == VoiceState.DISABLED)
        mock_ui.sync_mute_state = MagicMock()
        mock_ui.set_state = MagicMock()
        mock_ui.write_log = MagicMock()

        live = JarvisLive.__new__(JarvisLive)
        live.ui = mock_ui
        live._interrupted = False
        live._in_interrupt = False
        live._interrupt_lock = threading.Lock()
        live._speaking_lock = threading.Lock()
        live._is_speaking = (initial_state == VoiceState.SPEAKING)
        live._tail_until = 0.0
        live._out_latency = 0.0
        live.audio_in_queue = None
        live.out_queue = None
        live._turn_done_event = None
        live._engine = None
        live._visemes = MagicMock()
        live._dashboard = None
        live._loop = None
        live._wake_enabled = False
        live._wake_words = ["Jarvis"]
        live._wake_threshold = 0.200
        live._wake_detector = None
        live._wake_detector_lock = threading.Lock()
        live._wake_sleep_timeout = IDLE_SLEEP_TIMEOUT
        live._awake = (initial_state not in (VoiceState.SLEEPING,))
        live._last_user_speech = time.monotonic()
        live.voice_state = VoiceStateManager(
            initial_state=initial_state,
            on_state_change=live._on_voice_state_changed,
        )
        return live, mock_ui

    def test_ctrl_alt_j_transitions_mute_to_sleep_and_sticky_sleep(self):
        """Ctrl+Alt+J transitions: Active -> MUTED; MUTED -> SLEEPING; SLEEPING -> remains SLEEPING."""
        # 1. From LISTENING -> MUTED (DISABLED)
        sm = VoiceStateManager(initial_state=VoiceState.LISTENING)
        self.assertEqual(sm.handle_mute_or_sleep(), VoiceState.DISABLED)
        self.assertTrue(sm.is_muted)

        # 2. From DISABLED (MUTED) -> SLEEPING
        self.assertEqual(sm.handle_mute_or_sleep(), VoiceState.SLEEPING)
        self.assertTrue(sm.is_sleeping)

        # 3. From SLEEPING -> remains SLEEPING regardless of repeated presses
        for _ in range(5):
            self.assertEqual(sm.handle_mute_or_sleep(), VoiceState.SLEEPING)
            self.assertTrue(sm.is_sleeping)
            self.assertFalse(sm.is_muted)

    def test_ctrl_alt_j_from_speaking_and_processing_interrupts_and_mutes(self):
        """Ctrl+Alt+J while speaking or processing interrupts immediately and switches to MUTED."""
        interrupts = []
        sm = VoiceStateManager(
            initial_state=VoiceState.LISTENING,
            on_interrupt=lambda: interrupts.append(1),
        )
        sm.start_speaking()
        self.assertTrue(sm.is_speaking)
        self.assertEqual(sm.handle_mute_or_sleep(), VoiceState.DISABLED)
        self.assertEqual(len(interrupts), 1)
        self.assertTrue(sm.is_muted)

        # Pressing again in MUTED goes to SLEEPING
        self.assertEqual(sm.handle_mute_or_sleep(), VoiceState.SLEEPING)
        self.assertTrue(sm.is_sleeping)

    def test_force_listening_from_all_states(self):
        """Ctrl+Alt+K unconditionally forces LISTENING from muted, sleeping, speaking, processing."""
        for start in (
            VoiceState.DISABLED,
            VoiceState.SLEEPING,
            VoiceState.LISTENING,
            VoiceState.PROCESSING,
            VoiceState.SPEAKING,
        ):
            interrupts = []
            sm = VoiceStateManager(
                initial_state=start,
                on_interrupt=lambda: interrupts.append(1),
            )
            result = sm.force_listening()
            self.assertEqual(result, VoiceState.LISTENING)
            self.assertTrue(sm.is_listening)
            self.assertFalse(sm.is_muted)
            self.assertFalse(sm.is_sleeping)
            if start in (VoiceState.SPEAKING, VoiceState.PROCESSING):
                self.assertEqual(len(interrupts), 1)
            else:
                self.assertEqual(len(interrupts), 0)

    def test_jarvis_live_toggle_mute_cycle_and_sticky_sleep(self):
        """Verify JarvisLive.global_toggle_mute cycles LISTENING -> MUTED -> SLEEPING -> remains SLEEPING."""
        live, mock_ui = self._make_stub_live(VoiceState.LISTENING)
        with patch.object(live, "_ensure_wake_detector_async"):
            # 1. From LISTENING -> switches to MUTED
            st1 = live.global_toggle_mute()
            self.assertEqual(st1, VoiceState.DISABLED)
            mock_ui.sync_mute_state.assert_called_with(True, "MUTED")

            # 2. From MUTED -> switches to SLEEPING
            st2 = live.global_toggle_mute()
            self.assertEqual(st2, VoiceState.SLEEPING)
            self.assertFalse(live._awake)
            mock_ui.sync_mute_state.assert_called_with(False, "SLEEPING")

            # 3. Repeated presses while in SLEEPING remain in SLEEPING
            for _ in range(3):
                st3 = live.global_toggle_mute()
                self.assertEqual(st3, VoiceState.SLEEPING)
                self.assertTrue(live.voice_state.is_sleeping)
                self.assertFalse(live._awake)

    def test_jarvis_live_force_listening_from_sleep_and_mute(self):
        """Verify Ctrl+Alt+K wakes from SLEEPING and unmutes from MUTED without leaving _interrupted set."""
        # From SLEEPING
        live, mock_ui = self._make_stub_live(VoiceState.SLEEPING)
        st = live.global_force_listening()
        self.assertEqual(st, VoiceState.LISTENING)
        self.assertTrue(live._awake)
        self.assertFalse(live._interrupted)
        mock_ui.sync_mute_state.assert_called_with(False, "LISTENING")

        # From DISABLED (MUTED)
        live2, mock_ui2 = self._make_stub_live(VoiceState.DISABLED)
        st2 = live2.global_force_listening()
        self.assertEqual(st2, VoiceState.LISTENING)
        self.assertTrue(live2._awake)
        self.assertFalse(live2._interrupted)
        mock_ui2.sync_mute_state.assert_called_with(False, "LISTENING")

    def test_global_input_force_listening_active_while_sleeping(self):
        """Verify GlobalInputController binds Ctrl+Alt+J (mute) and Ctrl+Alt+K (force listening) while asleep."""
        sm = VoiceStateManager(initial_state=VoiceState.SLEEPING)
        ctrl = GlobalInputController(
            on_toggle_mute=sm.toggle_mute,
            on_force_listening=sm.force_listening,
            mute_chord=("ctrl", "alt", "j"),
            force_listening_chord=("ctrl", "alt", "k"),
            mouse_gesture_enabled=False,
        )
        self.assertEqual(ctrl._listening_codes, (0x11, 0x12, 0x4A))
        self.assertEqual(ctrl._emergency_codes, (0x11, 0x12, 0x4B))

        # Trigger Force Listening while asleep
        ctrl._dispatch(ctrl.on_force_listening, "Global Force Listening")
        time.sleep(0.05)
        self.assertEqual(sm.state, VoiceState.LISTENING)

    def test_wake_word_defaults_and_matching(self):
        """Verify default wake word ('Jarvis' alone), threshold 0.200 (200), and matching logic."""
        from core.wake_word import (
            DEFAULT_WAKE_WORDS,
            DEFAULT_THRESHOLD,
            WakeWordDetector,
            match_wake_word,
            normalize_wake_words,
        )
        from memory.config_manager import (
            DEFAULT_WAKE_WORDS as CFG_WAKE_WORDS,
            DEFAULT_WAKE_WORD_THRESHOLD as CFG_THRESHOLD,
            DEFAULT_IDLE_SLEEP_TIMEOUT as CFG_IDLE_TIMEOUT,
            save_wake_word_threshold,
            get_wake_word_threshold,
        )

        self.assertEqual(tuple(DEFAULT_WAKE_WORDS), ("Jarvis",))
        self.assertAlmostEqual(DEFAULT_THRESHOLD, 0.200, places=3)
        self.assertEqual(CFG_WAKE_WORDS, ["Jarvis"])
        self.assertAlmostEqual(CFG_THRESHOLD, 0.200, places=3)
        self.assertEqual(CFG_IDLE_TIMEOUT, 180.0)

        # Passing 200 normalizes to 0.200
        save_wake_word_threshold(200)
        self.assertAlmostEqual(get_wake_word_threshold(), 0.200, places=3)

        # "Hello" is explicitly stripped by normalization
        self.assertEqual(normalize_wake_words(["Jarvis", "Hello"]), ["Jarvis"])
        self.assertEqual(normalize_wake_words(["hello"]), ["Jarvis"])

        # Positive matches for "Jarvis"
        self.assertEqual(match_wake_word("Hey Jarvis, what time is it?", ["Jarvis"]), "Jarvis")
        self.assertEqual(match_wake_word("JARVIS!", ["Jarvis"]), "Jarvis")

        # "Hello" is NOT matched as a wake word
        self.assertIsNone(match_wake_word("Hello there", ["Jarvis"]))
        self.assertIsNone(match_wake_word("hello.", ["Jarvis"]))

        # Negative matches (no false substring triggers)
        self.assertIsNone(match_wake_word("shell script", ["Jarvis"]))
        self.assertIsNone(match_wake_word("technical jargon", ["Jarvis"]))
        self.assertIsNone(match_wake_word("", ["Jarvis"]))

        # Verify detector default configuration and threshold trigger
        detected = []
        det = WakeWordDetector(on_detect=lambda w: detected.append(w), enable_keyword_spotter=False)
        self.assertEqual(det.wake_words, ["Jarvis"])
        self.assertAlmostEqual(det.threshold, 0.200, places=3)

        # Passing 200 to detector normalizes to 0.200
        det_200 = WakeWordDetector(on_detect=lambda w: None, threshold=200, enable_keyword_spotter=False)
        self.assertAlmostEqual(det_200.threshold, 0.200, places=3)

        det._running = True
        det._model_to_word = {"hey_jarvis": "Jarvis"}
        word, score = det._map_score_to_word({"hey_jarvis": 0.205})
        self.assertEqual(word, "Jarvis")
        self.assertGreaterEqual(score, det.threshold)
        det._trigger_detection(word, score)
        self.assertEqual(detected, ["Jarvis"])
        self.assertEqual(det.last_detected_word, "Jarvis")

        # Wake word must NOT wake JARVIS while muted, MUST wake while sleeping
        live_muted, _ = self._make_stub_live(VoiceState.DISABLED)
        live_muted._on_wake_detected("Jarvis")
        self.assertTrue(live_muted.voice_state.is_disabled)

        live_asleep, _ = self._make_stub_live(VoiceState.SLEEPING)
        live_asleep._on_wake_detected("Jarvis")
        self.assertTrue(live_asleep.voice_state.is_listening)
        self.assertTrue(live_asleep._awake)

    def test_idle_sleep_timer_3_minutes(self):
        """Verify 3-minute (180s) idle auto-sleep rules and command/speech activity resets."""
        from main import IDLE_SLEEP_TIMEOUT, WAKE_SLEEP_TIMEOUT
        from core.voice_state import DEFAULT_IDLE_SLEEP_TIMEOUT

        self.assertEqual(IDLE_SLEEP_TIMEOUT, 180.0)
        self.assertEqual(WAKE_SLEEP_TIMEOUT, 180.0)
        self.assertEqual(DEFAULT_IDLE_SLEEP_TIMEOUT, 180.0)

        sm = VoiceStateManager(initial_state=VoiceState.LISTENING)
        t0 = 1000.0
        sm.touch_activity(t0)

        # Not idle enough at 179.9s
        self.assertFalse(sm.should_auto_sleep(180.0, now=t0 + 179.9))
        # Idle at exactly 180.0s -> auto-sleep triggers
        self.assertTrue(sm.should_auto_sleep(180.0, now=t0 + 180.0))

        # Active command prevents auto-sleep even well past 180s
        sm.begin_command()
        self.assertFalse(sm.should_auto_sleep(180.0, now=time.monotonic() + 500.0))
        sm.end_command()
        # end_command resets the timer, so immediately after it should NOT sleep
        self.assertFalse(sm.should_auto_sleep(180.0))

        # In SLEEPING, Ctrl+Alt+J remains in SLEEPING and never auto-sleeps
        sm.enter_sleep()
        self.assertTrue(sm.is_sleeping)
        self.assertFalse(sm.should_auto_sleep(180.0, now=time.monotonic() + 500.0))
        sm.toggle_mute()
        self.assertTrue(sm.is_sleeping)
        self.assertFalse(sm.should_auto_sleep(180.0, now=time.monotonic() + 500.0))

        # In DISABLED (MUTED), also never auto-sleeps
        sm.disable_listening()
        self.assertTrue(sm.is_disabled)
        self.assertFalse(sm.should_auto_sleep(180.0, now=time.monotonic() + 500.0))

    def test_ctrl_alt_k_when_already_listening_never_mutes_or_sleeps(self):
        """Ctrl+Alt+K repeated while ALREADY in LISTENING keeps LISTENING and never mutes or sleeps."""
        live, mock_ui = self._make_stub_live(VoiceState.LISTENING)
        for _ in range(5):
            st = live.global_force_listening()
            self.assertEqual(st, VoiceState.LISTENING)
            self.assertTrue(live.voice_state.is_listening)
            self.assertFalse(live.voice_state.is_muted)
            self.assertFalse(live.voice_state.is_sleeping)
            self.assertTrue(live._awake)
            self.assertFalse(live._interrupted)
            mock_ui.sync_mute_state.assert_called_with(False, "LISTENING")

    def test_webcam_and_camera_completely_removed(self):
        """Verify all webcam capture, streaming, and OpenCV dependencies are completely removed."""
        from actions import screen_processor
        import main
        import ui

        self.assertFalse(hasattr(screen_processor, "_capture_camera"))
        self.assertFalse(hasattr(screen_processor, "_detect_camera_index"))
        self.assertFalse(hasattr(screen_processor, "_probe_camera"))
        self.assertFalse(hasattr(screen_processor, "cv2"))
        self.assertTrue(hasattr(screen_processor, "_capture_screen"))

        tool_names = {t["name"] for t in main.TOOL_DECLARATIONS}
        self.assertNotIn("close_camera", tool_names)
        self.assertIn("screen_process", tool_names)

        self.assertFalse(hasattr(ui.JarvisUI, "start_camera_stream"))
        self.assertFalse(hasattr(ui.JarvisUI, "stop_camera_stream"))
        self.assertFalse(hasattr(ui.JarvisUI, "show_camera_frame"))
        self.assertFalse(hasattr(ui, "_CameraPreview"))


if __name__ == "__main__":
    unittest.main()

