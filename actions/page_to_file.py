"""
actions/page_to_file.py — Direct "Save This Page/Chat as a File" Action.

Extracts structured conversation turns or article text from the active browser tab
(ChatGPT, Claude, or generic webpage) via CDP without UI noise or manual selection,
formats turns clearly, and writes atomically to disk (default Desktop) without overwriting.
Verifies file on disk and optionally opens it in Notepad if requested.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from actions._chat_extractors import (
    extract_messages_from_page,
    format_conversation_text,
    verify_written_file,
    write_file_atomic,
)
from actions.browser_session import get_browser_session_manager
from core import outcome

logger = logging.getLogger("page_to_file")


def page_to_file(parameters: Optional[Dict[str, Any]] = None, player=None, **kwargs: Any) -> str:
    """
    Extract conversation turns or article content from active browser tab and save to file.
    """
    params = dict(parameters or {})
    params.update(kwargs)

    source = str(params.get("source") or "active_tab").strip()
    selector = params.get("selector")
    if selector:
        selector = str(selector).strip() or None

    goal_text = str(params.get("goal") or "").lower()
    open_file = bool(params.get("open_file") or params.get("open") or "open" in goal_text)

    format_type = str(params.get("format") or "").strip().lower()
    if format_type not in ("txt", "md"):
        # Infer from filename or default to txt
        filename_hint = str(params.get("filename") or "").lower()
        format_type = "md" if filename_hint.endswith(".md") else "txt"

    session_mgr = get_browser_session_manager()

    # Guard: verify CDP browser connection
    try:
        ok, err_msg = session_mgr.check_connection(timeout=2.0)
        if not ok and hasattr(session_mgr, "_try_auto_launch"):
            if session_mgr._try_auto_launch():
                ok = True
        if not ok:
            return outcome.failed("page_to_file", "Browser is not connected or remote debugging is not enabled")
    except Exception as e:
        return outcome.failed("page_to_file", f"Failed to check browser connection: {e}")

    try:
        session_mgr.run_coroutine(session_mgr._ensure_attached(), timeout=10.0)
    except Exception as e:
        return outcome.failed("page_to_file", f"Failed attaching to browser session: {e}")

    page = session_mgr.active_page
    if not page or page.is_closed():
        return outcome.failed("page_to_file", "No active browser tab found")

    active_url = (getattr(page, "url", "") or "").lower()
    is_chatgpt_active = "chatgpt.com" in active_url or "chat.openai.com" in active_url
    goal_mentions_chatgpt = any(w in goal_text for w in ("chatgpt", "openai", "gpt"))

    if goal_mentions_chatgpt and not is_chatgpt_active:
        tabs = session_mgr.list_tabs()
        chatgpt_tab = next(
            (t for t in tabs if "chatgpt.com" in (t.url or "").lower() or "chat.openai.com" in (t.url or "").lower()),
            None,
        )
        if chatgpt_tab:
            logger.info(f"Switching to existing ChatGPT tab [{chatgpt_tab.index}] '{chatgpt_tab.title}'")
            session_mgr.switch_tab(chatgpt_tab.index)
            page = session_mgr.active_page
            is_chatgpt_active = True
        else:
            logger.info("ChatGPT not open in any tab. Opening https://chatgpt.com")
            session_mgr.open_tab("https://chatgpt.com")
            return outcome.ask(
                "page_to_file",
                "Opened ChatGPT in a new tab. Which chat would you like me to export?",
            )

    try:
        raw_title = session_mgr.run_coroutine(page.title(), timeout=5.0)
        title = str(raw_title or "Page Export")
        if not isinstance(raw_title, str):
            title = "Page Export"
    except Exception:
        title = "Page Export"

    # Extract messages / content via CDP
    try:
        messages = session_mgr.run_coroutine(
            extract_messages_from_page(page, selector=selector),
            timeout=25.0,
        )
    except Exception as e:
        logger.error(f"Error extracting DOM messages: {e}", exc_info=True)
        return outcome.failed("page_to_file", f"Error extracting page text: {e}", verdict="not_done")

    if not messages:
        if is_chatgpt_active:
            return outcome.ask(
                "page_to_file",
                "ChatGPT is open, but no active chat messages were found. Please select or open the conversation you would like me to export.",
            )
        return outcome.failed(
            "page_to_file",
            "No conversation messages or readable article content found on the active page.",
            verdict="not_done",
        )

    formatted_text = format_conversation_text(messages, title=title, format_type=format_type)
    if not formatted_text.strip():
        return outcome.failed(
            "page_to_file",
            "Extracted content was empty after formatting.",
            verdict="not_done",
        )

    # Resolve target folder (defaults to user's Desktop)
    dest_path_param = params.get("destination_path")
    folder_param = params.get("folder")
    filename_param = params.get("filename")

    if dest_path_param and not folder_param and not filename_param:
        p_obj = Path(dest_path_param).expanduser()
        dest_folder = p_obj.parent.resolve()
        filename_param = p_obj.name
    elif folder_param:
        dest_folder = Path(folder_param).expanduser().resolve()
    else:
        dest_folder = (Path.home() / "Desktop").resolve()
    if not filename_param:
        safe_title = re.sub(r'[<>:"/\\|?*\s]+', '_', title.strip())[:45].strip("_")
        filename_param = f"{safe_title or 'chat_export'}.{format_type}"
    else:
        filename_param = str(filename_param).strip()
        if not Path(filename_param).suffix:
            filename_param = f"{filename_param}.{format_type}"

    # Atomic write avoiding overwrite
    try:
        written_path = write_file_atomic(dest_folder, filename_param, formatted_text)
    except Exception as e:
        return outcome.failed("page_to_file", f"Failed writing file to disk: {e}", verdict="not_done")

    # Post-action verification: read back from disk
    is_valid, v_msg = verify_written_file(written_path, messages, len(formatted_text))
    if not is_valid:
        return outcome.failed("page_to_file", v_msg, verdict="not_done")

    # Open file if requested
    if open_file:
        try:
            os.startfile(str(written_path))
        except Exception as e:
            logger.warning(f"Could not open file in default editor: {e}")
            try:
                import subprocess
                subprocess.Popen(["notepad.exe", str(written_path)])
            except Exception as pe:
                logger.warning(f"Failed to launch notepad: {pe}")

    if player and hasattr(player, "write_log"):
        player.write_log(f"Saved {len(messages)} turns to {written_path.name}")

    return outcome.done(
        "page_to_file",
        verdict="verified",
        path=str(written_path),
        bytes=written_path.stat().st_size,
        message_count=len(messages),
        opened=open_file,
    )


TOOL = {
    "name": "page_to_file",
    "description": (
        "Extracts conversation turns or article text from the active browser tab (e.g. ChatGPT, Claude, or webpage) "
        "and saves it cleanly directly to a file on disk (default Desktop). Strips UI buttons, badges and controls, "
        "preserves markdown code fences and structure, and writes atomically without overwriting existing files. "
        "Use this tool whenever the user asks to save, copy, export, or write a chat, conversation, or page to a file or Notepad."
    ),
    "brief": "Save active browser page or chat transcript cleanly to a file.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "source": {
                "type": "STRING",
                "description": "Source of text to extract ('active_tab' by default).",
            },
            "selector": {
                "type": "STRING",
                "description": "Optional CSS selector to isolate a specific container element.",
            },
            "destination_path": {
                "type": "STRING",
                "description": "Optional full destination file path (e.g. 'C:/Users/User/Desktop/chat.txt'). If provided, folder and filename are derived from it.",
            },
            "filename": {
                "type": "STRING",
                "description": "Name of the target file (e.g. 'chatgpt_discussion.txt' or 'notes.md'). If omitted, derived from page title.",
            },
            "folder": {
                "type": "STRING",
                "description": "Target folder path (defaults to user's Desktop).",
            },
            "format": {
                "type": "STRING",
                "description": "Output format: 'txt' (default) or 'md'.",
            },
            "open_file": {
                "type": "BOOLEAN",
                "description": "Whether to immediately open the exported file in the default text editor (Notepad). Default False.",
            },
        },
    },
    "handler": page_to_file,
    "tier": "LOW",
    "permission": "normal",
}
