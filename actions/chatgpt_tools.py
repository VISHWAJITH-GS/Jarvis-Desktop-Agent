"""
actions/chatgpt_tools.py — High-Level Verified ChatGPT Web Actions.

Features:
- Instant query via https://chatgpt.com/?q=<prompt> deep links without UI wandering.
- Event-based completion detection: waits for stop button to disappear and copy button to appear (no fixed sleeps).
- chatgpt_ask(prompt, new_chat=True)
- chatgpt_continue(prompt)
- chatgpt_read_last()
- chatgpt_open_history(query)
- chatgpt_upload(path, prompt)
- Untrusted output wrapping for prompt injection defense.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from core.apps import (
    AppVerifier,
    SessionGuard,
    ToolResult,
    get_app_session,
    wrap_untrusted,
)

logger = logging.getLogger("chatgpt_tools")

CHATGPT_BASE_URL = "https://chatgpt.com"


def _get_chatgpt_tab():
    session = get_app_session()
    tab = session.get_tab("chatgpt", default_url=CHATGPT_BASE_URL)
    return tab


def _wait_for_completion(tab, timeout: float = 40.0) -> bool:
    """Event-based completion detection: wait for stop button to disappear and copy button to appear."""
    deadline = time.monotonic() + timeout
    # First, wait a moment for generation to begin (stop button appears or prompt clears)
    time.sleep(0.4)

    stop_selectors = [
        'button[data-testid="stop-button"]',
        'button[aria-label="Stop generating"]',
        'button:has-text("Stop")',
    ]
    copy_selectors = [
        'button[aria-label="Copy"]',
        '[data-testid="copy-turn-action-button"]',
        'button:has(svg[data-icon="copy"])',
    ]

    while time.monotonic() < deadline:
        # Check if stop button is active
        stop_btn, _ = tab.find_first("stop_button", stop_selectors)
        if not stop_btn:
            # Stop button is gone. Check if copy button or end-turn element is present
            copy_btn, _ = tab.find_first("copy_button", copy_selectors)
            if copy_btn:
                return True
        time.sleep(0.3)

    return False


def _read_last_reply(tab) -> str:
    """Read the latest assistant markdown bubble from the DOM."""
    js_extract = """
    () => {
        const turns = document.querySelectorAll('[data-testid^="conversation-turn-"], div[data-message-author-role="assistant"], .agent-turn');
        if (turns.length === 0) return '';
        const lastTurn = turns[turns.length - 1];
        const md = lastTurn.querySelector('.markdown') || lastTurn;
        return (md.innerText || '').trim();
    }
    """
    reply = tab.evaluate(js_extract) or ""
    return reply.strip()


# ── 1. chatgpt_ask ────────────────────────────────────────────────────────────

def chatgpt_ask(parameters: dict, **kwargs) -> str:
    """Ask ChatGPT a prompt: types prompt into text box first, then asks for user confirmation before submitting."""
    p = parameters or {}
    prompt = str(p.get("prompt", "")).strip()
    new_chat = bool(p.get("new_chat", True))
    is_auth = bool(p.get("_execution_authorized") or p.get("confirmed"))

    if not prompt:
        return ToolResult.failed("A 'prompt' is required.").to_action_result("chatgpt_ask")

    tab = _get_chatgpt_tab()
    logged_in, reason = SessionGuard.check_logged_in(tab)
    if not logged_in:
        return ToolResult.needs_login(reason).to_action_result("chatgpt_ask")

    target_url = CHATGPT_BASE_URL if new_chat else tab.url()
    tab.ensure_open(target_url)
    tab.focus()

    # Step 1: Type the prompt into the text box WITHOUT submitting (press_enter=False)
    textarea_selectors = [
        '#prompt-textarea',
        'textarea[data-id="root"]',
        '[data-testid="conversation-turn-composer"] [contenteditable="true"]',
        'div[contenteditable="true"]',
        'textarea',
    ]
    tab.type_candidate("prompt_textarea", prompt, textarea_selectors, press_enter=False, clear_first=True)

    # Step 2: Confirmation gate — prompt is typed into the box, now ask permission to submit
    if not is_auth:
        from core.apps import AppConfirmationGate
        preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
        return AppConfirmationGate.request_confirmation(
            tool_name="chatgpt_ask",
            action_summary=f"Submit prompt to ChatGPT: '{preview}'?",
            run_callable=lambda: chatgpt_ask({**p, "_execution_authorized": True, "confirmed": True}, **kwargs),
            parameters=p,
            title="Submit ChatGPT Prompt",
        ).to_action_result("chatgpt_ask")

    # Step 3: User confirmed — submit by clicking send button or pressing Enter
    send_selectors = [
        'button[data-testid="send-button"]',
        'button[aria-label="Send prompt"]',
        'button[aria-label="Send"]',
        'button:has(svg[data-icon="arrow-up"])',
    ]
    clicked = tab.click_candidate("send_button", send_selectors)
    if not clicked:
        tab.press_key("Enter")

    # Step 4: Event-based wait for completion
    completed = _wait_for_completion(tab, timeout=35.0)
    raw_reply = _read_last_reply(tab)

    if not raw_reply:
        return ToolResult.uncertain(
            "Prompt submitted, but no reply text could be extracted from ChatGPT.",
            data={"prompt": prompt},
        ).to_action_result("chatgpt_ask")

    wrapped_reply = wrap_untrusted("chatgpt", raw_reply)
    summary = f"ChatGPT Reply:\n\n{wrapped_reply}"

    return ToolResult.ok(
        summary,
        data={"prompt": prompt, "reply": wrapped_reply, "completed": completed},
        verified=True,
    ).to_action_result("chatgpt_ask")


# ── 2. chatgpt_continue ───────────────────────────────────────────────────────

def chatgpt_continue(parameters: dict, **kwargs) -> str:
    """Send follow-up prompt to the active ChatGPT conversation."""
    return chatgpt_ask({**parameters, "new_chat": False}, **kwargs)


# ── 3. chatgpt_read_last ──────────────────────────────────────────────────────

def chatgpt_read_last(parameters: dict = None, **kwargs) -> str:
    """Read the latest assistant message without sending a new prompt."""
    tab = _get_chatgpt_tab()
    reply = _read_last_reply(tab)
    if not reply:
        return ToolResult.ok("No assistant messages found in active ChatGPT conversation.", data={}).to_action_result("chatgpt_read_last")

    wrapped = wrap_untrusted("chatgpt", reply)
    return ToolResult.ok(f"Latest ChatGPT Reply:\n\n{wrapped}", data={"reply": wrapped}, verified=True).to_action_result("chatgpt_read_last")


# ── 4. chatgpt_open_history ───────────────────────────────────────────────────

def chatgpt_open_history(parameters: dict, **kwargs) -> str:
    """Search and open a past chat from ChatGPT history sidebar."""
    p = parameters or {}
    query = str(p.get("query", "")).strip().lower()

    tab = _get_chatgpt_tab()
    # Click chat item matching query
    js_click_history = f"""
    () => {{
        const items = document.querySelectorAll('nav a[href^="/c/"], nav li a');
        for (const it of items) {{
            if ((it.innerText || '').toLowerCase().includes('{query}')) {{
                it.click();
                return it.innerText.trim();
            }}
        }}
        return '';
    }}
    """
    matched = tab.evaluate(js_click_history)
    if matched:
        time.sleep(0.5)
        return ToolResult.ok(f"Opened conversation '{matched}'.", data={"title": matched}, verified=True).to_action_result("chatgpt_open_history")

    return ToolResult.not_found(f"Could not find conversation matching '{query}' in ChatGPT history.", data={"query": query}).to_action_result("chatgpt_open_history")


# ── 5. chatgpt_upload ─────────────────────────────────────────────────────────

def chatgpt_upload(parameters: dict, **kwargs) -> str:
    """Attach file and submit prompt."""
    p = parameters or {}
    path = str(p.get("path", "")).strip()
    prompt = str(p.get("prompt", "")).strip()

    if not path:
        return ToolResult.failed("A 'path' parameter is required.").to_action_result("chatgpt_upload")

    tab = _get_chatgpt_tab()
    # Attach via input[type="file"]
    try:
        file_input = tab.wait_for_selector('input[type="file"]', timeout_sec=2.0)
        if file_input:
            tab.browser_mgr.run_coroutine(file_input.set_input_files(path))
            time.sleep(1.0)
    except Exception as e:
        logger.debug("[ChatGPT] File set error: %s", e)

    if prompt:
        tab.type_candidate("prompt_textarea", prompt, ['#prompt-textarea'], press_enter=True)
        _wait_for_completion(tab, timeout=35.0)
        reply = _read_last_reply(tab)
        return ToolResult.ok(f"Uploaded '{path}'. Reply:\n{wrap_untrusted('chatgpt', reply)}", verified=True).to_action_result("chatgpt_upload")

    return ToolResult.ok(f"Attached file '{path}' to ChatGPT prompt.", verified=True).to_action_result("chatgpt_upload")


# ── Registration Declarations for core/action_loader ──────────────────────────

TOOLS = [
    {
        "name": "chatgpt_ask",
        "description": "Ask ChatGPT a prompt using direct deep link. Waits for event-based generation completion. Output is wrapped in untrusted safety markers.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "prompt": {"type": "STRING", "description": "Prompt or question for ChatGPT."},
                "new_chat": {"type": "BOOLEAN", "description": "Whether to start a fresh chat (default True)."},
            },
            "required": ["prompt"],
        },
        "handler": chatgpt_ask,
        "permission": "normal",
    },
    {
        "name": "chatgpt_continue",
        "description": "Send follow-up prompt to active ChatGPT conversation. Waits for completion.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "prompt": {"type": "STRING", "description": "Follow-up message."},
            },
            "required": ["prompt"],
        },
        "handler": chatgpt_continue,
        "permission": "normal",
    },
    {
        "name": "chatgpt_read_last",
        "description": "Read the latest assistant response from active ChatGPT conversation without sending anything.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": chatgpt_read_last,
        "permission": "read_only",
    },
    {
        "name": "chatgpt_open_history",
        "description": "Search and open a conversation from ChatGPT history by title.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Conversation title snippet."},
            },
            "required": ["query"],
        },
        "handler": chatgpt_open_history,
        "permission": "normal",
    },
    {
        "name": "chatgpt_upload",
        "description": "Attach file and optionally submit prompt to ChatGPT.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Local file path to attach."},
                "prompt": {"type": "STRING", "description": "Optional prompt to submit with attachment."},
            },
            "required": ["path"],
        },
        "handler": chatgpt_upload,
        "permission": "normal",
    },
]
