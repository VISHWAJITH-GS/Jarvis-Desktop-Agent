import time
import pytest
from unittest.mock import MagicMock
from PyQt6.QtWidgets import QApplication

_qapp = QApplication.instance() or QApplication([])

from core.risk_tiers import RiskTier
from core.confirm import (
    save_pending,
    get_pending,
    reset_state,
    intercept_confirmation,
    resolve_by_button,
    PendingAction,
)


@pytest.fixture(autouse=True)
def clean_pending():
    reset_state()
    yield
    reset_state()


def test_medium_tier_voice_confirmation():
    called = []

    def mock_cb():
        called.append(True)
        return "Message sent successfully"

    save_pending(
        tool="whatsapp_send",
        params={"contact": "Alice", "text": "hello"},
        run=mock_cb,
        tier=RiskTier.MEDIUM,
        turn_id="turn_1",
    )

    # Confirm from next turn
    res = intercept_confirmation("confirm", turn_id="turn_2")
    assert res is not None
    assert res.success is True
    assert called == [True]
    assert "message sent successfully" in res.message.lower()
    assert get_pending() is None


def test_medium_tier_voice_cancellation():
    called = []

    def mock_cb():
        called.append(True)
        return "Done"

    save_pending(
        tool="whatsapp_send",
        params={"contact": "Alice", "text": "hello"},
        run=mock_cb,
        tier=RiskTier.MEDIUM,
    )

    res = intercept_confirmation("no, cancel that")
    assert res is not None
    assert res.success is False
    assert called == []
    assert "cancelled" in res.message.lower()
    assert get_pending() is None


def test_high_tier_dual_confirmation_voice_then_button():
    called = []

    def mock_cb():
        called.append(True)
        return "Files deleted"

    pending = save_pending(
        tool="delete_file",
        params={"path": "report.pdf"},
        run=mock_cb,
        tier=RiskTier.HIGH,
        turn_id="turn_1",
    )

    # 1. Voice confirm alone does not execute
    res_voice = intercept_confirmation("yes, confirm", turn_id="turn_2")
    assert res_voice is not None
    assert res_voice.success is False
    assert called == []
    assert "button" in res_voice.message.lower()
    assert pending.voice_confirmed is True
    assert pending.button_confirmed is False

    # 2. Physical button click completes the dual confirmation
    ok, msg = resolve_by_button(accepted=True, action_id=pending.id, sync=True)
    assert ok is True
    assert called == [True]
    assert "files deleted" in msg.lower()
    assert get_pending() is None


def test_high_tier_dual_confirmation_button_then_voice():
    called = []

    def mock_cb():
        called.append(True)
        return "System shutdown executed"

    pending = save_pending(
        tool="shutdown",
        params={},
        run=mock_cb,
        tier=RiskTier.HIGH,
        turn_id="turn_1",
    )

    # 1. Button click alone does not execute
    ok, msg = resolve_by_button(accepted=True, action_id=pending.id, sync=True)
    assert ok is False
    assert called == []
    assert "say confirm" in msg.lower() or "voice" in msg.lower()
    assert pending.button_confirmed is True
    assert pending.voice_confirmed is False

    # 2. Spoken confirmation completes dual confirmation
    res_voice = intercept_confirmation("confirm", turn_id="turn_2")
    assert res_voice is not None
    assert res_voice.success is True
    assert called == [True]
    assert "system shutdown executed" in res_voice.message.lower()
    assert get_pending() is None


def test_synthetic_clicks_blocked_on_button():
    from gui.overlays import ConfirmBanner
    from PyQt6.QtCore import QEvent

    synth_event = MagicMock()
    synth_event.spontaneous.return_value = False

    banner = ConfirmBanner(title="Confirm deletion", detail="Delete test file", tier="HIGH")
    banner.mousePressEvent(synth_event)

    assert synth_event.ignore.called


def test_tts_echo_suppression():
    save_pending(
        tool="whatsapp_send",
        params={"contact": "Alice", "text": "hello"},
        run=lambda: "Sent",
        tier=RiskTier.MEDIUM,
        turn_id="turn_1",
    )

    # When TTS is speaking, voice confirmations are rejected
    mock_player = MagicMock()
    mock_player.is_speaking = True
    res = intercept_confirmation("confirm", player=mock_player, turn_id="turn_2")
    assert res is None
    assert get_pending() is not None


