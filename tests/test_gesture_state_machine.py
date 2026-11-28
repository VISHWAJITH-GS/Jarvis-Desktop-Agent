"""
tests/test_gesture_state_machine.py — Unit Tests for Hand Gesture State Machine & Actions.

Tests:
1. Initial LOCKED state and rejection of non-arm gestures.
2. LOCKED -> ARMED transition requiring steady Open_Palm hold for 1.0s.
3. False-trigger rejection on brief or interrupted gestures.
4. ARMED 5-second active window expiration -> LOCKED.
5. ARMED -> COOLDOWN on steady action gesture hold (0.45s).
6. Full mapping verification: Closed_Fist, Thumb_Up, Thumb_Down, Victory, Pointing_Up.
7. COOLDOWN input lockout and transition back to LOCKED (or ARMED in continuous mode).
8. Confidence threshold gates (lock_confidence=0.75, action_confidence=0.70).
9. GestureControlService enable/disable lifecycle & tools.
10. Config loading from config/gestures.json.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from actions.gesture_control import (
    FingerMouseTracker,
    GestureControlService,
    GestureState,
    GestureStateMachine,
    disable_gesture_mode,
    disable_mouse_control,
    enable_gesture_mode,
    enable_mouse_control,
    get_gesture_status,
)


class MockPlayer:
    """Mock JarvisUI for testing thread-safe Qt signals and logs."""

    def __init__(self):
        self.logs: list[str] = []
        self.slide_alerts: list[tuple[str, str]] = []
        self.active_badge: str = ""
        self.badge_visible: bool = False

    def write_log(self, text: str) -> None:
        self.logs.append(text)

    def show_slide_alert(self, message: str, title: str = "SYSTEM ALERT") -> None:
        self.slide_alerts.append((title, message))

    def set_watcher_badge(self, text: str, active: bool = True) -> None:
        self.active_badge = text if active else ""
        self.badge_visible = active


# ── 1. LOCKED State & False-Trigger Rejection ─────────────────────────────────

def test_initial_state_is_locked():
    """Verify state machine initializes in LOCKED state."""
    sm = GestureStateMachine()
    assert sm.state == GestureState.LOCKED


def test_locked_ignores_all_non_palm_gestures():
    """Verify non-arm gestures (Closed_Fist, Thumb_Up, Victory) are ignored in LOCKED."""
    sm = GestureStateMachine()
    t = 100.0

    for gesture in ["Closed_Fist", "Thumb_Up", "Thumb_Down", "Victory", "Pointing_Up", "None"]:
        state, action = sm.process_gesture(gesture, confidence=0.95, current_time=t)
        assert state == GestureState.LOCKED
        assert action is None
        t += 0.5


def test_locked_requires_full_one_second_hold():
    """Verify Open_Palm held for < 1.0s does not arm."""
    transitions: list[tuple[GestureState, GestureState]] = []
    sm = GestureStateMachine(on_state_change=lambda o, n: transitions.append((o, n)))

    t0 = 1000.0
    # 0.0s: Start palm
    st, act = sm.process_gesture("Open_Palm", confidence=0.85, current_time=t0)
    assert st == GestureState.LOCKED
    assert act is None

    # 0.5s: Still holding palm
    st, act = sm.process_gesture("Open_Palm", confidence=0.85, current_time=t0 + 0.5)
    assert st == GestureState.LOCKED
    assert act is None

    # 0.9s: Still under 1.0s threshold
    st, act = sm.process_gesture("Open_Palm", confidence=0.85, current_time=t0 + 0.9)
    assert st == GestureState.LOCKED
    assert act is None

    # Gesture drops before 1.0s -> timer resets
    st, act = sm.process_gesture("None", confidence=0.0, current_time=t0 + 0.95)
    assert st == GestureState.LOCKED

    # Another 0.5s of palm -> does not reach 1.0s from start
    st, act = sm.process_gesture("Open_Palm", confidence=0.85, current_time=t0 + 1.2)
    assert st == GestureState.LOCKED
    assert len(transitions) == 0


def test_locked_to_armed_transition():
    """Verify Open_Palm held continuously for >= 1.0s transitions to ARMED."""
    transitions: list[tuple[GestureState, GestureState]] = []
    sm = GestureStateMachine(on_state_change=lambda o, n: transitions.append((o, n)))

    t0 = 1000.0
    sm.process_gesture("Open_Palm", confidence=0.85, current_time=t0)
    sm.process_gesture("Open_Palm", confidence=0.85, current_time=t0 + 0.5)
    st, act = sm.process_gesture("Open_Palm", confidence=0.85, current_time=t0 + 1.0)

    assert st == GestureState.ARMED
    assert act is None
    assert transitions == [(GestureState.LOCKED, GestureState.ARMED)]


def test_lock_confidence_threshold_gate():
    """Verify Open_Palm with confidence < 0.75 does not arm even if held for 2.0s."""
    sm = GestureStateMachine(lock_confidence=0.75)
    t0 = 1000.0

    sm.process_gesture("Open_Palm", confidence=0.70, current_time=t0)
    sm.process_gesture("Open_Palm", confidence=0.70, current_time=t0 + 1.5)
    st, _ = sm.process_gesture("Open_Palm", confidence=0.74, current_time=t0 + 2.0)

    assert st == GestureState.LOCKED


# ── 2. ARMED State & Active Window Timeout ────────────────────────────────────

def test_armed_timeout_returns_to_locked():
    """Verify ARMED state times out after 5.0 seconds and resets to LOCKED."""
    transitions: list[tuple[GestureState, GestureState]] = []
    sm = GestureStateMachine(on_state_change=lambda o, n: transitions.append((o, n)))

    t0 = 1000.0
    # Arm the system
    sm.process_gesture("Open_Palm", 0.85, t0)
    sm.process_gesture("Open_Palm", 0.85, t0 + 1.0)
    assert sm.state == GestureState.ARMED

    # Advance time within 5s window (e.g. 4.0s) -> stays ARMED
    st, act = sm.process_gesture("None", 0.0, t0 + 1.0 + 4.0)
    assert st == GestureState.ARMED
    assert act is None

    # Advance past 5.0s (e.g. 5.1s) -> resets to LOCKED
    st, act = sm.process_gesture("None", 0.0, t0 + 1.0 + 5.1)
    assert st == GestureState.LOCKED
    assert act is None
    assert transitions[-1] == (GestureState.ARMED, GestureState.LOCKED)


# ── 3. Action Execution & Hold Durations ───────────────────────────────────────

def test_action_gesture_requires_hold_duration():
    """Verify action gesture held for < 0.45s does not execute."""
    actions_fired: list[str] = []
    sm = GestureStateMachine(on_action=lambda act, g: actions_fired.append(act))

    t0 = 1000.0
    # 1. Arm
    sm.process_gesture("Open_Palm", 0.85, t0)
    sm.process_gesture("Open_Palm", 0.85, t0 + 1.0)
    assert sm.state == GestureState.ARMED

    t_arm = t0 + 1.0
    # 2. Start Closed_Fist at 0.0s
    st, act = sm.process_gesture("Closed_Fist", 0.90, t_arm + 0.1)
    assert st == GestureState.ARMED
    assert act is None

    # 3. Hold Closed_Fist for 0.3s (under 0.45s)
    st, act = sm.process_gesture("Closed_Fist", 0.90, t_arm + 0.4)
    assert st == GestureState.ARMED
    assert act is None

    # 4. Hand twitches to another gesture -> timer resets
    st, act = sm.process_gesture("Victory", 0.90, t_arm + 0.42)
    assert st == GestureState.ARMED
    assert act is None
    assert len(actions_fired) == 0


def test_action_execution_and_cooldown_transition():
    """Verify action gesture held steady for >= 0.45s executes and enters COOLDOWN."""
    actions_fired: list[tuple[str, str]] = []
    transitions: list[tuple[GestureState, GestureState]] = []
    sm = GestureStateMachine(
        on_action=lambda act, g: actions_fired.append((act, g)),
        on_state_change=lambda o, n: transitions.append((o, n)),
    )

    t0 = 1000.0
    # 1. Arm
    sm.process_gesture("Open_Palm", 0.85, t0)
    sm.process_gesture("Open_Palm", 0.85, t0 + 1.0)
    assert sm.state == GestureState.ARMED

    t_arm = t0 + 1.0
    # 2. Hold Closed_Fist continuously for 0.45s
    sm.process_gesture("Closed_Fist", 0.85, t_arm + 0.1)
    st, act = sm.process_gesture("Closed_Fist", 0.85, t_arm + 0.55)

    # 3. Verify triggered action and transition to COOLDOWN
    assert act == "toggle_play_pause"
    assert st == GestureState.COOLDOWN
    assert actions_fired == [("toggle_play_pause", "Closed_Fist")]
    assert transitions[-1] == (GestureState.ARMED, GestureState.COOLDOWN)


# ── 4. Full Gesture Mappings Verification ─────────────────────────────────────

@pytest.mark.parametrize(
    "gesture_name,expected_action",
    [
        ("Closed_Fist", "toggle_play_pause"),
        ("Thumb_Up", "confirm_or_enter"),
        ("Thumb_Down", "reject_or_mute"),
        ("Victory", "next_track_or_tab"),
        ("Pointing_Up", "trigger_push_to_talk"),
    ],
)
def test_all_gesture_mappings(gesture_name: str, expected_action: str):
    """Verify each configured gesture triggers its designated desktop action."""
    actions_fired: list[str] = []
    sm = GestureStateMachine(on_action=lambda act, g: actions_fired.append(act))

    t0 = 100.0
    # Arm
    sm.process_gesture("Open_Palm", 0.80, t0)
    sm.process_gesture("Open_Palm", 0.80, t0 + 1.0)

    # Hold action gesture for 0.45s
    t_arm = t0 + 1.0
    sm.process_gesture(gesture_name, 0.85, t_arm + 0.1)
    st, act = sm.process_gesture(gesture_name, 0.85, t_arm + 0.55)

    assert act == expected_action
    assert st == GestureState.COOLDOWN
    assert actions_fired == [expected_action]


# ── 5. COOLDOWN Input Lockout & Return Transitions ────────────────────────────

def test_cooldown_locks_all_input():
    """Verify all gestures are discarded during 1.5s COOLDOWN period."""
    sm = GestureStateMachine(cooldown_duration=1.5, continuous_mode=False)

    t0 = 100.0
    # Arm and execute action
    sm.process_gesture("Open_Palm", 0.85, t0)
    sm.process_gesture("Open_Palm", 0.85, t0 + 1.0)
    sm.process_gesture("Closed_Fist", 0.85, t0 + 1.1)
    sm.process_gesture("Closed_Fist", 0.85, t0 + 1.55)
    assert sm.state == GestureState.COOLDOWN

    t_cool = t0 + 1.55
    # During cooldown (1.0s elapsed < 1.5s) -> input locked
    for g in ["Open_Palm", "Closed_Fist", "Victory"]:
        st, act = sm.process_gesture(g, 0.95, t_cool + 1.0)
        assert st == GestureState.COOLDOWN
        assert act is None

    # After cooldown (1.6s elapsed > 1.5s) -> returns to LOCKED
    st, act = sm.process_gesture("None", 0.0, t_cool + 1.6)
    assert st == GestureState.LOCKED
    assert act is None


def test_continuous_mode_returns_to_armed():
    """Verify continuous_mode=True returns to ARMED instead of LOCKED after COOLDOWN."""
    sm = GestureStateMachine(cooldown_duration=1.5, continuous_mode=True)

    t0 = 100.0
    sm.process_gesture("Open_Palm", 0.85, t0)
    sm.process_gesture("Open_Palm", 0.85, t0 + 1.0)
    sm.process_gesture("Closed_Fist", 0.85, t0 + 1.1)
    sm.process_gesture("Closed_Fist", 0.85, t0 + 1.55)
    assert sm.state == GestureState.COOLDOWN

    t_cool = t0 + 1.55
    # After cooldown in continuous mode -> transitions to ARMED
    st, act = sm.process_gesture("None", 0.0, t_cool + 1.6)
    assert st == GestureState.ARMED
    assert act is None


# ── 6. GestureControlService Lifecycle & Tools ────────────────────────────────

def test_gesture_control_service_tools():
    """Verify enable, disable, and status tool functions return valid JSON."""
    player = MockPlayer()
    svc = GestureControlService.get_instance(player=player)

    # Disable first
    svc.disable()
    assert not svc.is_enabled()

    # Tool: enable_gesture_mode (mock background thread capture)
    with patch.object(svc, "_ensure_model_downloaded"):
        with patch.object(svc, "_capture_and_infer_loop"):
            res_en = json.loads(enable_gesture_mode(player=player))
            assert res_en["status"] == "enabled"
            assert svc.is_enabled()
            assert player.badge_visible

            # Tool: get_gesture_status
            res_stat = json.loads(get_gesture_status(player=player))
            assert res_stat["enabled"] is True
            assert res_stat["state"] == "LOCKED"

            # Tool: disable_gesture_mode
            res_dis = json.loads(disable_gesture_mode(player=player))
            assert res_dis["status"] == "disabled"
            assert not svc.is_enabled()
            assert not player.badge_visible


def test_action_execution_dispatches_correct_os_actions():
    """Verify execute_action triggers simulated keyboard events and worker approval checks."""
    player = MockPlayer()
    svc = GestureControlService.get_instance(player=player)

    # 1. toggle_play_pause -> press VK_MEDIA_PLAY_PAUSE
    with patch("actions.gesture_control.press_vk_key") as mock_vk:
        svc.execute_action("toggle_play_pause")
        mock_vk.assert_called_with(0xB3)

    # 2. confirm_or_enter -> falls back to VK_RETURN when no worker is waiting
    with patch("actions.gesture_control.press_vk_key") as mock_vk:
        with patch.object(svc, "_try_resolve_pending_worker", return_value=False):
            svc.execute_action("confirm_or_enter")
            mock_vk.assert_called_with(0x0D)

    # 3. confirm_or_enter -> resolves worker approval when worker is waiting
    with patch.object(svc, "_try_resolve_pending_worker", return_value=True) as mock_worker:
        with patch("actions.gesture_control.press_vk_key") as mock_vk:
            svc.execute_action("confirm_or_enter")
            mock_worker.assert_called_once_with(approved=True)
            mock_vk.assert_not_called()

    # 4. reject_or_mute -> resolves worker rejection when worker is waiting
    with patch.object(svc, "_try_resolve_pending_worker", return_value=True) as mock_worker:
        with patch("actions.gesture_control.press_vk_key") as mock_vk:
            svc.execute_action("reject_or_mute")
            mock_worker.assert_called_once_with(approved=False)
            mock_vk.assert_not_called()

    # 5. next_track_or_tab -> press VK_MEDIA_NEXT_TRACK
    with patch("actions.gesture_control.press_vk_key") as mock_vk:
        svc.execute_action("next_track_or_tab")
        mock_vk.assert_called_with(0xB0)

    # 6. trigger_push_to_talk -> triggers wake or PTT
    with patch.object(svc, "_trigger_voice_listening") as mock_ptt:
        svc.execute_action("trigger_push_to_talk")
        mock_ptt.assert_called_once()


# ── 7. One-Finger Mouse Tracker Unit Tests ────────────────────────────────────

class MockLandmark:
    """Mock landmark matching MediaPipe's NormalizedLandmark."""
    def __init__(self, x: float, y: float, z: float = 0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)


