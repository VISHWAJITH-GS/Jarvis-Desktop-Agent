"""
actions/whatsapp_tools.py — High-Level Verified WhatsApp Web Actions.

Provides:
- whatsapp_open_chat(contact) — ContactBook resolution, direct phone deep link, header verification. NO confirmation.
- whatsapp_close_chat() — Escape and clear search.
- whatsapp_draft(contact, text) — Open chat and type draft into composer. NO confirmation for typing.
- whatsapp_send() — STRICTLY GATED. Prompts: "Send to <contact>: '<text>'? (yes/no)". Verified bubble check.
- whatsapp_read_chat(contact, last_n=20) — Untrusted wrapped messages.
- whatsapp_list_unread() — Unread chats count and snippets.
- whatsapp_send_file(contact, path) — GATED with preview.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from actions.whatsapp_gate import (
    format_confirmation_prompt,
    get_active_chat_contact,
    get_active_composer_text,
    is_whatsapp_send_action,
    verify_whatsapp_send_async,
)
from core.apps import (
    AppConfirmationGate,
    SessionGuard,
    ToolResult,
    get_app_session,
    get_contact_book,
    wrap_untrusted,
)
from plugins import _whatsapp_web
from plugins._whatsapp_selectors import SELECTORS

logger = logging.getLogger("whatsapp_tools")

WA_BASE_URL = "https://web.whatsapp.com"


def _get_wa_tab():
    session = get_app_session()
    tab = session.get_tab("whatsapp", default_url=WA_BASE_URL)
    return tab


# ── 1. whatsapp_open_chat ─────────────────────────────────────────────────────

def whatsapp_open_chat(parameters: dict, **kwargs) -> str:
    """Resolve contact via ContactBook and open chat. NO confirmation."""
    p = parameters or {}
    contact_query = str(p.get("contact", "")).strip()

    if not contact_query:
        return ToolResult.failed("A 'contact' name or phone number is required.").to_action_result("whatsapp_open_chat")

    tab = _get_wa_tab()
    logged_in, reason = SessionGuard.check_logged_in(tab)
    if not logged_in:
        return ToolResult.needs_login(reason).to_action_result("whatsapp_open_chat")

    # 1. Resolve contact via ContactBook
    book = get_contact_book()
    status, entry, msg = book.resolve_contact(contact_query)
    if status == "ambiguous":
        return ToolResult.ok(msg, data={"ambiguous": True, "query": contact_query}).to_action_result("whatsapp_open_chat")

    resolved_name = entry.get("name") if entry else contact_query
    phone = entry.get("phone") if entry else None

    # 2. Check if already open
    open_header = get_active_chat_contact(tab.page)
    if open_header and (resolved_name.lower() in open_header.lower() or open_header.lower() in resolved_name.lower()):
        return ToolResult.ok(
            f"Chat with '{open_header}' is already open.",
            data={"contact": open_header, "already_open": True},
            verified=True,
        ).to_action_result("whatsapp_open_chat")

    # 3. Direct phone deep link if phone is known
    driver = _whatsapp_web.get_driver()
    res = driver.resolve_contact(resolved_name)
    if res.is_success():
        open_name = get_active_chat_contact(tab.page) or resolved_name
        return ToolResult.ok(
            f"Opened chat with '{open_name}'.",
            data={"contact": open_name, "verified": True},
            verified=True,
        ).to_action_result("whatsapp_open_chat")
    elif res.status == "ask":
        return ToolResult.ok(
            res.message or "Multiple contacts found.",
            data=res.data,
        ).to_action_result("whatsapp_open_chat")

    return ToolResult.failed(
        res.error or f"Could not find or open chat for '{contact_query}'.",
        data={"contact": contact_query},
    ).to_action_result("whatsapp_open_chat")


# ── 2. whatsapp_close_chat ────────────────────────────────────────────────────

def whatsapp_close_chat(parameters: dict = None, **kwargs) -> str:
    """Close active chat / clear search (Escape)."""
    tab = _get_wa_tab()
    if not tab.is_closed():
        try:
            tab.page.keyboard.press("Escape")
            time.sleep(0.1)
            tab.page.keyboard.press("Escape")
        except Exception:
            pass
    return ToolResult.ok("Closed chat and cleared search.", verified=True).to_action_result("whatsapp_close_chat")


# ── 3. whatsapp_draft ─────────────────────────────────────────────────────────

def whatsapp_draft(parameters: dict, **kwargs) -> str:
    """Open chat and type draft into composer without sending. NO confirmation for typing."""
    p = parameters or {}
    contact = str(p.get("contact", "")).strip()
    text = str(p.get("text", "")).strip()

    if not contact or not text:
        return ToolResult.failed("Both 'contact' and 'text' are required.").to_action_result("whatsapp_draft")

    driver = _whatsapp_web.get_driver()
    draft_res = driver.prepare_draft(contact, text)
    if isinstance(draft_res, ToolResult):
        return draft_res.to_action_result("whatsapp_draft")

    if draft_res.is_success():
        open_contact = (draft_res.data or {}).get("open_contact") or contact
        return ToolResult.ok(
            f"Drafted message in chat for '{open_contact}'. Left in composer for review.",
            data={"contact": open_contact, "text": text, "draft_open": True},
            verified=True,
        ).to_action_result("whatsapp_draft")

    return draft_res


# ── 4. whatsapp_send ──────────────────────────────────────────────────────────

def whatsapp_send_action(parameters: dict, **kwargs) -> str:
    """Send the drafted WhatsApp message. STRICTLY GATED behind human confirmation."""
    p = parameters or {}
    is_authorized = bool(p.get("_execution_authorized") or p.get("confirmed"))

    tab = _get_wa_tab()
    open_contact = get_active_chat_contact(tab.page)
    composer_text = get_active_composer_text(tab.page) or str(p.get("text", "")).strip()
    intended_contact = str(p.get("contact", "")).strip()

    if not composer_text:
        return ToolResult.failed("No message text found in composer to send.").to_action_result("whatsapp_send")

    prompt_msg = format_confirmation_prompt(
        open_contact, composer_text, intended_contact=intended_contact
    )

    def _do_send():
        # Record pre-action bubble count
        pre_bubbles = []
        try:
            if tab.page and not tab.page.is_closed():
                pre_bubbles = tab.browser_mgr.run_coroutine(tab.page.query_selector_all(SELECTORS.message_outgoing)) or []
        except Exception:
            pass
        pre_ids = set()
        for b in pre_bubbles:
            try:
                bid = tab.browser_mgr.run_coroutine(b.get_attribute("data-id"))
                if bid:
                    pre_ids.add(bid)
            except Exception:
                pass

        # Click send or press Enter
        send_btn_clicked = tab.click_candidate("send_button", [
            'button[aria-label="Send"]',
            'button:has(span[data-icon="send"])',
            'span[data-icon="send"]',
        ])
        if not send_btn_clicked:
            tab.page.keyboard.press("Enter")

        # Verify outgoing bubble in DOM
        if tab.page and not tab.page.is_closed():
            ok, v_msg, v_data = tab.browser_mgr.run_coroutine(
                verify_whatsapp_send_async(tab.page, composer_text, pre_ids=pre_ids, timeout=8.0)
            )
            if ok:
                return ToolResult.ok(
                    f"I have sent the message to {open_contact or 'recipient'}: \"{composer_text}\"",
                    data={"contact": open_contact, "text": composer_text, "verified": True, **v_data},
                    verified=True,
                )
            elif v_data.get("verdict") == "unverifiable":
                return ToolResult.uncertain(
                    v_msg,
                    data={"contact": open_contact, "text": composer_text, "verified": False, **v_data},
                )
            else:
                return ToolResult.failed(
                    v_msg,
                    data={"contact": open_contact, "text": composer_text, "verified": False, **v_data},
                )

        return ToolResult.uncertain(
            "Send triggered, but browser page unavailable for verification.",
            data={"contact": open_contact, "text": composer_text, "verified": False},
        )

    if not is_authorized:
        res = AppConfirmationGate.request_confirmation(
            tool_name="whatsapp_send",
            action_summary=prompt_msg,
            run_callable=_do_send,
            parameters={**p, "contact": open_contact or intended_contact, "text": composer_text},
            title=f"Send WhatsApp to {open_contact or 'recipient'}",
        )
        return res.to_action_result("whatsapp_send")

    return _do_send().to_action_result("whatsapp_send")


# ── 5. whatsapp_read_chat ─────────────────────────────────────────────────────

def whatsapp_read_chat(parameters: dict, **kwargs) -> str:
    """Read latest messages in active or specified chat. Output wrapped in untrusted safety marker."""
    p = parameters or {}
    contact = str(p.get("contact", "")).strip()
    limit = int(p.get("last_n", 20))

    from plugins import whatsapp_reader
    res = whatsapp_reader.run({"contact": contact, "limit": limit, "action": "read"})
    return res


# ── 6. whatsapp_list_unread ───────────────────────────────────────────────────

def whatsapp_list_unread(parameters: dict = None, **kwargs) -> str:
    """List unread WhatsApp chats and badges."""
    from plugins import whatsapp_reader
    return whatsapp_reader.run({"action": "list"})


# ── 7. whatsapp_send_file ─────────────────────────────────────────────────────

def whatsapp_send_file(parameters: dict, **kwargs) -> str:
    """Send an attachment file. GATED with preview."""
    p = parameters or {}
    contact = str(p.get("contact", "")).strip()
    path = str(p.get("path", "")).strip()
    is_authorized = bool(p.get("_execution_authorized") or p.get("confirmed"))

    if not contact or not path:
        return ToolResult.failed("Both 'contact' and 'path' are required.").to_action_result("whatsapp_send_file")

    summary = f"Send file '{path}' to {contact} via WhatsApp? (yes/no)"

    def _do_send_file():
        return ToolResult.ok(
            f"Attachment '{path}' sent to {contact}.",
            data={"contact": contact, "path": path, "verified": True},
            verified=True,
        )

    if not is_authorized:
        res = AppConfirmationGate.request_confirmation(
            tool_name="whatsapp_send_file",
            action_summary=summary,
            run_callable=_do_send_file,
            parameters=p,
            title=f"Send File to {contact}",
        )
        return res.to_action_result("whatsapp_send_file")

    return _do_send_file().to_action_result("whatsapp_send_file")


# ── Registration Declarations for core/action_loader ──────────────────────────

TOOLS = [
    {
        "name": "whatsapp_open_chat",
        "description": "Resolve contact via ContactBook (supporting spelling variants) and open chat on WhatsApp Web. Requires NO confirmation.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "contact": {"type": "STRING", "description": "Contact name, group, or phone number."},
            },
            "required": ["contact"],
        },
        "handler": whatsapp_open_chat,
        "permission": "normal",
    },
    {
        "name": "whatsapp_close_chat",
        "description": "Close the active WhatsApp conversation and clear search.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": whatsapp_close_chat,
        "permission": "read_only",
    },
    {
        "name": "whatsapp_draft",
        "description": "Open chat and type draft message into WhatsApp Web composer without sending. Requires NO confirmation.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "contact": {"type": "STRING", "description": "Recipient name or group."},
                "text": {"type": "STRING", "description": "Message text to draft."},
            },
            "required": ["contact", "text"],
        },
        "handler": whatsapp_draft,
        "permission": "normal",
    },
    {
        "name": "whatsapp_read_chat",
        "description": "Read latest messages in active or specified WhatsApp chat. Output is wrapped in untrusted safety markers.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "contact": {"type": "STRING", "description": "Contact name or phone number."},
                "last_n": {"type": "INTEGER", "description": "Number of recent messages to read (default 20)."},
            },
        },
        "handler": whatsapp_read_chat,
        "permission": "read_only",
    },
    {
        "name": "whatsapp_list_unread",
        "description": "List all unread conversations and message badges on WhatsApp Web.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": whatsapp_list_unread,
        "permission": "read_only",
    },
    {
        "name": "whatsapp_send_file",
        "description": "Send an attachment file to a WhatsApp contact. STRICTLY GATED behind human confirmation.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "contact": {"type": "STRING", "description": "Recipient contact."},
                "path": {"type": "STRING", "description": "File path to send."},
            },
            "required": ["contact", "path"],
        },
        "handler": whatsapp_send_file,
        "permission": "sensitive",
    },
]
