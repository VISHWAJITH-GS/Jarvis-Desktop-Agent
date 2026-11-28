"""
actions/gesture_control.py — On-Demand, Low-CPU Hand & Action Gesture Control Module.

Architecture & Features:
1. Low-Overhead Lifecycle & Resource Management:
   - Camera closed by default at boot (0% CPU / camera usage).
   - On-demand activation via enable_gesture_mode() / disable_gesture_mode() tools
     and GUI Settings Drawer toggle.
   - Optional auto-enable idle hook via Windows GetLastInputInfo.
   - Captures at 320x240 @ 15 FPS in a dedicated background daemon thread.
   - Camera and inference pipelines are released immediately upon disabling.
2. Local-Only Privacy:
   - Strictly local MediaPipe Tasks Vision (GestureRecognizer in LIVE_STREAM mode).
   - Zero frames written to disk or sent over any network.
3. 3-Stage Temporal State Machine (False-Trigger Prevention):
   - LOCKED (Default): Ignores all gestures except holding Open_Palm continuously
     for 1.0 second (confidence >= 0.75).
   - ARMED (5.0s active window): Plays subtle local chime, updates HUD indicator,
     and listens for an action gesture held steady for 0.45s:
     * Closed_Fist -> Toggle Play/Pause (VK_MEDIA_PLAY_PAUSE)
     * Thumb_Up    -> Approve pending Jarvis worker prompt or press Enter
     * Thumb_Down  -> Reject pending prompt or toggle system mute (VK_VOLUME_MUTE)
     * Victory     -> Next track (VK_MEDIA_NEXT_TRACK) or switch window (Alt+Tab)
     * Pointing_Up -> Trigger Jarvis Push-to-Talk voice listening mode
   - COOLDOWN (1.5s): Locks input after action execution before returning to LOCKED
     (or ARMED if continuous_mode is enabled).
4. Thread-Safe Integration:
   - Action dispatch and HUD notifications run without blocking PyQt6 or asyncio.
"""

from __future__ import annotations

import collections
import ctypes
import json
import logging
import os
import platform
import threading
import time
import urllib.request
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("gesture_control")
_IS_WINDOWS = platform.system() == "Windows"

# Standard Virtual Key Codes on Windows
VK_RETURN = 0x0D
VK_VOLUME_MUTE = 0xAD
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MENU = 0x12  # Alt
VK_TAB = 0x09   # Tab

# Model configuration
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task"
CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "gestures.json"
MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_PATH = MODELS_DIR / "gesture_recognizer.task"


# ── Windows Idle Input Hook ───────────────────────────────────────────────────

if _IS_WINDOWS:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_idle_time_seconds() -> float:
    """Returns seconds since last physical user keyboard or mouse input on Windows."""
    if not _IS_WINDOWS:
        return 0.0
    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return max(0.0, float(millis) / 1000.0)
    except Exception as e:
        logger.debug(f"[GestureControl] GetLastInputInfo failed: {e}")
    return 0.0


# ── Windows Virtual Key Press Simulation ──────────────────────────────────────

def press_vk_key(vk_code: int) -> None:
    """Simulate a clean key press and release using Windows keybd_event."""
    if not _IS_WINDOWS:
        return
    try:
        ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
        time.sleep(0.04)
        ctypes.windll.user32.keybd_event(vk_code, 0, 2, 0)  # KEYEVENTF_KEYUP = 2
    except Exception as e:
        logger.error(f"[GestureControl] press_vk_key error: {e}")


def press_alt_tab() -> None:
    """Simulate Alt+Tab window switch."""
    if not _IS_WINDOWS:
        return
    try:
        ctypes.windll.user32.keybd_event(VK_MENU, 0, 0, 0)
        time.sleep(0.02)
        ctypes.windll.user32.keybd_event(VK_TAB, 0, 0, 0)
        time.sleep(0.04)
        ctypes.windll.user32.keybd_event(VK_TAB, 0, 2, 0)
        time.sleep(0.02)
        ctypes.windll.user32.keybd_event(VK_MENU, 0, 2, 0)
    except Exception as e:
        logger.error(f"[GestureControl] press_alt_tab error: {e}")


# ── Windows Mouse Click Simulation ────────────────────────────────────────────

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010


def press_mouse_click(button: str = "left") -> None:
    """Simulate clean mouse click using Windows mouse_event."""
    if not _IS_WINDOWS:
        return
    try:
        if button == "left":
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        elif button == "right":
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_RIGHTDOWN | MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
    except Exception as e:
        logger.error(f"[GestureControl] press_mouse_click error: {e}")


# ── One-Finger Mouse Tracker ──────────────────────────────────────────────────