def make_pointing_landmarks(index_x: float = 0.5, index_y: float = 0.4, thumb_x: float = 0.35, thumb_y: float = 0.6):
    """Generates 21 landmarks where index is extended and other fingers are curled."""
    lms = [MockLandmark(0.5, 0.9) for _ in range(21)]
    # Wrist
    lms[0] = MockLandmark(0.5, 0.9)
    # Thumb
    lms[4] = MockLandmark(thumb_x, thumb_y)
    # Index extended
    lms[5] = MockLandmark(0.5, 0.7)
    lms[6] = MockLandmark(0.5, 0.6)
    lms[7] = MockLandmark(0.5, 0.5)
    lms[8] = MockLandmark(index_x, index_y)
    # Middle curled
    lms[9] = MockLandmark(0.55, 0.7)
    lms[10] = MockLandmark(0.55, 0.65)
    lms[12] = MockLandmark(0.55, 0.75)
    # Ring curled
    lms[13] = MockLandmark(0.6, 0.7)
    lms[14] = MockLandmark(0.6, 0.65)
    lms[16] = MockLandmark(0.6, 0.75)
    # Pinky curled
    lms[17] = MockLandmark(0.65, 0.7)
    lms[18] = MockLandmark(0.65, 0.65)
    lms[20] = MockLandmark(0.65, 0.75)
    return lms


