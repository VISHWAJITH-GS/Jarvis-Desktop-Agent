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


def test_page_to_file_switches_to_existing_chatgpt_tab(tmp_path):
    mock_mgr = MagicMock()
    mock_mgr.check_connection.return_value = (True, "Connected")

    # Current tab is generic/other
    tab0 = MagicMock()
    tab0.is_closed.return_value = False
    tab0.url = "https://google.com"

    tab1 = MagicMock()
    tab1.is_closed.return_value = False
    tab1.url = "https://chatgpt.com/c/123"

    mock_mgr.active_page = tab0

    from actions.browser_session import TabInfo
    mock_mgr.list_tabs.return_value = [
        TabInfo(index=0, title="Google", url="https://google.com", is_active=True),
        TabInfo(index=1, title="ChatGPT Discussion", url="https://chatgpt.com/c/123", is_active=False),
    ]

    def mock_switch(idx):
        mock_mgr.active_page = tab1
        return TabInfo(index=1, title="ChatGPT Discussion", url="https://chatgpt.com/c/123", is_active=True)

    mock_mgr.switch_tab.side_effect = mock_switch

    messages = [
        {"role": "user", "author": "You", "text": "What is Python?"},
        {"role": "assistant", "author": "ChatGPT", "text": "Python is a programming language."},
    ]

    def mock_run_coro(coro, timeout=30.0):
        if hasattr(coro, "close"):
            coro.close()
        return "ChatGPT Discussion"

    # Separate mock for extract_messages_from_page
    with patch("actions.page_to_file.extract_messages_from_page", return_value=None):
        pass

    def mock_run_coro_smart(coro, timeout=30.0):
        if hasattr(coro, "close"):
            coro.close()
        # If it's a coroutine from title() -> string, if extract_messages -> list
        if getattr(coro, "__name__", "") == "extract_messages_from_page" or "extract" in str(coro):
            return messages
        if isinstance(coro, str):
            return coro
        return messages

    mock_mgr.run_coroutine.side_effect = mock_run_coro_smart

    dest_folder = str(tmp_path / "chats")
    with patch("actions.page_to_file.get_browser_session_manager", return_value=mock_mgr):
        res = page_to_file({
            "goal": "copy this chatgpt conversation into a file",
            "folder": dest_folder,
        })

    # Assert switched to tab 1
    mock_mgr.switch_tab.assert_called_with(1)
    assert "status=done" in res
    assert "verdict=verified" in res


def test_page_to_file_opens_chatgpt_and_asks_when_not_open():
    mock_mgr = MagicMock()
    mock_mgr.check_connection.return_value = (True, "Connected")

    page = MagicMock()
    page.is_closed.return_value = False
    page.url = "https://github.com"
    mock_mgr.active_page = page

    from actions.browser_session import TabInfo
    mock_mgr.list_tabs.return_value = [
        TabInfo(index=0, title="GitHub", url="https://github.com", is_active=True),
    ]

    with patch("actions.page_to_file.get_browser_session_manager", return_value=mock_mgr):
        res = page_to_file({
            "goal": "save this chatgpt chat to file",
        })

    mock_mgr.open_tab.assert_called_with("https://chatgpt.com")
    assert "status=ask" in res
    assert "Which chat would you like me to export?" in res


def test_browser_open_tab_reuses_existing_tab():
    import asyncio
    from actions.browser_session import BrowserSessionManager, TabInfo

    async def _run():
        mgr = BrowserSessionManager.__new__(BrowserSessionManager)
        mgr._ensure_attached = MagicMock()

        async def mock_ensure():
            pass
        mgr._ensure_attached.side_effect = mock_ensure

        p1 = MagicMock()
        p1.is_closed.return_value = False
        p1.url = "https://chatgpt.com/c/my-topic"
        async def bring(): pass
        async def p1_title(): return "My Topic"
        p1.bring_to_front.side_effect = bring
        p1.title.side_effect = p1_title

        mock_context = MagicMock()
        mock_context.pages = [p1]
        mgr._context = mock_context
        mgr._active_page = None
        mgr._cdp_session = None

        # Calling _open_tab with the same origin/path should reuse p1
        tab = await mgr._open_tab("https://chatgpt.com/c/my-topic")
        assert tab.index == 0
        assert tab.url == "https://chatgpt.com/c/my-topic"
        assert mgr._active_page == p1
        mock_context.new_page.assert_not_called()

    asyncio.run(_run())


def test_browser_control_open_link_in_new_tab():
    from actions.browser_control import browser_control
    from actions.browser_session import TabInfo

    mock_mgr = MagicMock()
    mock_mgr.check_connection.return_value = (True, "Connected")
    mock_executor = MagicMock()

    def mock_run_coro(coro, timeout=30.0):
        if hasattr(coro, "close"):
            coro.close()
        return "Opened link in tab [1] 'ChatGPT' (https://chatgpt.com/c/456)."

    mock_mgr.run_coroutine.side_effect = mock_run_coro

    with patch("actions.browser_control.get_browser_session_manager", return_value=mock_mgr), \
         patch("actions.browser_control.get_action_executor", return_value=mock_executor):
        res = browser_control({"action": "open_link_in_new_tab", "index": 4})

    assert "status=done" in res
    assert "open_link_in_new_tab" in res
    assert "index=4" in res
