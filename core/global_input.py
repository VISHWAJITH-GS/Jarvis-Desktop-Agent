"""
core/global_input.py — Global Input Controller for MARK LVI / JARVIS

Provides system-wide keyboard shortcuts and optional mouse gestures that remain
active regardless of foreground focus, minimization, or JARVIS's current state
(LISTENING, SLEEPING, MUTED/DISABLED, PROCESSING, or SPEAKING).

Shortcuts:
- Toggle Mute:     Ctrl + Alt + J  (toggles between current state and muted;
                                    pressing again restores the previous state)
- Force Listening: Ctrl + Alt + K  (immediately forces JARVIS into LISTENING
                                    from ANY state, including SLEEPING or MUTED)
- Optional Simultaneous Mouse Gesture: Left + Right Mouse Button (Toggle Mute)

Backend Strategy (safe & dependency-free on Windows):
- Windows: Low-overhead 50 Hz `user32.GetAsyncKeyState` polling on a dedicated
  daemon thread with 40 ms debounce and edge-triggered latching. Never blocks
  other apps, survives UAC focus switches, and requires no extra packages.
- macOS / Linux: Optional fallback to `pynput.keyboard.GlobalHotKeys` or
  `keyboard.add_hotkey` if installed, with safe cleanup on `stop()`.
"""

from __future__ import annotations

import logging
import platform
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

# ─────────────────────────────────────────────────────────────────────────────
# Default Hotkey Chords
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_MUTE_CHORD: Tuple[str, ...] = ("ctrl", "alt", "j")
DEFAULT_FORCE_LISTENING_CHORD: Tuple[str, ...] = ("ctrl", "alt", "k")

# ─────────────────────────────────────────────────────────────────────────────
# Virtual Key Mapping (Windows)
# ─────────────────────────────────────────────────────────────────────────────

_VK_MAP: Dict[str, int] = {
    # Modifiers
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "menu": 0x12,
    "win": 0x5B,
    "windows": 0x5B,
    # Standard Keys
    "space": 0x20,
    "esc": 0x1B,
    "escape": 0x1B,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "capslock": 0x14,
    # Mouse Buttons
    "lbutton": 0x01,
    "rbutton": 0x02,
    "mbutton": 0x04,
}

# Add Letters A-Z
for _c in range(ord("a"), ord("z") + 1):
    _VK_MAP[chr(_c)] = ord(chr(_c).upper())

# Add Digits 0-9
for _d in range(10):
    _VK_MAP[str(_d)] = 0x30 + _d

# Add Function Keys F1-F12
for _f in range(1, 13):
    _VK_MAP[f"f{_f}"] = 0x70 + (_f - 1)


def _normalize_chord_tokens(chord_def: str | list | tuple) -> List[str]:
    if isinstance(chord_def, str):
        return [k.strip().lower() for k in chord_def.replace("+", " ").replace("-", " ").split() if k.strip()]
    return [str(k).strip().lower() for k in chord_def if str(k).strip()]


def parse_chord(chord_def: str | list | tuple) -> List[int]:
    """Parse a chord string (e.g. 'ctrl+alt+j') or list into Windows virtual-key codes."""
    keys = _normalize_chord_tokens(chord_def)
    vk_codes: List[int] = []
    for k in keys:
        if k in _VK_MAP:
            vk_codes.append(_VK_MAP[k])
        else:
            logger.warning("Unknown key in chord: '%s'", k)
    return vk_codes


def _format_pynput_chord(chord_def: str | list | tuple) -> str:
    """Format a chord into pynput GlobalHotKeys syntax (e.g. '<ctrl>+<alt>+j')."""
    special = {
        "ctrl": "<ctrl>",
        "control": "<ctrl>",
        "alt": "<alt>",
        "menu": "<alt>",
        "shift": "<shift>",
        "win": "<cmd>",
        "windows": "<cmd>",
        "space": "<space>",
        "esc": "<esc>",
        "escape": "<esc>",
        "enter": "<enter>",
        "return": "<enter>",
        "tab": "<tab>",
    }
    tokens = _normalize_chord_tokens(chord_def)
    parts = [special.get(t, f"<{t}>" if t.startswith("f") and t[1:].isdigit() else t) for t in tokens]
    return "+".join(parts)