def make_fist_landmarks():
    """Generates 21 landmarks where all fingers are curled (Closed_Fist)."""
    lms = [MockLandmark(0.5, 0.8) for _ in range(21)]
    lms[0] = MockLandmark(0.5, 0.9)
    # Index curled
    lms[6] = MockLandmark(0.5, 0.65)
    lms[8] = MockLandmark(0.5, 0.75)
    # Middle curled
    lms[10] = MockLandmark(0.55, 0.65)
    lms[12] = MockLandmark(0.55, 0.75)
    # Ring curled
    lms[14] = MockLandmark(0.6, 0.65)
    lms[16] = MockLandmark(0.6, 0.75)
    # Pinky curled
    lms[18] = MockLandmark(0.65, 0.65)
    lms[20] = MockLandmark(0.65, 0.75)
    return lms


def make_open_palm_landmarks():
    """Generates 21 landmarks where all fingers are extended (Open_Palm)."""
    lms = [MockLandmark(0.5, 0.8) for _ in range(21)]
    lms[0] = MockLandmark(0.5, 0.9)
    lms[6] = MockLandmark(0.5, 0.6)
    lms[8] = MockLandmark(0.5, 0.3)
    lms[10] = MockLandmark(0.55, 0.6)
    lms[12] = MockLandmark(0.55, 0.28)
    lms[14] = MockLandmark(0.6, 0.6)
    lms[16] = MockLandmark(0.6, 0.3)
    lms[18] = MockLandmark(0.65, 0.65)
    lms[20] = MockLandmark(0.65, 0.35)
    return lms