def test_status_queries_do_not_confirm():
    save_pending(
        tool="whatsapp_send",
        params={"contact": "Alice", "text": "hello"},
        run=lambda: "Sent",
        tier=RiskTier.MEDIUM,
    )

    # Status query must NOT confirm
    res = intercept_confirmation("is it sent yet?")
    assert res is None or res.success is False
    assert get_pending() is not None

    res2 = intercept_confirmation("did you send it?")
    assert res2 is None or res2.success is False
    assert get_pending() is not None


def test_expiry_after_60_seconds():
    pending = save_pending(
        tool="whatsapp_send",
        params={"contact": "Alice", "text": "hello"},
        run=lambda: "Sent",
        tier=RiskTier.MEDIUM,
        turn_id="turn_1",
    )

    # Artificially age the pending action past 60s
    pending.created_at = time.monotonic() - 65

    res = intercept_confirmation("confirm", turn_id="turn_2")
    assert res is not None
    assert res.success is False
    assert "expired" in res.message.lower() or "timed out" in res.message.lower()
    assert get_pending() is None


def test_no_pending_action_reports_nothing_to_confirm():
    res = intercept_confirmation("confirm")
    assert res is not None
    assert res.success is False
    assert "nothing to confirm" in res.message.lower()


def test_confirm_executes_stored_action_exactly_once():
    execution_count = []

    def mock_action():
        execution_count.append(1)
        return "Chat exported to Desktop\\chat.txt successfully."

    save_pending(
        tool="file_controller",
        params={"action": "create_file", "path": "Desktop/chat.txt"},
        run=mock_action,
        tier=RiskTier.MEDIUM,
        title="Export Chat to Desktop",
        turn_id="turn_1",
    )

    # User says "confirm"
    res = intercept_confirmation("confirm", turn_id="turn_2")
    assert res is not None
    assert res.success is True
    assert "chat exported to desktop" in res.message.lower()
    assert execution_count == [1]

    # Verify pending action is cleared
    assert get_pending() is None


def test_canonicalize_params_maps_equivalent_calls_to_one_pending_id():
    from core.confirm import canonicalize_params, actions_equivalent

    # Equivalent file params (case, slashes, meta flags)
    p1 = {"path": "C:\\Users\\User\\Desktop\\Chat.txt", "_execution_authorized": False, "confirmed": False}
    p2 = {"path": "c:/users/user/desktop/chat.txt", "timestamp": 123456}

    assert actions_equivalent("file_controller", p1, "file_controller", p2) is True

    # Same for WhatsApp
    w1 = {"contact": "Ram Nath", "text": "Hello world", "platform": "whatsapp"}
    w2 = {"receiver": "ram nath ", "message": " Hello world", "action": "send_message"}
    assert actions_equivalent("whatsapp_send", w1, "send_message", w2) is True


def test_confirm_clears_pending_and_duplicate_does_not_rerun():
    execution_count = []

    def mock_action():
        execution_count.append(1)
        return "Operation executed."

    save_pending(
        tool="windows_control",
        params={"action": "close_app", "target": "notepad"},
        run=mock_action,
        tier=RiskTier.MEDIUM,
        title="Close Notepad",
        turn_id="turn_1",
    )

    # First confirm -> executes
    res1 = intercept_confirmation("confirm", turn_id="turn_2")
    assert res1.success is True
    assert execution_count == [1]
    assert get_pending() is None

    # Immediate second confirm -> reports already done, does NOT execute again
    res2 = intercept_confirmation("confirm", turn_id="turn_3")
    assert res2 is not None
    assert "already done" in res2.message.lower()
    assert execution_count == [1]


def test_strip_model_confirmation_phrases():
    from core.confirm import strip_model_confirmation_phrases

    msg1 = "I am ready to export the conversation. Shall I proceed with exporting? Say confirm to proceed or no to cancel."
    cleaned1 = strip_model_confirmation_phrases(msg1)
    assert "say confirm" not in cleaned1.lower()
    assert "I am ready to export the conversation." in cleaned1

    msg2 = "Please say confirm to execute."
    assert strip_model_confirmation_phrases(msg2) == ""

