"""
core/voice_state.py — Centralized Voice State Management for MARK LVI / JARVIS

Manages the authoritative voice states:
- DISABLED (MUTED): Microphone capture stopped, wake-word stopped, no audio
                    processing, voice output stopped, queues cleared.
                    Pressing Ctrl + Alt + J while muted transitions to SLEEPING.
- SLEEPING:         Low-power standby after idle timeout (default 3 minutes) or
                    manual sleep / Ctrl + Alt + J from MUTED. Microphone audio is
                    NOT streamed to the cloud; only local wake-word detection
                    ("Jarvis") and the global Force Listening shortcut
                    (Ctrl + Alt + K) wake it. Pressing Ctrl + Alt + J while
                    sleeping does NOT change state.
- LISTENING:        Microphone capture active, waiting for speech/commands.
- PROCESSING:       Command received, thinking/synthesizing response.
- SPEAKING:         Audio response playing, cancellable immediately.

Enforces thread-safe transitions and dispatches event notifications to audio I/O,
UI elements, and the Gemini Live conversation loop.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Default inactivity window (seconds) before transitioning to SLEEPING.
DEFAULT_IDLE_SLEEP_TIMEOUT = 180.0   # 3 minutes


class VoiceState(str, enum.Enum):
    DISABLED = "DISABLED"
    MUTED = "DISABLED"       # Canonical alias for DISABLED
    SLEEPING = "SLEEPING"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"


class VoiceStateManager:
    """
    Central thread-safe voice state machine for JARVIS.
    Coordinates microphone gating, sleep/wake lifecycle, mute memory,
    speech interruption, idle tracking, and UI feedback.
    """

    def __init__(
        self,
        initial_state: VoiceState = VoiceState.LISTENING,
        on_state_change: Optional[Callable[[VoiceState, VoiceState], None]] = None,
        on_interrupt: Optional[Callable[[], None]] = None,
    ):
        self._state = initial_state
        # Resting state to restore when unmuting via Toggle Mute (Ctrl + Alt + J).
        self._pre_mute_state: VoiceState = (
            VoiceState.SLEEPING
            if initial_state == VoiceState.SLEEPING
            else VoiceState.LISTENING
        )
        self._lock = threading.RLock()
        self._generation_id = 0
        self._on_state_change = on_state_change
        self._on_interrupt = on_interrupt
        self._in_interrupt = False
        now = time.monotonic()
        self._last_state_change = now
        self._last_activity = now
        self._active_commands = 0

    # ── State Inspection ─────────────────────────────────────────────────────

    @property
    def state(self) -> VoiceState:
        with self._lock:
            return self._state

    @property
    def is_disabled(self) -> bool:
        with self._lock:
            return self._state == VoiceState.DISABLED

    @property
    def is_muted(self) -> bool:
        """Alias for is_disabled."""
        with self._lock:
            return self._state == VoiceState.DISABLED

    @property
    def is_sleeping(self) -> bool:
        with self._lock:
            return self._state == VoiceState.SLEEPING

    @property
    def is_listening(self) -> bool:
        with self._lock:
            return self._state == VoiceState.LISTENING

    @property
    def is_processing(self) -> bool:
        with self._lock:
            return self._state == VoiceState.PROCESSING

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            return self._state == VoiceState.SPEAKING

    @property
    def is_awake(self) -> bool:
        """True when active (LISTENING, PROCESSING, or SPEAKING)."""
        with self._lock:
            return self._state not in (VoiceState.DISABLED, VoiceState.SLEEPING)

    @property
    def previous_state(self) -> VoiceState:
        """The resting state that will be restored when toggling mute off."""
        with self._lock:
            return self._pre_mute_state

    @property
    def generation_id(self) -> int:
        """Generation counter incremented on speech interruption to discard stale chunks."""
        with self._lock:
            return self._generation_id

    @property
    def last_activity(self) -> float:
        with self._lock:
            return self._last_activity

    @property
    def idle_seconds(self) -> float:
        with self._lock:
            return max(0.0, time.monotonic() - self._last_activity)

    @property
    def active_commands(self) -> int:
        with self._lock:
            return self._active_commands

    # ── Activity & Idle Sleep Tracking ───────────────────────────────────────

    def touch_activity(self, now: Optional[float] = None) -> None:
        """Record user speech, command, or interaction to reset the idle timer."""
        with self._lock:
            self._last_activity = time.monotonic() if now is None else float(now)

    def begin_command(self) -> None:
        """Mark an active command/tool as running (prevents auto-sleep)."""
        with self._lock:
            self._active_commands += 1
            self._last_activity = time.monotonic()

    def end_command(self) -> None:
        """Mark a command/tool as finished and reset the idle clock."""
        with self._lock:
            self._active_commands = max(0, self._active_commands - 1)
            self._last_activity = time.monotonic()

    def should_auto_sleep(
        self,
        timeout_seconds: float = DEFAULT_IDLE_SLEEP_TIMEOUT,
        now: Optional[float] = None,
    ) -> bool:
        """
        True when JARVIS is idle in LISTENING with no active commands for at
        least `timeout_seconds` (default 180s / 3 minutes).
        Returns False if timeout_seconds <= 0 or None (auto-sleep disabled).
        """
        with self._lock:
            if timeout_seconds is None or timeout_seconds <= 0:
                return False
            if self._state != VoiceState.LISTENING:
                return False
            if self._active_commands > 0:
                return False
            current = time.monotonic() if now is None else float(now)
            return (current - self._last_activity) >= max(1.0, float(timeout_seconds))

    # ── Transition Methods ───────────────────────────────────────────────────

    def handle_mute_or_sleep(self) -> VoiceState:
        """
        State transition for Ctrl + Alt + J:
        - If currently in SLEEPING: remain in SLEEPING (sticky; no state change).
        - If currently in DISABLED (MUTED): transition to SLEEPING.
        - If currently in any active state (LISTENING, PROCESSING, SPEAKING):
          interrupt speech/processing if active and transition to DISABLED (MUTED).
        Returns the resulting VoiceState.
        """
        with self._lock:
            if self._state == VoiceState.SLEEPING:
                # In sleep state: never change state on Ctrl+Alt+J
                return self._state

            if self._state == VoiceState.DISABLED:
                # In muted state: pressing Ctrl+Alt+J transitions to SLEEPING
                self._transition_to(VoiceState.SLEEPING)
                return self._state

            # Any active state (LISTENING, PROCESSING, SPEAKING) transitions to DISABLED (MUTED)
            if self._state in (VoiceState.SPEAKING, VoiceState.PROCESSING):
                self._trigger_interrupt()
            self._transition_to(VoiceState.DISABLED)
            return self._state

    def toggle_mute(self) -> VoiceState:
        """Alias for handle_mute_or_sleep (Ctrl + Alt + J)."""
        return self.handle_mute_or_sleep()

    def force_listening(self) -> VoiceState:
        """
        Force Listening (Ctrl + Alt + K):
        Unconditionally transitions JARVIS to LISTENING regardless of whether it
        is currently DISABLED (muted), SLEEPING, PROCESSING, SPEAKING, or already
        LISTENING.
        - Interrupts any in-flight speech/processing if active.
        - If already in LISTENING, remains in LISTENING and touches activity.
        - Never mutes or sleeps.
        Returns the resulting VoiceState.
        """
        with self._lock:
            if self._state in (VoiceState.SPEAKING, VoiceState.PROCESSING):
                self._trigger_interrupt()
            self._pre_mute_state = VoiceState.LISTENING
            self._last_activity = time.monotonic()
            self._transition_to(VoiceState.LISTENING)
            return self._state

    def enter_sleep(self) -> bool:
        """
        Transition into low-power SLEEPING mode (auto-sleep or manual sleep).
        Does NOT override DISABLED (muted) state.
        Returns True if state changed to SLEEPING.
        """
        with self._lock:
            if self._state in (VoiceState.SLEEPING, VoiceState.DISABLED):
                return False
            if self._state in (VoiceState.SPEAKING, VoiceState.PROCESSING):
                self._trigger_interrupt()
            self._transition_to(VoiceState.SLEEPING)
            return True

    def wake_up(self) -> bool:
        """
        Transition from SLEEPING to LISTENING (e.g. via wake word "Jarvis" / "Hello").
        Will NOT override DISABLED (muted) state — use force_listening() or
        toggle_mute() to leave muted mode.
        Returns True if state changed.
        """
        with self._lock:
            if self._state != VoiceState.SLEEPING:
                return False
            self._last_activity = time.monotonic()
            self._transition_to(VoiceState.LISTENING)
            return True

    def enable_listening(self) -> bool:
        """
        Transition from DISABLED or SLEEPING to LISTENING.
        Returns True if state changed, False if already enabled/active.
        """
        with self._lock:
            if self._state in (VoiceState.DISABLED, VoiceState.SLEEPING):
                self._pre_mute_state = VoiceState.LISTENING
                self._last_activity = time.monotonic()
                self._transition_to(VoiceState.LISTENING)
                return True
            return False

    def disable_listening(self) -> bool:
        """
        Transition from any active or sleeping state to DISABLED (MUTED).
        Immediately halts microphone ingestion, cancels any active speech output,
        and clears audio pipelines while remembering the prior resting state.
        """
        with self._lock:
            if self._state == VoiceState.DISABLED:
                return False

            if self._state == VoiceState.SLEEPING:
                self._pre_mute_state = VoiceState.SLEEPING
            else:
                self._pre_mute_state = VoiceState.LISTENING
                if self._state in (VoiceState.SPEAKING, VoiceState.PROCESSING):
                    self._trigger_interrupt()

            self._transition_to(VoiceState.DISABLED)
            return True

    def toggle_listening(self) -> VoiceState:
        """
        Toggle mute state (restores previous state when unmuting).
        Kept as an alias for toggle_mute() for backward compatibility.
        """
        return self.toggle_mute()

    def start_processing(self) -> bool:
        """
        Transition from LISTENING to PROCESSING when user speech is detected/submitted.
        """
        with self._lock:
            if self._state == VoiceState.LISTENING:
                self._last_activity = time.monotonic()
                self._transition_to(VoiceState.PROCESSING)
                return True
            return False

    def start_speaking(self) -> bool:
        """
        Transition from PROCESSING (or LISTENING) to SPEAKING when audio chunks begin playback.
        """
        with self._lock:
            if self._state in (VoiceState.PROCESSING, VoiceState.LISTENING):
                self._last_activity = time.monotonic()
                self._transition_to(VoiceState.SPEAKING)
                return True
            return False

    def finish_speaking(self) -> bool:
        """
        Transition from SPEAKING back to LISTENING when audio playback finishes normally.
        """
        with self._lock:
            if self._state == VoiceState.SPEAKING:
                self._last_activity = time.monotonic()
                self._transition_to(VoiceState.LISTENING)
                return True
            return False

    def emergency_stop_speech(self) -> bool:
        """
        Immediately interrupt speech playback and return to LISTENING if active.
        If DISABLED or SLEEPING, remains in that state while advancing generation_id.
        Increments generation_id to invalidate queued/buffered chunks.
        """
        with self._lock:
            interrupted = False
            if self._state in (VoiceState.SPEAKING, VoiceState.PROCESSING):
                self._trigger_interrupt()
                interrupted = True
                self._last_activity = time.monotonic()
                self._transition_to(VoiceState.LISTENING)
            elif self._state in (VoiceState.DISABLED, VoiceState.SLEEPING):
                # Still ensure generation token advances and queues flush
                self._trigger_interrupt()
            return interrupted

    # ── Internal Helpers ─────────────────────────────────────────────────────

    def _trigger_interrupt(self) -> None:
        self._generation_id += 1
        if self._on_interrupt:
            try:
                self._on_interrupt()
            except Exception as e:
                logger.warning("Error in on_interrupt callback: %s", e)
        try:
            from core.ducking import restore_background_audio
            restore_background_audio()
        except Exception:
            pass

    def _transition_to(self, new_state: VoiceState) -> None:
        old_state = self._state
        if old_state == new_state:
            return
        self._state = new_state
        self._last_state_change = time.monotonic()
        logger.info(
            "[VoiceState] %s -> %s (prev=%s, gen=%d)",
            old_state.value,
            new_state.value,
            self._pre_mute_state.value,
            self._generation_id,
        )

        try:
            from core.ducking import notify_speech_activity
            if new_state == VoiceState.SPEAKING:
                notify_speech_activity(True)
            elif old_state == VoiceState.SPEAKING:
                notify_speech_activity(False)
        except Exception:
            pass

        if self._on_state_change:
            try:
                self._on_state_change(old_state, new_state)
            except Exception as e:
                logger.warning("Error in on_state_change callback: %s", e)