class FingerMouseTracker:
    """
    Real-time fingertip cursor tracking and click detection.
    - Smoothly maps index fingertip (landmark 8) coordinates to desktop screen resolution.
    - Applies adaptive exponential moving average (EMA) smoothing to eliminate camera jitter.
    - Uses index finger for steering mouse pointer.
    - Uses thumb extension for clicking (or pinch / both based on click_mode).
    """

    def __init__(
        self,
        enabled: bool = True,
        smoothing: float = 0.35,
        margin: float = 0.15,
        pinch_threshold: float = 0.055,
        thumb_extend_threshold: float = 1.05,
        click_mode: str = "both",
        click_cooldown: float = 0.35,
        on_click: Optional[Callable[[int, int], None]] = None,
    ):
        self.enabled = enabled
        self.smoothing = smoothing
        self.margin = margin
        self.pinch_threshold = pinch_threshold
        self.thumb_extend_threshold = thumb_extend_threshold
        self.click_mode = click_mode
        self.click_cooldown = click_cooldown
        self.on_click = on_click

        self.curr_x: Optional[float] = None
        self.curr_y: Optional[float] = None
        self.last_click_time: float = 0.0
        self.is_pinched: bool = False
        self.is_clicked: bool = False
        self._screen_w: int = 1920
        self._screen_h: int = 1080
        self._lock = threading.RLock()
        self._init_screen_size()

    def _init_screen_size(self) -> None:
        if _IS_WINDOWS:
            try:
                self._screen_w = ctypes.windll.user32.GetSystemMetrics(0)
                self._screen_h = ctypes.windll.user32.GetSystemMetrics(1)
            except Exception:
                self._screen_w, self._screen_h = 1920, 1080

    def is_pointing(self, landmarks: Any, top_gesture: str) -> bool:
        """Determines if user is pointing with single index finger."""
        if top_gesture == "Pointing_Up":
            return True
        if not landmarks or len(landmarks) < 21:
            return False
        try:
            # 1. Height-based check (when hand is upright: index extended, others curled)
            index_extended_y = landmarks[8].y < landmarks[6].y
            middle_curled_y = landmarks[12].y > landmarks[10].y
            ring_curled_y = landmarks[16].y > landmarks[14].y
            pinky_curled_y = landmarks[20].y > landmarks[18].y
            if index_extended_y and middle_curled_y and ring_curled_y and pinky_curled_y:
                return True

            # 2. Distance-from-wrist check (invariant to hand rotation/angle)
            w = landmarks[0]
            d_index_tip = (landmarks[8].x - w.x)**2 + (landmarks[8].y - w.y)**2
            d_index_pip = (landmarks[6].x - w.x)**2 + (landmarks[6].y - w.y)**2
            d_mid_tip = (landmarks[12].x - w.x)**2 + (landmarks[12].y - w.y)**2
            d_mid_mcp = (landmarks[9].x - w.x)**2 + (landmarks[9].y - w.y)**2
            d_ring_tip = (landmarks[16].x - w.x)**2 + (landmarks[16].y - w.y)**2
            d_ring_mcp = (landmarks[13].x - w.x)**2 + (landmarks[13].y - w.y)**2
            d_pinky_tip = (landmarks[20].x - w.x)**2 + (landmarks[20].y - w.y)**2
            d_pinky_mcp = (landmarks[17].x - w.x)**2 + (landmarks[17].y - w.y)**2

            index_extended_d = d_index_tip > d_index_pip * 1.1
            others_curled_d = (
                d_mid_tip < d_mid_mcp * 1.3 and
                d_ring_tip < d_ring_mcp * 1.3 and
                d_pinky_tip < d_pinky_mcp * 1.3
            )
            return bool(index_extended_d and others_curled_d)
        except Exception:
            return False

    def is_thumb_extended(self, landmarks: Any) -> bool:
        """
        Determines if user is extending their thumb outward for clicking.
        Compares distance from thumb tip (4) to index MCP (5), normalized by
        palm scale (distance between wrist 0 and index MCP 5) for distance invariance.
        """
        if not landmarks or len(landmarks) < 21:
            return False
        try:
            thumb_tip = landmarks[4]
            index_mcp = landmarks[5]
            wrist = landmarks[0]

            palm_scale = max(0.01, ((index_mcp.x - wrist.x)**2 + (index_mcp.y - wrist.y)**2)**0.5)
            d_thumb_mcp = ((thumb_tip.x - index_mcp.x)**2 + (thumb_tip.y - index_mcp.y)**2)**0.5
            ratio = d_thumb_mcp / palm_scale

            return bool(ratio >= self.thumb_extend_threshold or d_thumb_mcp >= 0.22)
        except Exception:
            return False

    def update(
        self,
        landmarks: Any,
        top_gesture: str,
        current_time: Optional[float] = None,
    ) -> Tuple[Optional[Tuple[int, int]], bool]:
        """
        Process hand landmarks. If pointing, maps index fingertip to screen and detects clicks.
        Supports extending thumb for clicking (and/or pinch-to-click).
        Returns: ((screen_x, screen_y), clicked: bool)
        """
        if not self.enabled or not landmarks or len(landmarks) < 21:
            with self._lock:
                self.curr_x, self.curr_y = None, None
            return None, False

        now = time.time() if current_time is None else current_time

        if not self.is_pointing(landmarks, top_gesture):
            with self._lock:
                self.curr_x, self.curr_y = None, None
            return None, False

        index_tip = landmarks[8]
        thumb_tip = landmarks[4]

        # 1. Click detection: Thumb Extension and / or Pinch
        dist_pinch = ((thumb_tip.x - index_tip.x)**2 + (thumb_tip.y - index_tip.y)**2) ** 0.5
        pinch_detected = dist_pinch <= self.pinch_threshold
        thumb_extended = self.is_thumb_extended(landmarks)

        if self.click_mode == "thumb_extend":
            should_click = thumb_extended
        elif self.click_mode == "pinch":
            should_click = pinch_detected
        else:  # "both" (default)
            should_click = thumb_extended or pinch_detected

        clicked = False
        if should_click:
            with self._lock:
                if not self.is_clicked and (now - self.last_click_time) >= self.click_cooldown:
                    self.is_clicked = True
                    self.is_pinched = True  # backward compatibility
                    self.last_click_time = now
                    clicked = True
        else:
            with self._lock:
                self.is_clicked = False
                self.is_pinched = False

        # 2. Coordinate mapping with margin and horizontal mirroring
        mirrored_x = 1.0 - index_tip.x
        norm_w = max(0.01, 1.0 - 2.0 * self.margin)
        clamped_x = max(0.0, min(1.0, (mirrored_x - self.margin) / norm_w))
        clamped_y = max(0.0, min(1.0, (index_tip.y - self.margin) / norm_w))

        target_x = clamped_x * self._screen_w
        target_y = clamped_y * self._screen_h

        # 3. Adaptive Exponential Moving Average (EMA) smoothing with click anti-jitter
        with self._lock:
            # During click hold (thumb extended or pinched), freeze cursor to anchor click accurately without jitter
            if (self.is_clicked or self.is_pinched) and self.curr_x is not None and self.curr_y is not None:
                final_x = int(self.curr_x)
                final_y = int(self.curr_y)
            elif self.curr_x is None or self.curr_y is None:
                self.curr_x = target_x
                self.curr_y = target_y
                final_x = int(self.curr_x)
                final_y = int(self.curr_y)
            else:
                dx = target_x - self.curr_x
                dy = target_y - self.curr_y
                dist = (dx**2 + dy**2) ** 0.5
                if self.smoothing >= 1.0:
                    alpha = 1.0
                else:
                    alpha = min(0.90, max(self.smoothing, dist / 220.0))
                self.curr_x = alpha * target_x + (1.0 - alpha) * self.curr_x
                self.curr_y = alpha * target_y + (1.0 - alpha) * self.curr_y
                final_x = int(self.curr_x)
                final_y = int(self.curr_y)

        # Move Windows mouse cursor
        if _IS_WINDOWS:
            try:
                ctypes.windll.user32.SetCursorPos(final_x, final_y)
            except Exception as e:
                logger.debug(f"[GestureControl] SetCursorPos error: {e}")

        if clicked:
            press_mouse_click("left")
            if self.on_click:
                try:
                    self.on_click(final_x, final_y)
                except Exception:
                    pass

        return (final_x, final_y), clicked


