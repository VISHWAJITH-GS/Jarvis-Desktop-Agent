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
