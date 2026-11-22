"""
tests/test_chat_export.py — End-to-end tests for chat extraction, page_to_file,
speech-noise filtering, and language-restricted confirmation matching.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from actions import _chat_extractors
from actions.page_to_file import page_to_file
from core import confirm
from core.speech_filter import (
    is_allowed_language,
    is_speech_noise,
    should_drop_transcript,
)
from memory import config_manager


# ── 1. Mock ChatGPT Page DOM Fixture ──────────────────────────────────────────

CHATGPT_HTML_FIXTURE = """<!DOCTYPE html>
<html>
<head><title>ChatGPT - Python Optimization</title></head>
<body>
  <nav class="sidebar">
    <a href="/c/chat-1">Chat History 1</a>
    <a href="/c/chat-2">Chat History 2</a>
  </nav>
  <main>
    <div data-message-author-role="user">
      <div class="prose">
        <p>How do I optimize a list comprehension in Python?</p>
      </div>
      <button aria-label="Edit message">Edit</button>
      <svg><path d="M0 0"/></svg>
    </div>
    <div data-message-author-role="assistant">
      <div class="prose">
        <p>Here is an example using generator expressions:</p>
        <pre><code class="language-python">
# Compute squares lazily
squares = (x * x for x in range(1000))
        </code></pre>
        <button aria-label="Copy code">Copy</button>
        <button class="feedback">Thumbs Up</button>
        <svg><circle cx="5" cy="5" r="5"/></svg>
      </div>
    </div>
  </main>