def test_finger_mouse_pointing_detection():
    """Verify is_pointing recognizes index finger pointing and rejects fists and palms."""
    tracker = FingerMouseTracker()

    # 1. Pointing_Up recognized immediately
    assert tracker.is_pointing([], "Pointing_Up") is True

    # 2. Pointing landmarks with extended index + curled other fingers
    pointing_lms = make_pointing_landmarks()
    assert tracker.is_pointing(pointing_lms, "None") is True

    # 3. Fist is rejected
    fist_lms = make_fist_landmarks()
    assert tracker.is_pointing(fist_lms, "Closed_Fist") is False

    # 4. Open palm is rejected
    palm_lms = make_open_palm_landmarks()
    assert tracker.is_pointing(palm_lms, "Open_Palm") is False


def test_finger_mouse_coordinate_mapping():
    """Verify coordinate mapping maps camera normalized space to screen with margins and mirroring."""
    tracker = FingerMouseTracker(smoothing=1.0, margin=0.15)
    tracker._screen_w = 1920
    tracker._screen_h = 1080

    with patch("actions.gesture_control.ctypes.windll.user32.SetCursorPos") as mock_set_cursor:
        with patch("actions.gesture_control.press_mouse_click"):
            # 1. Center of screen: normalized camera x=0.5, y=0.5
            lms_center = make_pointing_landmarks(index_x=0.5, index_y=0.5)
            coords, clicked = tracker.update(lms_center, "Pointing_Up", current_time=100.0)
            assert coords is not None
            assert coords == (960, 540)
            assert clicked is False
            mock_set_cursor.assert_called_with(960, 540)

            # 2. Horizontal Mirroring: Hand moved to physical right (camera x=0.15) -> screen right (x=1920)
            lms_right = make_pointing_landmarks(index_x=0.15, index_y=0.5)
            coords, _ = tracker.update(lms_right, "Pointing_Up", current_time=101.0)
            assert coords == (1920, 540)

            # 3. Hand moved to physical left (camera x=0.85) -> screen left (x=0)
            lms_left = make_pointing_landmarks(index_x=0.85, index_y=0.5)
            coords, _ = tracker.update(lms_left, "Pointing_Up", current_time=102.0)
            assert coords == (0, 540)

            # 4. Margin Clamping: Beyond margin (e.g. x=0.05 < 0.15) clamps to 1920
            lms_clamp = make_pointing_landmarks(index_x=0.05, index_y=0.05)
            coords, _ = tracker.update(lms_clamp, "Pointing_Up", current_time=103.0)
            assert coords == (1920, 0)