# ── State Machine ─────────────────────────────────────────────────────────────

class GestureState(str, Enum):
    LOCKED = "LOCKED"
    ARMED = "ARMED"
    COOLDOWN = "COOLDOWN"


class GestureStateMachine:
    """
    3-Stage Temporal State Machine preventing false triggers:
    1. LOCKED: Discards all gestures except holding Open_Palm for lock_to_arm_duration (1.0s).
    2. ARMED: Active for armed_timeout (5.0s). Listens for an action gesture held steady
       for action_hold_duration (0.45s).
    3. COOLDOWN: Discards all gestures for cooldown_duration (1.5s), then resets to LOCKED.
    """

    def __init__(
        self,
        lock_confidence: float = 0.75,
        action_confidence: float = 0.70,
        lock_to_arm_duration: float = 1.0,
        armed_timeout: float = 5.0,
        action_hold_duration: float = 0.45,
        cooldown_duration: float = 1.5,
        continuous_mode: bool = False,
        actions_map: Optional[Dict[str, str]] = None,
        on_state_change: Optional[Callable[[GestureState, GestureState], None]] = None,
        on_action: Optional[Callable[[str, str], None]] = None,
    ):
        self.state = GestureState.LOCKED
        self.lock_confidence = lock_confidence
        self.action_confidence = action_confidence
        self.lock_to_arm_duration = lock_to_arm_duration
        self.armed_timeout = armed_timeout
        self.action_hold_duration = action_hold_duration
        self.cooldown_duration = cooldown_duration
        self.continuous_mode = continuous_mode

        self.actions_map = actions_map or {
            "Closed_Fist": "toggle_play_pause",
            "Thumb_Up": "confirm_or_enter",
            "Thumb_Down": "reject_or_mute",
            "Victory": "next_track_or_tab",
            "Pointing_Up": "trigger_push_to_talk",
        }

        self.on_state_change = on_state_change
        self.on_action = on_action

        # Internal tracking
        self._current_gesture: Optional[str] = None
        self._gesture_start_time: float = 0.0
        self._armed_start_time: float = 0.0
        self._cooldown_start_time: float = 0.0
        self._lock = threading.RLock()

    def reset(self) -> None:
        """Reset state machine to initial LOCKED state."""
        with self._lock:
            old = self.state
            self.state = GestureState.LOCKED
            self._current_gesture = None
            self._gesture_start_time = 0.0
            self._armed_start_time = 0.0
            self._cooldown_start_time = 0.0
            if old != GestureState.LOCKED and self.on_state_change:
                self.on_state_change(old, GestureState.LOCKED)

    def process_gesture(
        self,
        gesture_name: str,
        confidence: float,
        current_time: Optional[float] = None,
    ) -> Tuple[GestureState, Optional[str]]:
        """
        Process a detected gesture name and confidence score.
        Returns: (current_state, triggered_action_or_None)
        """
        now = time.time() if current_time is None else current_time
        triggered_action: Optional[str] = None

        with self._lock:
            # ── 1. COOLDOWN State ─────────────────────────────────────────────
            if self.state == GestureState.COOLDOWN:
                if (now - self._cooldown_start_time) >= (self.cooldown_duration - 1e-4):
                    old = self.state
                    target_state = GestureState.ARMED if self.continuous_mode else GestureState.LOCKED
                    self.state = target_state
                    self._current_gesture = None
                    self._gesture_start_time = 0.0
                    if target_state == GestureState.ARMED:
                        self._armed_start_time = now
                    if self.on_state_change:
                        self.on_state_change(old, target_state)
                else:
                    return GestureState.COOLDOWN, None

            # ── 2. LOCKED State ───────────────────────────────────────────────
            if self.state == GestureState.LOCKED:
                if gesture_name == "Open_Palm" and confidence >= self.lock_confidence:
                    if self._current_gesture == "Open_Palm":
                        hold_time = now - self._gesture_start_time
                        if hold_time >= (self.lock_to_arm_duration - 1e-4):
                            old = self.state
                            self.state = GestureState.ARMED
                            self._armed_start_time = now
                            self._current_gesture = None
                            self._gesture_start_time = 0.0
                            if self.on_state_change:
                                self.on_state_change(old, GestureState.ARMED)
                    else:
                        self._current_gesture = "Open_Palm"
                        self._gesture_start_time = now
                else:
                    self._current_gesture = None
                    self._gesture_start_time = 0.0

                return self.state, None

            # ── 3. ARMED State ────────────────────────────────────────────────
            if self.state == GestureState.ARMED:
                # Check for 5.0s active window timeout
                if (now - self._armed_start_time) >= (self.armed_timeout - 1e-4):
                    old = self.state
                    self.state = GestureState.LOCKED
                    self._current_gesture = None
                    self._gesture_start_time = 0.0
                    if self.on_state_change:
                        self.on_state_change(old, GestureState.LOCKED)
                    return GestureState.LOCKED, None

                # Check for candidate action gesture
                if gesture_name in self.actions_map and confidence >= self.action_confidence:
                    if self._current_gesture == gesture_name:
                        hold_time = now - self._gesture_start_time
                        if hold_time >= (self.action_hold_duration - 1e-4):
                            action = self.actions_map[gesture_name]
                            triggered_action = action
                            old = self.state
                            self.state = GestureState.COOLDOWN
                            self._cooldown_start_time = now
                            self._current_gesture = None
                            self._gesture_start_time = 0.0
                            if self.on_action:
                                self.on_action(action, gesture_name)
                            if self.on_state_change:
                                self.on_state_change(old, GestureState.COOLDOWN)
                    else:
                        self._current_gesture = gesture_name
                        self._gesture_start_time = now
                else:
                    self._current_gesture = None
                    self._gesture_start_time = 0.0

                return self.state, triggered_action

        return self.state, None