</body>
</html>
"""


def test_chatgpt_dom_extraction_with_playwright(tmp_path):
    """Test DOM extraction JavaScript in a real headless browser context if available, or fallback."""
    from playwright.sync_api import sync_playwright

    html_file = tmp_path / "chatgpt_fixture.html"
    html_file.write_text(CHATGPT_HTML_FIXTURE, encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(html_file.as_uri())

        # Evaluate the extraction JS directly
        extracted = page.evaluate(_chat_extractors.CHATGPT_EXTRACTOR_JS)
        browser.close()

    assert len(extracted) == 2

    user_turn = extracted[0]
    assert user_turn["role"] == "user"
    assert user_turn["author"] == "You"
    assert "How do I optimize a list comprehension in Python?" in user_turn["text"]
    assert "Edit" not in user_turn["text"]

    assistant_turn = extracted[1]
    assert assistant_turn["role"] == "assistant"
    assert assistant_turn["author"] == "ChatGPT"
    assert "Here is an example using generator expressions:" in assistant_turn["text"]
    # Verify code block fences are preserved with language
    assert "```python" in assistant_turn["text"]
    assert "squares = (x * x for x in range(1000))" in assistant_turn["text"]
    assert "```" in assistant_turn["text"]
    # Verify UI noise buttons dropped
    assert "Copy" not in assistant_turn["text"]
    assert "Thumbs Up" not in assistant_turn["text"]


# ── 2. End-to-End Extraction and Atomic File Write ────────────────────────────

def test_chat_export_to_file_and_verification(tmp_path):
    dest_dir = tmp_path / "saved_chats"
    messages = [
        {"role": "user", "author": "You", "text": "How do I optimize a list comprehension in Python?"},
        {
            "role": "assistant",
            "author": "ChatGPT",
            "text": "Here is an example:\n```python\nsquares = (x * x for x in range(1000))\n```",
        },
    ]

    formatted = _chat_extractors.format_conversation_text(messages, title="Python Optimization", format_type="txt")
    out_file = _chat_extractors.write_file_atomic(dest_dir, "python_optimization.txt", formatted)

    assert out_file.exists()
    assert out_file.stat().st_size > 0

    ok, msg = _chat_extractors.verify_written_file(out_file, messages, len(formatted))
    assert ok is True
    assert "Verified" in msg

    text = out_file.read_text(encoding="utf-8")
    assert "You:\nHow do I optimize a list comprehension in Python?" in text
    assert "ChatGPT:\nHere is an example:" in text
    assert "```python" in text


# ── 3. "Open It" Triggers Editor Launch ───────────────────────────────────────

def test_page_to_file_open_file_launches_editor(tmp_path):
    mock_mgr = MagicMock()
    mock_mgr.check_connection.return_value = (True, "Connected")
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://chatgpt.com/c/active"
    mock_mgr.active_page = mock_page

    messages = [
        {"role": "user", "author": "You", "text": "Test question"},
        {"role": "assistant", "author": "ChatGPT", "text": "Test answer"},
    ]

    def mock_run_coro(coro, timeout=30.0):
        if hasattr(coro, "close"):
            coro.close()
        return messages

    mock_mgr.run_coroutine.side_effect = mock_run_coro

    dest_folder = str(tmp_path / "chats")
    with patch("actions.page_to_file.get_browser_session_manager", return_value=mock_mgr), \
         patch("os.startfile") as mock_startfile:
        res = page_to_file({
            "goal": "save this chat and open it",
            "folder": dest_folder,
            "filename": "my_opened_chat.txt",
        })

    assert "status=done" in res
    assert "verdict=verified" in res
    assert mock_startfile.called
    opened_path = str(mock_startfile.call_args[0][0])
    assert "my_opened_chat.txt" in opened_path


# ── 4. Empty Extraction Returns not_done and Creates No File ─────────────────

def test_empty_extraction_returns_not_done_and_no_file(tmp_path):
    mock_mgr = MagicMock()
    mock_mgr.check_connection.return_value = (True, "Connected")
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://example.com/empty"
    mock_mgr.active_page = mock_page

    # Returns empty messages
    def mock_run_coro(coro, timeout=30.0):
        if hasattr(coro, "close"):
            coro.close()
        return []

    mock_mgr.run_coroutine.side_effect = mock_run_coro

    dest_folder = tmp_path / "empty_target"
    with patch("actions.page_to_file.get_browser_session_manager", return_value=mock_mgr):
        res = page_to_file({
            "folder": str(dest_folder),
            "filename": "should_not_exist.txt",
        })

    assert "status=failed" in res
    assert "verdict=not_done" in res
    # Assert target file was never created
    assert not (dest_folder / "should_not_exist.txt").exists()


# ── 5. Speech Noise and Foreign Language Artifact Rejection ──────────────────

def test_foreign_language_transcripts_are_dropped():
    # Korean Hangul hallucinations (e.g. "감사합니다")
    drop_ko, r_ko = should_drop_transcript("감사합니다", allowed_langs=["en"])
    assert drop_ko is True
    assert r_ko == "disallowed_language"

    # Foreign Latin hallucinations (e.g. "Confirmo", "Muchas gracias")
    drop_confirmo, r_conf = should_drop_transcript("Confirmo", allowed_langs=["en"])
    assert drop_confirmo is True
    assert r_conf == "disallowed_language"

    drop_gracias, r_gracias = should_drop_transcript("muchas gracias", allowed_langs=["en"])
    assert drop_gracias is True
    assert r_gracias == "disallowed_language"

    # Russian Cyrillic
    drop_ru, r_ru = should_drop_transcript("Спасибо", allowed_langs=["en"])
    assert drop_ru is True
    assert r_ru == "disallowed_language"

    # English valid instructions must NOT be dropped
    drop_en, _ = should_drop_transcript("Save this chat to a file", allowed_langs=["en"])
    assert drop_en is False

    drop_confirm_en, _ = should_drop_transcript("confirm", allowed_langs=["en"])
    assert drop_confirm_en is False


def test_speech_noise_dropped_during_speaking_or_tail():
    # While speaking/tail: filler words and short noise must be dropped
    assert is_speech_noise("um", is_speaking_or_tail=True) is True
    assert is_speech_noise("uh", is_speaking_or_tail=True) is True
    assert is_speech_noise("ah", is_speaking_or_tail=True) is True
    assert is_speech_noise("mm", is_speaking_or_tail=True) is True
    assert is_speech_noise("er", is_speaking_or_tail=True) is True
    assert is_speech_noise("k", is_speaking_or_tail=True) is True

    # Real user turn must NOT be dropped as noise
    assert is_speech_noise("stop", is_speaking_or_tail=True) is False
    assert is_speech_noise("cancel that", is_speaking_or_tail=True) is False
    assert is_speech_noise("open the chat", is_speaking_or_tail=True) is False


def test_secondary_language_configuration():
    # When Spanish is enabled as secondary language:
    allowed = ["en", "es"]
    assert is_allowed_language("Sí", allowed_langs=allowed) is True
    assert is_allowed_language("confirmar", allowed_langs=allowed) is True
    # Korean is still rejected
    assert is_allowed_language("감사합니다", allowed_langs=allowed) is False


# ── 6. Confirmation Filtering by Configured STT Languages ─────────────────────

def test_confirmation_words_filtered_by_language():
    # Default English-only
    with patch.object(config_manager, "get_stt_languages", return_value=["en"]):
        assert confirm.matches_confirm("confirm") is True
        assert confirm.matches_confirm("yes") is True
        assert confirm.matches_confirm("send it") is True
        assert confirm.matches_confirm("proceed") is True

        # Foreign confirmation tokens must NOT match when language is English only
        assert confirm.matches_confirm("Confirmo") is False
        assert confirm.matches_confirm("confirmar") is False
        assert confirm.matches_confirm("bestätigen") is False
        assert confirm.matches_confirm("確認") is False

    # Multi-language configured: English + Spanish
    with patch.object(config_manager, "get_stt_languages", return_value=["en", "es"]):
        assert confirm.matches_confirm("confirm") is True
        assert confirm.matches_confirm("sí") is True
        assert confirm.matches_confirm("confirmar") is True
        assert confirm.matches_confirm("adelante") is True

        # German / Japanese still do not match
        assert confirm.matches_confirm("bestätigen") is False
        assert confirm.matches_confirm("確認") is False
