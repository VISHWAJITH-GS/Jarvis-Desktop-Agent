from unittest.mock import MagicMock, patch

import pytest

from actions.clipboard_manager import clipboard_manager, detect_and_mask_secrets
from core.outcome import ActionResult
from plugins.conversation_search import (
    record_conversation_turn,
)
from plugins.conversation_search import (
    run as conv_run,
)
from plugins.notes_manager import run as notes_run


@pytest.fixture(autouse=True)
def clean_stores(tmp_path):
    with patch("actions.clipboard_manager._DB_PATH", tmp_path / "clip.db"), \
         patch("plugins.notes_manager._DB_PATH", tmp_path / "notes.db"), \
         patch("plugins.conversation_search._DB_PATH", tmp_path / "conv.db"):
        yield


# ── 1. Clipboard Manager Tests ───────────────────────────────────────────────

def test_clipboard_secret_masking():
    is_sec, masked = detect_and_mask_secrets("My OpenAI key is sk-1234567890abcdef1234567890 and normal text")
    assert is_sec is True
    assert "sk-1234567890abcdef1234567890" not in masked
    assert "[REDACTED_SECRET]" in masked

    is_gh, gh_masked = detect_and_mask_secrets("GitHub token: ghp_123456789012345678901234567890123456")
    assert is_gh is True
    assert "ghp_" not in gh_masked

    is_clean, clean_text = detect_and_mask_secrets("Just a regular message about Python unit testing.")
    assert is_clean is False
    assert clean_text == "Just a regular message about Python unit testing."


def test_clipboard_manager_lifecycle():
    with patch("actions.clipboard_manager.pyperclip") as mock_clip:
        mock_clip.paste.return_value = "Initial copied text"

        # Capture current clipboard
        res_cap = clipboard_manager({"action": "capture"})
        assert isinstance(res_cap, ActionResult)
        assert res_cap.status == "done"
        assert "Initial copied text" in res_cap.message

        # Copy new text to clipboard
        res_copy = clipboard_manager({"action": "copy", "text": "New pasted content"})
        assert res_copy.status == "done"
        mock_clip.copy.assert_called_with("New pasted content")

        # History
        res_hist = clipboard_manager({"action": "history"})
        assert res_hist.status == "done"
        assert len(res_hist.data["entries"]) >= 2

        # Search
        res_search = clipboard_manager({"action": "search", "query": "New pasted"})
        assert res_search.status == "done"
        assert len(res_search.data["matches"]) >= 1

        # Pin
        res_pin = clipboard_manager({"action": "pin", "text": "Crucial Code Snippet", "title": "MySnippet"})
        assert res_pin.status == "done"

        # Pinned list
        res_pinned = clipboard_manager({"action": "pinned"})
        assert res_pinned.status == "done"
        assert len(res_pinned.data["pinned"]) >= 1

        # Clear unpinned
        res_clear = clipboard_manager({"action": "clear"})
        assert res_clear.status == "done"


# ── 2. Notes Manager Tests ───────────────────────────────────────────────────

def test_notes_manager_crud_and_daily():
    mock_player = MagicMock()

    # Add note
    res_add = notes_run(
        {"action": "add", "title": "Project Alpha", "content": "Complete architecture draft", "tags": "work,arch"},
        player=mock_player,
    )
    assert "Saved note" in res_add
    assert "Project Alpha" in res_add

    # List notes
    res_list = notes_run({"action": "list"}, player=mock_player)
    assert "Project Alpha" in res_list

    # Search notes
    res_search = notes_run({"action": "search", "query": "architecture"}, player=mock_player)
    assert "Found 1 notes" in res_search

    # Daily note append & get
    res_d_add = notes_run({"action": "daily_append", "content": "Completed phase 2 planning"}, player=mock_player)
    assert "Appended entry" in res_d_add

    res_d_get = notes_run({"action": "daily_get"}, player=mock_player)
    assert "phase 2 planning" in res_d_get

    # Scratchpad
    res_sc_set = notes_run({"action": "scratchpad_set", "key": "port", "content": "8080"})
    assert "Updated scratchpad" in res_sc_set

    res_sc_get = notes_run({"action": "scratchpad_get", "key": "port"})
    assert "8080" in res_sc_get

    res_sc_clear = notes_run({"action": "scratchpad_clear"})
    assert "Cleared all" in res_sc_clear


# ── 3. Conversation Search Tests ─────────────────────────────────────────────

def test_conversation_search_fts_and_query():
    mock_player = MagicMock()

    # Record turns
    record_conversation_turn("sess-1", "user", "How do I configure the Windows audio endpoint?")
    record_conversation_turn("sess-1", "assistant", "You can adjust volume through pycaw or windows_control.")
    record_conversation_turn("sess-2", "user", "What is the capital of France?")
    record_conversation_turn("sess-2", "assistant", "The capital of France is Paris.")

    # Search by keyword
    res_audio = conv_run({"action": "search", "query": "audio endpoint"}, player=mock_player)
    assert "matching turns" in res_audio.lower()
    assert "Windows audio endpoint" in res_audio

    # Filter by role
    res_user = conv_run({"action": "search", "query": "France", "role": "user"}, player=mock_player)
    assert "capital of France" in res_user

    # List recent
    res_recent = conv_run({"action": "list", "limit": 5})
    assert "Retrieved" in res_recent