def _format_keyboard_chord(chord_def: str | list | tuple) -> str:
    """Format a chord into python `keyboard` syntax (e.g. 'ctrl+alt+j')."""
    return "+".join(_normalize_chord_tokens(chord_def))


# ─────────────────────────────────────────────────────────────────────────────
# Global Input Controller
# ─────────────────────────────────────────────────────────────────────────────


class GlobalInputController:
    """
    Monitors global keyboard shortcuts and mouse gestures across applications.
    Remains active even while JARVIS is sleeping or muted, and dispatches
    thread-safe callbacks to the central voice controller.
    """

    def __init__(
        self,
        on_toggle_listening: Optional[Callable[[], None]] = None,
        on_emergency_stop: Optional[Callable[[], None]] = None,
        listening_chord: str | list | tuple = DEFAULT_MUTE_CHORD,
        emergency_chord: str | list | tuple = DEFAULT_FORCE_LISTENING_CHORD,
        mouse_gesture_enabled: bool = True,
        poll_hz: float = 50.0,
        *,
        on_toggle_mute: Optional[Callable[[], None]] = None,
        on_force_listening: Optional[Callable[[], None]] = None,
        mute_chord: Optional[str | list | tuple] = None,
        force_listening_chord: Optional[str | list | tuple] = None,
    ):
        # Support both the explicit new names and legacy aliases seamlessly.
        self.on_toggle_mute = on_toggle_mute or on_toggle_listening
        self.on_force_listening = on_force_listening or on_emergency_stop

        effective_mute_chord = mute_chord if mute_chord is not None else listening_chord
        effective_force_chord = force_listening_chord if force_listening_chord is not None else emergency_chord

        self._mute_chord_raw: str | list | tuple = effective_mute_chord
        self._force_chord_raw: str | list | tuple = effective_force_chord

        self._listening_codes: Tuple[int, ...] = tuple(parse_chord(effective_mute_chord))
        self._emergency_codes: Tuple[int, ...] = tuple(parse_chord(effective_force_chord))
        self._mouse_gesture_enabled = bool(mouse_gesture_enabled)
        self._poll_hz = float(poll_hz)

        self._listening_chord_name = _format_keyboard_chord(effective_mute_chord)
        self._emergency_chord_name = _format_keyboard_chord(effective_force_chord)

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._backend: str = "none"
        self._pynput_listener: Any = None
        self._keyboard_handles: List[Any] = []

        # State latches to prevent repeated firing while held
        self._listening_latched = False
        self._emergency_latched = False
        self._mouse_latched = False
        self._listening_down_time = 0.0
        self._emergency_down_time = 0.0
        self._mouse_down_time = 0.0

    # Backward-compatible callback properties
    @property
    def on_toggle_listening(self) -> Optional[Callable[[], None]]:
        return self.on_toggle_mute

    @on_toggle_listening.setter
    def on_toggle_listening(self, cb: Optional[Callable[[], None]]) -> None:
        self.on_toggle_mute = cb

    @property
    def on_emergency_stop(self) -> Optional[Callable[[], None]]:
        return self.on_force_listening

    @on_emergency_stop.setter
    def on_emergency_stop(self, cb: Optional[Callable[[], None]]) -> None:
        self.on_force_listening = cb

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def is_running(self) -> bool:
        if self._backend == "windows_poll":
            return self._thread is not None and self._thread.is_alive()
        if self._backend == "pynput":
            return self._pynput_listener is not None and bool(getattr(self._pynput_listener, "running", False))
        if self._backend == "keyboard":
            return bool(self._keyboard_handles)
        return False

    @property
    def mouse_gesture_enabled(self) -> bool:
        return self._mouse_gesture_enabled

    @mouse_gesture_enabled.setter
    def mouse_gesture_enabled(self, value: bool) -> None:
        self._mouse_gesture_enabled = bool(value)

    def set_shortcuts(
        self,
        listening_chord: str | list | tuple,
        emergency_chord: str | list | tuple,
    ) -> None:
        """Update Toggle Mute and Force Listening shortcuts safely at runtime."""
        with self._lock:
            self._mute_chord_raw = listening_chord
            self._force_chord_raw = emergency_chord
            self._listening_codes = tuple(parse_chord(listening_chord))
            self._emergency_codes = tuple(parse_chord(emergency_chord))
            self._listening_chord_name = _format_keyboard_chord(listening_chord)
            self._emergency_chord_name = _format_keyboard_chord(emergency_chord)
        logger.info(
            "[GlobalInput] Shortcuts updated: ToggleMute=%s, ForceListening=%s",
            self._listening_chord_name,
            self._emergency_chord_name,
        )
        # Non-Windows hook backends need re-registration to apply new chords.
        if self._backend in ("pynput", "keyboard"):
            self.stop()
            self.start()

    def start(self) -> bool:
        """Start the global hotkey listener safely. Never raises."""
        with self._lock:
            if self.is_running:
                return True
            self._stop_event.clear()

            if _IS_WINDOWS:
                self._backend = "windows_poll"
                self._thread = threading.Thread(
                    target=self._monitor_loop,
                    name="jarvis-global-input",
                    daemon=True,
                )
                self._thread.start()
                logger.info(
                    "[GlobalInput] Started Windows global listener (ToggleMute: %s, ForceListening: %s, MouseGesture: %s)",
                    self._listening_chord_name,
                    self._emergency_chord_name,
                    self._mouse_gesture_enabled,
                )
                return True

            # Non-Windows safe fallback: try pynput first, then keyboard.
            if self._start_pynput():
                return True
            if self._start_keyboard_lib():
                return True

            logger.warning(
                "[GlobalInput] No global hotkey backend available on %s "
                "(install 'pynput' or 'keyboard' for system-wide shortcuts).",
                platform.system(),
            )
            self._backend = "none"
            return False

    def stop(self) -> None:
        """Stop the global input listener cleanly without leaking hooks."""
        self._stop_event.set()
        with self._lock:
            t = self._thread
            self._thread = None
            pynput_listener = self._pynput_listener
            self._pynput_listener = None
            kb_handles = list(self._keyboard_handles)
            self._keyboard_handles.clear()
            prev_backend = self._backend
            self._backend = "none"

        if t is not None and t.is_alive():
            t.join(timeout=1.0)

        if pynput_listener is not None:
            try:
                pynput_listener.stop()
            except Exception as e:
                logger.debug("[GlobalInput] Error stopping pynput listener: %s", e)

        if kb_handles:
            try:
                import keyboard  # type: ignore

                for h in kb_handles:
                    try:
                        keyboard.remove_hotkey(h)
                    except Exception:
                        pass
            except Exception:
                pass

        if prev_backend != "none":
            logger.info("[GlobalInput] Stopped global input listener (%s).", prev_backend)

    def __enter__(self) -> "GlobalInputController":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ── Non-Windows Backends ─────────────────────────────────────────────────

    def _start_pynput(self) -> bool:
        try:
            from pynput import keyboard as pynput_kb  # type: ignore

            hotkeys: Dict[str, Callable[[], None]] = {}
            mute_key = _format_pynput_chord(self._mute_chord_raw)
            force_key = _format_pynput_chord(self._force_chord_raw)
            if mute_key:
                hotkeys[mute_key] = lambda: self._dispatch(self.on_toggle_mute, "Global Toggle Mute")
            if force_key:
                hotkeys[force_key] = lambda: self._dispatch(self.on_force_listening, "Global Force Listening")
            if not hotkeys:
                return False

            listener = pynput_kb.GlobalHotKeys(hotkeys)
            listener.daemon = True
            listener.start()
            self._pynput_listener = listener
            self._backend = "pynput"
            logger.info(
                "[GlobalInput] Started pynput global listener (ToggleMute: %s, ForceListening: %s)",
                mute_key,
                force_key,
            )
            return True
        except Exception as e:
            logger.debug("[GlobalInput] pynput backend unavailable: %s", e)
            return False

    def _start_keyboard_lib(self) -> bool:
        try:
            import keyboard  # type: ignore

            handles = []
            if self._listening_chord_name:
                handles.append(
                    keyboard.add_hotkey(
                        self._listening_chord_name,
                        lambda: self._dispatch(self.on_toggle_mute, "Global Toggle Mute"),
                        suppress=False,
                    )
                )
            if self._emergency_chord_name:
                handles.append(
                    keyboard.add_hotkey(
                        self._emergency_chord_name,
                        lambda: self._dispatch(self.on_force_listening, "Global Force Listening"),
                        suppress=False,
                    )
                )
            if not handles:
                return False
            self._keyboard_handles = handles
            self._backend = "keyboard"
            logger.info(
                "[GlobalInput] Started keyboard-lib global listener (ToggleMute: %s, ForceListening: %s)",
                self._listening_chord_name,
                self._emergency_chord_name,
            )
            return True
        except Exception as e:
            logger.debug("[GlobalInput] keyboard backend unavailable: %s", e)
            return False

    # ── Windows Input Loop ───────────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        import ctypes

        user32 = ctypes.windll.user32
        period = 1.0 / max(10.0, self._poll_hz)

        VK_LBUTTON = 0x01
        VK_RBUTTON = 0x02

        while not self._stop_event.is_set():
            try:
                with self._lock:
                    mute_codes = self._listening_codes
                    force_codes = self._emergency_codes
                    mouse_enabled = self._mouse_gesture_enabled

                # 1. Global Toggle Mute (Ctrl + Alt + J)
                if len(mute_codes) >= 2:
                    mute_down = all(bool(user32.GetAsyncKeyState(vk) & 0x8000) for vk in mute_codes)
                    now = time.monotonic()
                    if mute_down:
                        if not self._listening_latched:
                            if self._listening_down_time == 0.0:
                                self._listening_down_time = now
                            elif now - self._listening_down_time >= 0.04:
                                self._listening_latched = True
                                self._dispatch(self.on_toggle_mute, "Global Toggle Mute")
                    else:
                        self._listening_latched = False
                        self._listening_down_time = 0.0

                # 2. Global Force Listening (Ctrl + Alt + K) — active even while asleep/muted
                if len(force_codes) >= 1:
                    force_down = all(bool(user32.GetAsyncKeyState(vk) & 0x8000) for vk in force_codes)
                    now = time.monotonic()
                    if force_down:
                        if not self._emergency_latched:
                            if self._emergency_down_time == 0.0:
                                self._emergency_down_time = now
                            elif now - self._emergency_down_time >= 0.04:
                                self._emergency_latched = True
                                self._dispatch(self.on_force_listening, "Global Force Listening")
                    else:
                        self._emergency_latched = False
                        self._emergency_down_time = 0.0

                # 3. Optional Mouse Gesture (Left + Right Click simultaneously -> Toggle Mute)
                if mouse_enabled:
                    l_down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
                    r_down = bool(user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000)

                    if l_down and r_down:
                        now = time.monotonic()
                        if not self._mouse_latched:
                            if self._mouse_down_time == 0.0:
                                self._mouse_down_time = now
                            # Short debounce (~40ms) to ensure a deliberate simultaneous click
                            elif now - self._mouse_down_time >= 0.04:
                                self._mouse_latched = True
                                self._dispatch(self.on_toggle_mute, "Mouse Gesture (L+R Click)")
                    else:
                        # Unlatch ONLY when BOTH buttons have been released
                        if not l_down and not r_down:
                            self._mouse_latched = False
                            self._mouse_down_time = 0.0

            except Exception as e:
                logger.debug("[GlobalInput] Monitor tick exception: %s", e)

            self._stop_event.wait(period)

    def _dispatch(self, callback: Optional[Callable[[], None]], name: str) -> None:
        if not callback:
            return
        logger.info("[GlobalInput] Triggered: %s", name)

        # Dispatch on a dedicated worker thread so a slow/failing callback never stalls polling.
        def _run() -> None:
            try:
                callback()
            except Exception as e:
                logger.error("[GlobalInput] Error in callback for %s: %s", name, e)

        threading.Thread(target=_run, name=f"input-dispatch-{name}", daemon=True).start()
