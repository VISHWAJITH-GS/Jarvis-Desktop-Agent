"""
tests/test_on_demand_services.py — Tests for on-demand model and service lifecycle.
"""

from __future__ import annotations

import gc
from unittest.mock import MagicMock
from core.wake_word import WakeWordDetector
from dashboard.server import DashboardServer
from memory import config_manager
from providers import stt_whisper, tts_piper


def test_wake_word_single_detector_by_default():
    """Verify wake word runs only 1 detector by default, with keyword spotter disabled."""
    detector = WakeWordDetector(on_detect=lambda *_: None)
    assert detector._enable_keyword_spotter is False


def test_wake_word_stop_unloads_models():
    """Verify stop() clears models and unloads memory references."""
    detector = WakeWordDetector(on_detect=lambda *_: None)
    detector._model = MagicMock()
    detector._whisper_model = MagicMock()
    detector._ready = True

    detector.stop()

    assert detector._model is None
    assert detector._whisper_model is None
    assert detector._ready is False


def test_dashboard_server_starts_and_stops_on_demand():
    """Verify DashboardServer lifecycle: stop() sets should_exit flags cleanly."""
    server = DashboardServer()
    assert server._running is False

    mock_uvicorn_server = MagicMock()
    server._server = mock_uvicorn_server
    server._running = True

    server.stop()

    assert server._running is False
    assert mock_uvicorn_server.should_exit is True


def test_stt_and_tts_unload_methods():
    """Verify speech model unloading functions execute cleanly without raising."""
    stt = stt_whisper.WhisperSTT({})
    stt._model = MagicMock()
    stt.unload_model()
    assert stt._model is None

    tts_piper.PiperTTS.unload_voices()
    with tts_piper._voice_lock:
        assert len(tts_piper._voices) == 0
