"""
tests/test_page_to_file.py — Comprehensive tests for chat/page extraction actions.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pyperclip

from actions import _chat_extractors
from actions.page_to_file import page_to_file
from actions.copy_page_text_to_clipboard import copy_page_text_to_clipboard
from core.risk_tiers import RiskTier, get_action_tier
from core.permissions import ACTION_PERMISSIONS, READ_ONLY, NORMAL


def test_format_conversation_text_txt():
    messages = [
        {"role": "user", "author": "You", "text": "Explain quantum computing."},
        {"role": "assistant", "author": "ChatGPT", "text": "Quantum computing uses qubits."},
    ]
    formatted = _chat_extractors.format_conversation_text(messages, title="Physics Discussion", format_type="txt")
    assert "Physics Discussion" in formatted
    assert "You:\nExplain quantum computing." in formatted
    assert "ChatGPT:\nQuantum computing uses qubits." in formatted


def test_format_conversation_text_md():
    messages = [
        {"role": "user", "author": "You", "text": "Write a Python script."},
        {"role": "assistant", "author": "ChatGPT", "text": "```python\nprint('hello')\n```"},
    ]
    formatted = _chat_extractors.format_conversation_text(messages, title="Coding Session", format_type="md")
    assert "# Coding Session" in formatted
    assert "### You" in formatted
    assert "### ChatGPT" in formatted
    assert "```python" in formatted


def test_write_file_atomic_and_collision_suffix(tmp_path):
    dest_dir = tmp_path / "exports"
    content1 = "First version of chat content"
    path1 = _chat_extractors.write_file_atomic(dest_dir, "my_chat.txt", content1)
    assert path1.exists()
    assert path1.name == "my_chat.txt"
    assert path1.read_text(encoding="utf-8") == content1

    # Second write with same filename must NEVER overwrite: adds " (1)"
    content2 = "Second version of chat content"
    path2 = _chat_extractors.write_file_atomic(dest_dir, "my_chat.txt", content2)
    assert path2.exists()
    assert path2.name == "my_chat (1).txt"
    assert path2.read_text(encoding="utf-8") == content2

    # Third write
    content3 = "Third version of chat content"
    path3 = _chat_extractors.write_file_atomic(dest_dir, "my_chat.txt", content3)
    assert path3.exists()
    assert path3.name == "my_chat (2).txt"
    assert path3.read_text(encoding="utf-8") == content3


def test_verify_written_file(tmp_path):
    messages = [
        {"role": "user", "author": "You", "text": "Hello world!"},
        {"role": "assistant", "author": "ChatGPT", "text": "Greetings, how can I assist you today?"},
    ]
    f = tmp_path / "valid.txt"
    content = "You:\nHello world!\nChatGPT:\nGreetings, how can I assist you today?\n"
    f.write_text(content, encoding="utf-8")

    ok, msg = _chat_extractors.verify_written_file(f, messages, len(content))
    assert ok is True
    assert "Verified" in msg

    # Empty file
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    ok_empty, msg_empty = _chat_extractors.verify_written_file(empty, messages, len(content))
    assert ok_empty is False
    assert "0 bytes" in msg_empty

    # Missing file
    missing = tmp_path / "missing.txt"
    ok_missing, msg_missing = _chat_extractors.verify_written_file(missing, messages, len(content))
    assert ok_missing is False
    assert "not found" in msg_missing


def test_page_to_file_action(tmp_path):
    mock_mgr = MagicMock()
    mock_mgr.check_connection.return_value = (True, "Connected")
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_mgr.active_page = mock_page

    messages = [
        {"role": "user", "author": "You", "text": "Can you design a database schema?"},
        {"role": "assistant", "author": "ChatGPT", "text": "Here is an optimized relational schema."},
    ]

    def mock_run_coro(coro, timeout=30.0):
        if hasattr(coro, "close"):
            coro.close()
        return messages

    mock_mgr.run_coroutine.side_effect = mock_run_coro

    dest_folder = str(tmp_path / "chats")
    with patch("actions.page_to_file.get_browser_session_manager", return_value=mock_mgr):
        res = page_to_file({
            "source": "active_tab",
            "filename": "schema_chat.txt",
            "folder": dest_folder,
            "format": "txt",
        })

    assert "status=done" in res
    assert "verdict=verified" in res
    assert "schema_chat.txt" in res
    exported = Path(dest_folder) / "schema_chat.txt"
    assert exported.exists()
    text = exported.read_text(encoding="utf-8")
    assert "Can you design a database schema?" in text
    assert "Here is an optimized relational schema." in text


def test_copy_page_text_to_clipboard_action():
    mock_mgr = MagicMock()
    mock_mgr.check_connection.return_value = (True, "Connected")
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_mgr.active_page = mock_page

    messages = [
        {"role": "user", "author": "You", "text": "Clipboard test turn one"},
        {"role": "assistant", "author": "ChatGPT", "text": "Clipboard response turn two"},
    ]

    def mock_run_coro(coro, timeout=30.0):
        if hasattr(coro, "close"):
            coro.close()
        return messages

    mock_mgr.run_coroutine.side_effect = mock_run_coro

    with patch("actions.copy_page_text_to_clipboard.get_browser_session_manager", return_value=mock_mgr):
        res = copy_page_text_to_clipboard({})

    assert "status=done" in res
    assert "verdict=verified" in res
    assert "char_count=" in res
    # Verify clipboard content
    pasted = pyperclip.paste()
    assert "Clipboard test turn one" in pasted
    assert "Clipboard response turn two" in pasted


def test_page_to_file_tier_and_permission():
    tier = get_action_tier("page_to_file", {"filename": "test.txt"})
    assert tier == RiskTier.LOW

    clip_tier = get_action_tier("copy_page_text_to_clipboard", {})
    assert clip_tier == RiskTier.LOW

    assert ACTION_PERMISSIONS.get("page_to_file") == NORMAL
    assert ACTION_PERMISSIONS.get("copy_page_text_to_clipboard") == READ_ONLY
