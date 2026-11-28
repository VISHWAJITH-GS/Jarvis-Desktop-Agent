"""
tests/test_startup_greeting.py — Tests for Startup Greeting Formatting & Listening State.

Verifies:
1. Startup greeting includes period, user name/sir, time, date, and month.
2. Startup greeting strictly excludes the calendar year (no 4-digit year).
3. JarvisLive initializes in awake=True and VoiceState.LISTENING (does not sleep on boot).
4. Auto-sleep defaults to disabled so Jarvis maintains listening state indefinitely.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from core.live_session import JarvisLive
from core.voice_state import VoiceState, VoiceStateManager


class DummyUI:
    def __init__(self):
        self.muted = False
        self.current_state = "LISTENING"
        self.logs: list[str] = []

    def set_state(self, state: str) -> None:
        self.current_state = state

    def write_log(self, text: str) -> None:
        self.logs.append(text)

    def sync_mute_state(self, is_muted: bool, label: str) -> None:
        self.current_state = label


def test_startup_greeting_format_no_year():
    """Verify startup greeting includes time, day, month, and date, but strictly excludes the year."""
    async def _run():
        mock_ui = DummyUI()
        live = object.__new__(JarvisLive)
        live.ui = mock_ui
        live.session = MagicMock()
        live.session.send_client_content = AsyncMock()
        live._turn_done_event = asyncio.Event()
        live._session_log = []
        live._last_user_speech = 1000.0

        sent_prompts: list[str] = []

        async def mock_send(turns, turn_complete):
            for p in turns.get("parts", []):
                if "text" in p:
                    sent_prompts.append(p["text"])

        live.session.send_client_content.side_effect = mock_send

        # Execute startup greeting
        await live._send_startup_briefing()

        assert len(sent_prompts) == 1
        prompt_text = sent_prompts[0]

        # 1. Verify period (Good morning / afternoon / evening)
        assert any(p in prompt_text for p in ["Good morning", "Good afternoon", "Good evening"])

        # 2. Verify time (e.g. "11:42 AM" or "9:05 PM")
        time_match = re.search(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b", prompt_text)
        assert time_match is not None, f"Time missing in prompt: {prompt_text}"

        # 3. Verify day of week (Monday..Sunday)
        day_match = re.search(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", prompt_text)
        assert day_match is not None, f"Day of week missing: {prompt_text}"

        # 4. Verify month name (January..December)
        month_match = re.search(
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b",
            prompt_text,
        )
        assert month_match is not None, f"Month missing: {prompt_text}"

        # 5. Verify ordinal date (e.g. 1st, 2nd, 3rd, 28th)
        date_match = re.search(r"\b\d{1,2}(?:st|nd|rd|th)\b", prompt_text)
        assert date_match is not None, f"Ordinal date missing: {prompt_text}"

        # 6. Strictly verify NO calendar year (e.g. 19xx or 20xx) is in greeting text
        # Extract quoted greeting inside prompt: Say exactly: '...'
        match_quoted = re.search(r"Say exactly:\s*'([^']+)'", prompt_text)
        assert match_quoted is not None
        greeting_str = match_quoted.group(1)

        current_year = str(datetime.now().year)
        assert current_year not in greeting_str, f"Year {current_year} found in greeting: {greeting_str}"
        assert re.search(r"\b(19\d\d|20\d\d)\b", greeting_str) is None, f"Year pattern found in: {greeting_str}"

        # 7. Verify prompt explicitly forbids the year
        assert "Do not mention the year." in prompt_text

    asyncio.run(_run())


def test_live_session_initializes_awake_and_listening():
    """Verify JarvisLive starts awake and in VoiceState.LISTENING (does not sleep on boot)."""
    mock_ui = DummyUI()

    live = object.__new__(JarvisLive)
    live.ui = mock_ui
    live._wake_enabled = True
    live._auto_sleep_enabled = False
    live._awake = True
    live.voice_state = VoiceStateManager(initial_state=VoiceState.LISTENING)

    # Must start awake
    assert live._awake is True
    assert live.voice_state.state == VoiceState.LISTENING
    assert live.voice_state.is_sleeping is False
    assert live.voice_state.is_listening is True


def test_run_sleep_watch_does_not_sleep_when_auto_sleep_disabled():
    """Verify _run_sleep_watch keeps Jarvis awake when auto-sleep is disabled."""
    async def _run():
        mock_ui = DummyUI()
        live = object.__new__(JarvisLive)
        live.ui = mock_ui
        live._auto_sleep_enabled = False
        live._wake_sleep_timeout = 0.0
        live._awake = True
        live._speaking_lock = MagicMock()
        live._is_speaking = False
        live._last_user_speech = 100.0
        live.sleep_called = False

        def mock_sleep(reason=""):
            live.sleep_called = True

        live.sleep = mock_sleep
        live.voice_state = VoiceStateManager(initial_state=VoiceState.LISTENING)

        # Let sleep watch cycle once
        task = asyncio.create_task(live._run_sleep_watch())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert live.sleep_called is False
        assert live.voice_state.is_listening is True

    asyncio.run(_run())