# ── Gesture Control Service ───────────────────────────────────────────────────

class GestureControlService:
    """
    On-Demand, low-CPU camera capture and MediaPipe gesture inference engine.
    Ensures camera is closed when disabled (0% CPU/camera usage) and dispatches
    actions thread-safely.
    """

    _instance: Optional[GestureControlService] = None

    def __init__(self, player: Any = None, live_session: Any = None):
        self.player = player
        self.live_session = live_session
        self.config: Dict[str, Any] = self._load_config()

        self.state_machine = GestureStateMachine(
            lock_confidence=float(self.config.get("lock_confidence", 0.75)),
            action_confidence=float(self.config.get("action_confidence", 0.70)),
            lock_to_arm_duration=float(self.config.get("lock_to_arm_duration", 1.0)),
            armed_timeout=float(self.config.get("armed_timeout", 5.0)),
            action_hold_duration=float(self.config.get("action_hold_duration", 0.45)),
            cooldown_duration=float(self.config.get("cooldown_duration", 1.5)),
            continuous_mode=bool(self.config.get("continuous_mode", False)),
            actions_map=self.config.get("actions"),
            on_state_change=self._on_state_transition,
            on_action=self._on_action_triggered,
        )

        # One-finger mouse tracking
        mouse_cfg = self.config.get("mouse_tracking", {})
        self.mouse_tracker = FingerMouseTracker(
            enabled=bool(mouse_cfg.get("enabled", True)),
            smoothing=float(mouse_cfg.get("smoothing", 0.35)),
            margin=float(mouse_cfg.get("margin", 0.15)),
            pinch_threshold=float(mouse_cfg.get("pinch_threshold", 0.055)),
            thumb_extend_threshold=float(mouse_cfg.get("thumb_extend_threshold", 1.05)),
            click_mode=str(mouse_cfg.get("click_mode", "thumb_extend")),
            click_cooldown=float(mouse_cfg.get("click_cooldown", 0.35)),
            on_click=self._on_pinch_click,
        )

        self._enabled = False
        self._thread: Optional[threading.Thread] = None
        self._idle_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._recognizer: Any = None
        self._last_detected_gesture: str = "None"
        self._last_detected_confidence: float = 0.0

        # Start idle monitor if auto-enable is turned on in config
        if self.config.get("auto_enable_on_idle", False):
            self._start_idle_monitor()

    @classmethod
    def get_instance(cls, player: Any = None, live_session: Any = None) -> GestureControlService:
        if cls._instance is None:
            cls._instance = GestureControlService(player=player, live_session=live_session)
        else:
            if player is not None:
                cls._instance.player = player
            if live_session is not None:
                cls._instance.live_session = live_session
        return cls._instance

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from config/gestures.json with safe defaults."""
        defaults = {
            "enabled": False,
            "camera_index": 0,
            "frame_width": 320,
            "frame_height": 240,
            "target_fps": 15,
            "lock_confidence": 0.75,
            "action_confidence": 0.70,
            "lock_to_arm_duration": 1.0,
            "armed_timeout": 5.0,
            "action_hold_duration": 0.45,
            "cooldown_duration": 1.5,
            "continuous_mode": False,
            "auto_enable_on_idle": False,
            "idle_timeout_seconds": 120,
            "chime_enabled": true if "true" in "" else True,
            "actions": {
                "Closed_Fist": "toggle_play_pause",
                "Thumb_Up": "confirm_or_enter",
                "Thumb_Down": "reject_or_mute",
                "Victory": "next_track_or_tab",
                "Pointing_Up": "trigger_push_to_talk",
            },
        }
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    defaults.update(data)
            except Exception as e:
                logger.error(f"[GestureControl] Error loading gestures.json: {e}")
        return defaults

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def enable(self) -> Dict[str, Any]:
        """Start camera capture and gesture inference daemon thread."""
        with self._lock:
            if self._enabled:
                return {"status": "already_enabled", "message": "Gesture control is already active."}

            self._ensure_model_downloaded()
            self._enabled = True
            self._stop_event.clear()
            self.state_machine.reset()

            self._thread = threading.Thread(
                target=self._capture_and_infer_loop,
                daemon=True,
                name="gesture-capture-worker",
            )
            self._thread.start()

        self._update_hud_state("ENABLED (LOCKED)")
        logger.info("[GestureControl] Gesture control enabled.")
        return {"status": "enabled", "message": "Gesture control activated. Hold Open_Palm for 1s to arm."}

    def disable(self) -> Dict[str, Any]:
        """Stop camera capture and completely release camera hardware."""
        with self._lock:
            if not self._enabled:
                return {"status": "already_disabled", "message": "Gesture control is already inactive."}

            self._enabled = False
            self._stop_event.set()
            self.state_machine.reset()

        self._update_hud_state("DISABLED")
        logger.info("[GestureControl] Gesture control disabled and camera released.")
        return {"status": "disabled", "message": "Gesture control deactivated. Camera released."}

    def toggle(self) -> Dict[str, Any]:
        if self.is_enabled():
            return self.disable()
        return self.enable()

    # ── Model & MediaPipe Lifecycle ───────────────────────────────────────────

    def _ensure_model_downloaded(self) -> None:
        """Download gesture_recognizer.task from Google Cloud if missing."""
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        if not MODEL_PATH.exists() or MODEL_PATH.stat().st_size == 0:
            logger.info(f"[GestureControl] Downloading MediaPipe gesture model to {MODEL_PATH}...")
            try:
                urllib.request.urlretrieve(MODEL_URL, str(MODEL_PATH))
                logger.info("[GestureControl] MediaPipe gesture model downloaded successfully.")
            except Exception as e:
                logger.error(f"[GestureControl] Failed to download gesture model: {e}")
                raise RuntimeError(f"Could not download gesture model: {e}")

    def _init_recognizer(self) -> Any:
        """Initialize MediaPipe Tasks Vision GestureRecognizer in LIVE_STREAM mode."""
        try:
            import mediapipe as mp
            from mediapipe.tasks import python
            from mediapipe.tasks.python import vision

            def _on_result(result: Any, output_image: Any, timestamp_ms: int) -> None:
                try:
                    if result and result.gestures and len(result.gestures) > 0:
                        top_gesture = result.gestures[0][0]
                        self._last_detected_gesture = top_gesture.category_name
                        self._last_detected_confidence = float(top_gesture.score)
                    else:
                        self._last_detected_gesture = "None"
                        self._last_detected_confidence = 0.0

                    # Process in state machine
                    self.state_machine.process_gesture(
                        self._last_detected_gesture,
                        self._last_detected_confidence,
                    )

                    # One-finger mouse tracking and pinch click
                    if result and getattr(result, "hand_landmarks", None) and len(result.hand_landmarks) > 0:
                        landmarks = result.hand_landmarks[0]
                        coords, clicked = self.mouse_tracker.update(landmarks, self._last_detected_gesture)
                        if coords is not None and self.player and hasattr(self.player, "set_watcher_badge"):
                            badge_text = "🖐 MOUSE: CLICK" if clicked else f"🖐 MOUSE: ({coords[0]},{coords[1]})"
                            self.player.set_watcher_badge(badge_text, True)
                except Exception as cb_err:
                    logger.debug(f"[GestureControl] Result callback error: {cb_err}")

            options = vision.GestureRecognizerOptions(
                base_options=python.BaseOptions(model_asset_path=str(MODEL_PATH)),
                running_mode=vision.RunningMode.LIVE_STREAM,
                num_hands=1,
                min_hand_detection_confidence=0.55,
                min_hand_presence_confidence=0.55,
                min_tracking_confidence=0.55,
                result_callback=_on_result,
            )
            return vision.GestureRecognizer.create_from_options(options)
        except Exception as e:
            logger.error(f"[GestureControl] Failed to initialize MediaPipe recognizer: {e}")
            return None

    # ── Background Camera Loop ────────────────────────────────────────────────

    def _capture_and_infer_loop(self) -> None:
        """Dedicated background daemon loop running OpenCV and MediaPipe."""
        import cv2
        import mediapipe as mp

        cam_idx = int(self.config.get("camera_index", 0))
        target_w = int(self.config.get("frame_width", 320))
        target_h = int(self.config.get("frame_height", 240))
        target_fps = int(self.config.get("target_fps", 15))
        frame_interval = 1.0 / max(1, target_fps)

        recognizer = self._init_recognizer()
        if recognizer is None:
            logger.error("[GestureControl] Recognizer could not be initialized. Aborting capture.")
            with self._lock:
                self._enabled = False
            return

        cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW if _IS_WINDOWS else cv2.CAP_ANY)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, target_w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, target_h)

        if not cap.isOpened():
            logger.error(f"[GestureControl] Could not open camera {cam_idx}.")
            with self._lock:
                self._enabled = False
            try:
                recognizer.close()
            except Exception:
                pass
            return

        logger.info(f"[GestureControl] Camera opened: {target_w}x{target_h} @ {target_fps} FPS.")
        start_ts = time.time()

        try:
            while not self._stop_event.is_set():
                t_frame_start = time.time()

                ret, frame = cap.read()
                if not ret or frame is None:
                    time.sleep(0.05)
                    continue

                # Downsample to 320x240 for minimal CPU consumption
                if frame.shape[1] != target_w or frame.shape[0] != target_h:
                    frame = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_LINEAR)

                # Convert BGR to RGB for MediaPipe (never saved to disk or network)
                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)

                timestamp_ms = int((time.time() - start_ts) * 1000)
                try:
                    recognizer.recognize_async(mp_image, timestamp_ms)
                except Exception as rec_err:
                    logger.debug(f"[GestureControl] recognize_async error: {rec_err}")

                # Rate limit loop to target FPS
                elapsed = time.time() - t_frame_start
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

        finally:
            cap.release()
            try:
                recognizer.close()
            except Exception:
                pass
            logger.info("[GestureControl] Camera hardware released cleanly.")

    # ── Idle Detection Hook ───────────────────────────────────────────────────

    def _start_idle_monitor(self) -> None:
        """Start background daemon checking for keyboard/mouse idle time."""
        if self._idle_thread and self._idle_thread.is_alive():
            return

        def _idle_loop():
            idle_thresh = float(self.config.get("idle_timeout_seconds", 120))
            while True:
                try:
                    if self.config.get("auto_enable_on_idle", False):
                        idle_sec = get_idle_time_seconds()
                        if idle_sec >= idle_thresh and not self.is_enabled():
                            logger.info(f"[GestureControl] Auto-enabling after {idle_sec:.1f}s idle.")
                            self.enable()
                except Exception as e:
                    logger.debug(f"[GestureControl] Idle loop note: {e}")
                time.sleep(5.0)

        self._idle_thread = threading.Thread(target=_idle_loop, daemon=True, name="gesture-idle-monitor")
        self._idle_thread.start()

    # ── State Machine Callbacks & Action Dispatch ─────────────────────────────

    def _on_state_transition(self, old_state: GestureState, new_state: GestureState) -> None:
        """Trigger chime and HUD updates on state machine transitions."""
        logger.info(f"[GestureControl] State transition: {old_state.value} -> {new_state.value}")

        if new_state == GestureState.ARMED:
            # Play subtle local chime
            self._play_chime()
            self._update_hud_state("ARMED (5s Window)")
            if self.player:
                try:
                    self.player.write_log("GESTURE: ARMED — Ready for action gesture (Fist/Thumb/Victory/Point)")
                except Exception:
                    pass

        elif new_state == GestureState.COOLDOWN:
            self._update_hud_state("ACTION EXECUTED (Cooldown)")

        elif new_state == GestureState.LOCKED:
            self._update_hud_state("LOCKED (Hold Open_Palm to Arm)")

    def _on_action_triggered(self, action_name: str, gesture_name: str) -> None:
        """Execute desktop / Jarvis OS action thread-safely."""
        logger.info(f"[GestureControl] 🚀 Action triggered: {action_name} (from gesture '{gesture_name}')")
        if self.player:
            try:
                self.player.write_log(f"GESTURE ACTION: '{gesture_name}' -> {action_name.upper()}")
                if hasattr(self.player, "show_slide_alert"):
                    self.player.show_slide_alert(f"Gesture: {gesture_name} -> {action_name}", title="GESTURE ACTION")
            except Exception:
                pass

        self.execute_action(action_name)

    def execute_action(self, action_name: str) -> None:
        """Perform the mapped desktop automation or Jarvis voice action."""
        # 1. Closed_Fist -> Toggle Play/Pause media
        if action_name == "toggle_play_pause":
            press_vk_key(VK_MEDIA_PLAY_PAUSE)

        # 2. Thumb_Up -> Confirm / Approve pending Jarvis worker prompt or press Enter
        elif action_name == "confirm_or_enter":
            resolved = self._try_resolve_pending_worker(approved=True)
            if not resolved:
                press_vk_key(VK_RETURN)

        # 3. Thumb_Down -> Reject pending prompt or mute system audio
        elif action_name == "reject_or_mute":
            resolved = self._try_resolve_pending_worker(approved=False)
            if not resolved:
                press_vk_key(VK_VOLUME_MUTE)

        # 4. Victory -> Next media track or switch window
        elif action_name == "next_track_or_tab":
            press_vk_key(VK_MEDIA_NEXT_TRACK)

        # 5. Pointing_Up -> Trigger Jarvis Push-to-Talk voice listening mode
        elif action_name == "trigger_push_to_talk":
            self._trigger_voice_listening()

    def _try_resolve_pending_worker(self, approved: bool) -> bool:
        """Attempt to approve/reject any worker task paused in WAITING_APPROVAL."""
        try:
            from core.dispatcher import BackgroundWorkflowDispatcher, STATE_WAITING_APPROVAL

            dispatcher = BackgroundWorkflowDispatcher.get_instance(player=self.player, live_session=self.live_session)
            for tid, task in list(dispatcher.tasks.items()):
                if getattr(task, "status", None) == STATE_WAITING_APPROVAL:
                    res = dispatcher.resolve_worker_approval(task_id=tid, approved=approved)
                    logger.info(f"[GestureControl] Worker approval resolved via gesture: {res}")
                    return True
        except Exception as e:
            logger.debug(f"[GestureControl] Worker approval resolve note: {e}")
        return False

    def _trigger_voice_listening(self) -> None:
        """Activate Push-to-Talk or wake Jarvis listening mode."""
        if self.live_session:
            try:
                if hasattr(self.live_session, "wake"):
                    self.live_session.wake(reason="gesture_push_to_talk")
                elif hasattr(self.live_session, "_on_ptt"):
                    self.live_session._on_ptt(True)
                return
            except Exception as e:
                logger.debug(f"[GestureControl] Voice trigger note: {e}")

        # Fallback to platform_hooks
        try:
            from core import platform_hooks
            wake_fn = platform_hooks.get_hook("wake_jarvis")
            if wake_fn:
                wake_fn()
        except Exception:
            pass

    def _on_pinch_click(self, x: int, y: int) -> None:
        """Triggered when user pinches thumb and index finger while pointing."""
        logger.info(f"[GestureControl] 🖱 Pinch click executed at ({x}, {y})")
        if self.player:
            try:
                self.player.write_log(f"GESTURE: Pinch click at ({x}, {y})")
                if hasattr(self.player, "show_slide_alert"):
                    self.player.show_slide_alert(f"Click at ({x}, {y})", title="MOUSE CLICK")
            except Exception:
                pass

    def _play_chime(self) -> None:
        """Play subtle local audio chime on Windows."""
        if not self.config.get("chime_enabled", True) or not _IS_WINDOWS:
            return
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    def _update_hud_state(self, status_text: str) -> None:
        """Update HUD active watcher badge with gesture state."""
        if not self.player:
            return
        try:
            if hasattr(self.player, "set_watcher_badge"):
                if self.is_enabled():
                    self.player.set_watcher_badge(f"🖐 GESTURE: {status_text}", True)
                else:
                    self.player.set_watcher_badge("", False)
        except Exception:
            pass


# ── Action Handlers & Declarations for GEMINI_KEY_MAIN ────────────────────────

def enable_gesture_mode(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Enables low-CPU MediaPipe hand gesture control mode."""
    svc = GestureControlService.get_instance(player=player)
    res = svc.enable()
    return json.dumps(res)