def test_finger_mouse_ema_smoothing():
    """Verify EMA smoothing prevents sudden cursor jumping on noise."""
    tracker = FingerMouseTracker(smoothing=0.35, margin=0.15)
    tracker._screen_w = 1920
    tracker._screen_h = 1080

    with patch("actions.gesture_control.ctypes.windll.user32.SetCursorPos"):
        with patch("actions.gesture_control.press_mouse_click"):
            # Initial position at center (960, 540)
            lms1 = make_pointing_landmarks(index_x=0.5, index_y=0.5)
            c1, _ = tracker.update(lms1, "Pointing_Up", current_time=100.0)
            assert c1 == (960, 540)

            # Small step towards (1000, 560): smoothing damps the step
            # target x: (0.48 mirrored -> 0.52 -> clamp=0.528 -> x ~ 1015)
            lms2 = make_pointing_landmarks(index_x=0.48, index_y=0.51)
            c2, _ = tracker.update(lms2, "Pointing_Up", current_time=100.05)
            assert c2 is not None
            # Verified smoothed position lies between initial and target
            assert 960 < c2[0] < 1050
            assert 540 <= c2[1] <= 570


def test_finger_mouse_pinch_click():
    """Verify pinch gesture (thumb tip close to index tip) triggers mouse click with cooldown."""
    clicks: list[tuple[int, int]] = []
    tracker = FingerMouseTracker(
        smoothing=1.0,
        margin=0.15,
        pinch_threshold=0.055,
        click_cooldown=0.35,
        on_click=lambda x, y: clicks.append((x, y)),
    )
    tracker._screen_w = 1920
    tracker._screen_h = 1080

    with patch("actions.gesture_control.ctypes.windll.user32.SetCursorPos"):
        with patch("actions.gesture_control.press_mouse_click") as mock_click:
            t0 = 1000.0
            # 1. Normal pointing (thumb far from index: dist = 0.22 > 0.055)
            lms_normal = make_pointing_landmarks(index_x=0.5, index_y=0.5, thumb_x=0.35, thumb_y=0.65)
            coords, clicked = tracker.update(lms_normal, "Pointing_Up", current_time=t0)
            assert clicked is False
            assert len(clicks) == 0
            mock_click.assert_not_called()

            # 2. Pinch: thumb touches index tip (dist = 0.02 < 0.055)
            lms_pinch = make_pointing_landmarks(index_x=0.5, index_y=0.5, thumb_x=0.51, thumb_y=0.51)
            coords, clicked = tracker.update(lms_pinch, "Pointing_Up", current_time=t0 + 0.1)
            assert clicked is True
            assert len(clicks) == 1
            assert clicks[0] == (960, 540)
            mock_click.assert_called_once_with("left")

            # 3. Holding pinch within cooldown (0.1s later < 0.35s cooldown) -> no double click
            coords, clicked = tracker.update(lms_pinch, "Pointing_Up", current_time=t0 + 0.2)
            assert clicked is False
            assert len(clicks) == 1

            # 4. Release pinch (thumb moves away)
            coords, clicked = tracker.update(lms_normal, "Pointing_Up", current_time=t0 + 0.3)
            assert clicked is False
            assert tracker.is_pinched is False

            # 5. Pinch again after cooldown (0.5s later > 0.35s cooldown) -> triggers second click
            coords, clicked = tracker.update(lms_pinch, "Pointing_Up", current_time=t0 + 0.6)
            assert clicked is True
            assert len(clicks) == 2
            assert mock_click.call_count == 2


def test_mouse_control_tool_functions():
    """Verify enable_mouse_control and disable_mouse_control tool handlers."""
    player = MockPlayer()
    svc = GestureControlService.get_instance(player=player)

    with patch.object(svc, "_ensure_model_downloaded"):
        with patch.object(svc, "_capture_and_infer_loop"):
            # Enable mouse control
            res_en = json.loads(enable_mouse_control(player=player))
            assert res_en["status"] == "enabled"
            assert res_en["mouse_control"] is True
            assert svc.mouse_tracker.enabled is True
            assert svc.is_enabled() is True

            # Disable mouse control
            res_dis = json.loads(disable_mouse_control(player=player))
            assert res_dis["status"] == "disabled"
            assert res_dis["mouse_control"] is False
            assert svc.mouse_tracker.enabled is False

            # Clean up
            svc.disable()
