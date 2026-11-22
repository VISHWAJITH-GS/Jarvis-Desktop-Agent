import pytest
from unittest.mock import MagicMock

from actions.jarvis_control import jarvis_control
from gui.display_registry import DisplayRegistry, register_display


@pytest.fixture
def mock_player():
    player = MagicMock()
    player.speaker_muted = False
    player.mic_muted = False
    player.is_sleeping = False
    return player


@pytest.fixture(autouse=True)
def clean_display_registry():
    DisplayRegistry.clear()
    yield
    DisplayRegistry.clear()


def test_mute_speaker(mock_player):
    res = jarvis_control({"action": "mute", "target": "speaker"}, player=mock_player)
    assert mock_player.mute_speaker.called or mock_player.speaker_muted is True
    assert "speaker" in res.lower()
    assert "muted" in res.lower()


def test_unmute_speaker(mock_player):
    mock_player.speaker_muted = True
    res = jarvis_control({"action": "unmute", "target": "speaker"}, player=mock_player)
    assert mock_player.unmute_speaker.called or mock_player.speaker_muted is False
    assert "speaker" in res.lower()
    assert "unmuted" in res.lower()


def test_mute_microphone(mock_player):
    res = jarvis_control({"action": "mute", "target": "microphone"}, player=mock_player)
    assert mock_player.mute_mic.called or mock_player.mic_muted is True
    assert "microphone" in res.lower()
    assert "muted" in res.lower()


def test_unmute_microphone(mock_player):
    mock_player.mic_muted = True
    res = jarvis_control({"action": "unmute", "target": "microphone"}, player=mock_player)
    assert mock_player.unmute_mic.called or mock_player.mic_muted is False
    assert "microphone" in res.lower()
    assert "active" in res.lower() or "listening" in res.lower()


def test_mute_both(mock_player):
    res = jarvis_control({"action": "mute", "target": "all"}, player=mock_player)
    assert "fully muted" in res.lower()


def test_sleep_and_wake(mock_player):
    res_sleep = jarvis_control({"action": "sleep"}, player=mock_player)
    assert mock_player.sleep_jarvis.called
    assert "sleep" in res_sleep.lower() or "standby" in res_sleep.lower()

    res_wake = jarvis_control({"action": "wake"}, player=mock_player)
    assert mock_player.wake_jarvis.called
    assert "awake" in res_wake.lower() or "listening" in res_wake.lower()


def test_minimize_and_restore_hud(mock_player):
    res_min = jarvis_control({"action": "minimize"}, player=mock_player)
    assert mock_player.minimize_jarvis.called
    assert "minimized" in res_min.lower()

    res_rest = jarvis_control({"action": "restore"}, player=mock_player)
    assert mock_player.restore_jarvis.called
    assert "restored" in res_rest.lower()


def test_shutdown_jarvis_unconfirmed_prompts_confirmation(mock_player):
    res = jarvis_control({"action": "shutdown_jarvis"}, player=mock_player)
    assert "confirm" in res.lower()


def test_shutdown_jarvis_confirmed_executes(mock_player):
    res = jarvis_control(
        {"action": "shutdown_jarvis", "_execution_authorized": True},
        player=mock_player,
    )
    assert "shutting down jarvis" in res.lower()


def test_display_commands_delegation(mock_player):
    win_mock = MagicMock()
    register_display(1, title="Telemetry Monitor", kind="telemetry", qt_window_ref=win_mock)

    # List displays
    res_list = jarvis_control({"action": "list_displays"}, player=mock_player)
    assert "telemetry monitor" in res_list.lower()

    # Minimize display
    res_min = jarvis_control({"action": "minimize_display", "target": "Telemetry"}, player=mock_player)
    assert "minimized" in res_min.lower()

    # Restore display
    res_rest = jarvis_control({"action": "restore_display", "target": "Telemetry"}, player=mock_player)
    assert "restored" in res_rest.lower() or "focus" in res_rest.lower()

    # Close display
    res_close = jarvis_control({"action": "close_display", "target": "Telemetry"}, player=mock_player)
    assert "closed" in res_close.lower()
    assert not DisplayRegistry.has_display(1)


def test_unknown_action(mock_player):
    res = jarvis_control({"action": "fly_to_mars"}, player=mock_player)
    assert "unknown" in res.lower() and "jarvis_control" in res.lower()
