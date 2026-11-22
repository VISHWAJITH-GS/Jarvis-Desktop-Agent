"""
actions/copy_page_text_to_clipboard.py — Direct "Copy Page/Chat Text to Clipboard" Action.

Extracts structured conversation turns or article text from the active browser tab
via CDP without UI noise or manual selection, copies the clean text to the system clipboard,
and verifies the copied content with paste-back read verification.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import pyperclip

from actions._chat_extractors import (
    extract_messages_from_page,
    format_conversation_text,
)
from actions.browser_session import get_browser_session_manager
from core import outcome

logger = logging.getLogger("copy_page_text_to_clipboard")


def copy_page_text_to_clipboard(parameters: Optional[Dict[str, Any]] = None, player=None, **kwargs: Any) -> str:
    """
    Extract clean conversation turns or article content from active browser tab and copy to clipboard.
    """
    params = dict(parameters or {})
    params.update(kwargs)

    selector = params.get("selector")
    if selector:
        selector = str(selector).strip() or None

    session_mgr = get_browser_session_manager()

    try:
        ok, err_msg = session_mgr.check_connection(timeout=2.0)
        if not ok and hasattr(session_mgr, "_try_auto_launch"):
            if session_mgr._try_auto_launch():
                ok = True
        if not ok:
            return outcome.failed("copy_page_text_to_clipboard", "Browser is not connected or remote debugging is not enabled")
    except Exception as e:
        return outcome.failed("copy_page_text_to_clipboard", f"Failed to check browser connection: {e}")

    try:
        session_mgr.run_coroutine(session_mgr._ensure_attached(), timeout=10.0)
    except Exception as e:
        return outcome.failed("copy_page_text_to_clipboard", f"Failed attaching to browser session: {e}")

    page = session_mgr.active_page
    if not page or page.is_closed():
        return outcome.failed("copy_page_text_to_clipboard", "No active browser tab found")

    try:
        title = session_mgr.run_coroutine(page.title(), timeout=5.0) or ""
    except Exception:
        title = ""

    # Extract messages / content via CDP
    try:
        messages = session_mgr.run_coroutine(
            extract_messages_from_page(page, selector=selector),
            timeout=25.0,
        )
    except Exception as e:
        logger.error(f"Error extracting DOM messages: {e}", exc_info=True)
        return outcome.failed("copy_page_text_to_clipboard", f"Error extracting page text: {e}", verdict="not_done")

    if not messages:
        return outcome.failed(
            "copy_page_text_to_clipboard",
            "No conversation messages or readable article content found on the active page.",
            verdict="not_done",
        )

    formatted_text = format_conversation_text(messages, title=title, format_type="txt")
    if not formatted_text.strip():
        return outcome.failed(
            "copy_page_text_to_clipboard",
            "Extracted content was empty after formatting.",
            verdict="not_done",
        )

    # Copy to clipboard
    try:
        pyperclip.copy(formatted_text)
    except Exception as e:
        return outcome.failed("copy_page_text_to_clipboard", f"Failed copying to clipboard: {e}", verdict="not_done")

    # Verify with paste back
    try:
        pasted = pyperclip.paste()
        if not pasted or len(pasted) < len(formatted_text) * 0.8:
            return outcome.failed(
                "copy_page_text_to_clipboard",
                "Clipboard verification failed after copy (pasted content did not match).",
                verdict="mismatch",
            )
    except Exception as e:
        logger.warning(f"Could not verify clipboard paste: {e}")

    preview = formatted_text[:50].replace("\r", " ").replace("\n", " ").strip()
    if player and hasattr(player, "write_log"):
        player.write_log(f"Copied {len(messages)} turns ({len(formatted_text)} chars) to clipboard")

    return outcome.done(
        "copy_page_text_to_clipboard",
        verdict="verified",
        char_count=len(formatted_text),
        message_count=len(messages),
        preview=preview,
    )


TOOL = {
    "name": "copy_page_text_to_clipboard",
    "description": (
        "Extracts conversation turns or article text from the active browser tab (e.g. ChatGPT, Claude, or webpage) "
        "and copies it directly to the system clipboard without UI button clutter or manual selection. "
        "Verifies clipboard content with paste back. Use when the user asks to copy page text, chat text, or copy conversation to clipboard."
    ),
    "brief": "Copy clean conversation or article text from active browser tab to clipboard.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "selector": {
                "type": "STRING",
                "description": "Optional CSS selector to isolate a specific container element.",
            },
        },
    },
    "handler": copy_page_text_to_clipboard,
    "tier": "LOW",
    "permission": "read_only",
}