def disable_gesture_mode(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Disables hand gesture control mode and completely releases the webcam."""
    svc = GestureControlService.get_instance(player=player)
    res = svc.disable()
    return json.dumps(res)


def get_gesture_status(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Returns current active status of gesture control mode and state machine."""
    svc = GestureControlService.get_instance(player=player)
    return json.dumps({
        "enabled": svc.is_enabled(),
        "mouse_control": svc.mouse_tracker.enabled,
        "state": svc.state_machine.state.value,
        "camera_index": svc.config.get("camera_index", 0),
        "continuous_mode": svc.config.get("continuous_mode", False),
    })


def enable_mouse_control(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Enables one-finger index fingertip mouse cursor control and click detection."""
    params = parameters or {}
    click_mode = params.get("click_mode")
    svc = GestureControlService.get_instance(player=player)
    if click_mode:
        svc.mouse_tracker.click_mode = str(click_mode)
    svc.mouse_tracker.enabled = True
    if not svc.is_enabled():
        svc.enable()
    mode_desc = (
        "extend thumb outward to click" if svc.mouse_tracker.click_mode == "thumb_extend"
        else ("extend thumb or pinch to click" if svc.mouse_tracker.click_mode == "both" else "pinch to click")
    )
    return json.dumps({
        "status": "enabled",
        "mouse_control": True,
        "click_mode": svc.mouse_tracker.click_mode,
        "message": f"One-finger mouse cursor tracking enabled. Point index finger to steer cursor, {mode_desc}.",
    })


def disable_mouse_control(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Disables one-finger mouse cursor tracking."""
    svc = GestureControlService.get_instance(player=player)
    svc.mouse_tracker.enabled = False
    return json.dumps({
        "status": "disabled",
        "mouse_control": False,
        "message": "One-finger mouse cursor tracking disabled.",
    })


TOOLS = [
    {
        "name": "enable_gesture_mode",
        "description": (
            "Activates on-demand local MediaPipe hand gesture control mode to control media, "
            "approve/reject worker tasks, and trigger voice listening from across the room."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
        "handler": enable_gesture_mode,
    },
    {
        "name": "disable_gesture_mode",
        "description": (
            "Deactivates hand gesture control mode and immediately closes the webcam to ensure 0% CPU usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
        "handler": disable_gesture_mode,
    },
    {
        "name": "enable_mouse_control",
        "description": (
            "Enables real-time one-finger mouse cursor steering using index fingertip and thumb extension / pinch clicking."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "click_mode": {
                    "type": "STRING",
                    "description": "Click trigger mechanism: 'thumb_extend' (extending thumb clicks, default), 'pinch', or 'both'.",
                }
            },
        },
        "handler": enable_mouse_control,
    },
    {
        "name": "disable_mouse_control",
        "description": "Disables real-time one-finger mouse cursor tracking.",
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
        "handler": disable_mouse_control,
    },
    {
        "name": "get_gesture_status",
        "description": "Returns whether gesture control mode and mouse tracking are active and current state.",
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
        "handler": get_gesture_status,
    },
]
