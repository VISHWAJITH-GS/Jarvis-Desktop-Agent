"""
actions/gmail_tools.py — High-Level Verified Gmail Automation Actions.

Replaces slow multi-step browser perception/click/type wandering with:
- Deep links for instant direct navigation (#search, ?view=cm)
- Zero UI wandering
- High-speed DOM reads (10-50ms)
- Confirmation gate strictly on send and delete
- Untrusted output wrapping for prompt injection defense
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from core.apps import (
    AppConfirmationGate,
    AppVerifier,
    SessionGuard,
    ToolResult,
    get_app_session,
    wrap_untrusted,
)

logger = logging.getLogger("gmail_tools")

GMAIL_BASE_URL = "https://mail.google.com/mail/u/0"


def _get_gmail_tab():
    session = get_app_session()
    tab = session.get_tab("gmail", default_url=GMAIL_BASE_URL)
    return tab


# ── 1. gmail_search ───────────────────────────────────────────────────────────

def gmail_search(parameters: dict, **kwargs) -> str:
    """Search Gmail using deep links (#search/<query>) without UI wandering."""
    p = parameters or {}
    query = str(p.get("query", "")).strip()
    max_results = min(int(p.get("max", 10)), 25)

    if not query:
        return ToolResult.failed("A search query is required.").to_action_result("gmail_search")

    tab = _get_gmail_tab()
    logged_in, reason = SessionGuard.check_logged_in(tab)
    if not logged_in:
        return ToolResult.needs_login(reason).to_action_result("gmail_search")

    deep_link = f"{GMAIL_BASE_URL}/#search/{urllib.parse.quote_plus(query)}"
    tab.goto(deep_link)
    tab.wait_for_selector('tr.zA, div[role="main"]', timeout_sec=6.0)

    # Extract thread summaries directly from DOM table
    js_extract = """
    () => {
        const rows = document.querySelectorAll('tr.zA');
        const results = [];
        for (let i = 0; i < rows.length; i++) {
            const r = rows[i];
            const senderEl = r.querySelector('span[email], span.zF, span.bA');
            const subjectEl = r.querySelector('span.bog, span[data-thread-id]');
            const snippetEl = r.querySelector('span.y2');
            const dateEl = r.querySelector('td.xW span');
            const threadId = r.getAttribute('data-thread-id') || r.id || '';

            results.push({
                thread_id: threadId,
                sender: senderEl ? (senderEl.innerText || senderEl.getAttribute('email') || '').trim() : '',
                subject: subjectEl ? (subjectEl.innerText || '').trim() : '',
                snippet: snippetEl ? (snippetEl.innerText || '').trim() : '',
                date: dateEl ? (dateEl.innerText || '').trim() : '',
            });
        }
        return results;
    }
    """
    raw_threads = tab.evaluate(js_extract) or []
    threads = raw_threads[:max_results]

    if not threads:
        return ToolResult.ok(
            f"No emails found matching query '{query}'.",
            data={"query": query, "count": 0, "threads": []},
        ).to_action_result("gmail_search")

    summary_lines = [f"Found {len(threads)} emails for '{query}':"]
    for idx, t in enumerate(threads, 1):
        summary_lines.append(
            f"[{idx}] From: {t['sender']} | Subj: {t['subject']} | Date: {t['date']} (ID: {t['thread_id']})"
        )

    return ToolResult.ok(
        "\n".join(summary_lines),
        data={"query": query, "count": len(threads), "threads": threads},
        verified=True,
    ).to_action_result("gmail_search")


# ── 2. gmail_read ─────────────────────────────────────────────────────────────

def gmail_read(parameters: dict, **kwargs) -> str:
    """Read full email body and attachment names by thread_id with untrusted wrapper."""
    p = parameters or {}
    thread_id = str(p.get("thread_id", "")).strip()

    if not thread_id:
        return ToolResult.failed("A 'thread_id' parameter is required.").to_action_result("gmail_read")

    tab = _get_gmail_tab()
    logged_in, reason = SessionGuard.check_logged_in(tab)
    if not logged_in:
        return ToolResult.needs_login(reason).to_action_result("gmail_read")

    deep_link = f"{GMAIL_BASE_URL}/#all/{thread_id}"
    tab.goto(deep_link)
    tab.wait_for_selector('div.a3s, div[role="listitem"]', timeout_sec=6.0)

    js_extract_body = """
    () => {
        const bodies = document.querySelectorAll('div.a3s.aiL, div.a3s');
        const bodyText = bodies.length > 0 ? bodies[bodies.length - 1].innerText : '';
        const subjEl = document.querySelector('h2.hP');
        const senderEl = document.querySelector('span.gD');
        const attachEls = document.querySelectorAll('span.aV3, span[download_url]');
        const attachments = [];
        for (const a of attachEls) {
            attachments.push((a.innerText || a.getAttribute('download_url') || '').trim());
        }
        return {
            subject: subjEl ? subjEl.innerText : '',
            sender: senderEl ? (senderEl.getAttribute('email') || senderEl.innerText) : '',
            body: bodyText,
            attachments: attachments
        };
    }
    """
    res = tab.evaluate(js_extract_body) or {}
    body_text = res.get("body", "")
    wrapped_body = wrap_untrusted(f"gmail_thread_{thread_id}", body_text)

    summary = (
        f"Email from {res.get('sender', 'Unknown')} | Subject: '{res.get('subject', '')}'\n"
        f"Attachments: {', '.join(res.get('attachments', [])) or 'None'}\n\n"
        f"{wrapped_body}"
    )

    return ToolResult.ok(
        summary,
        data={
            "thread_id": thread_id,
            "sender": res.get("sender"),
            "subject": res.get("subject"),
            "body": wrapped_body,
            "attachments": res.get("attachments", []),
        },
        verified=True,
    ).to_action_result("gmail_read")


# ── 3. gmail_draft ────────────────────────────────────────────────────────────

def gmail_draft(parameters: dict, **kwargs) -> str:
    """Draft an email using deep link compose window. Leaves draft open. NO confirmation."""
    p = parameters or {}
    to = str(p.get("to", "")).strip()
    subject = str(p.get("subject", "")).strip()
    body = str(p.get("body", "")).strip()
    cc = str(p.get("cc", "")).strip()

    if not to and not subject and not body:
        return ToolResult.failed("Specify at least recipient ('to'), 'subject', or 'body'.").to_action_result("gmail_draft")

    tab = _get_gmail_tab()
    logged_in, reason = SessionGuard.check_logged_in(tab)
    if not logged_in:
        return ToolResult.needs_login(reason).to_action_result("gmail_draft")

    # Compose deep link opens full-screen or popup composer with prefilled fields
    compose_link = (
        f"{GMAIL_BASE_URL}/?view=cm&fs=1"
        f"&to={urllib.parse.quote_plus(to)}"
        f"&su={urllib.parse.quote_plus(subject)}"
        f"&body={urllib.parse.quote_plus(body)}"
    )
    if cc:
        compose_link += f"&cc={urllib.parse.quote_plus(cc)}"

    tab.goto(compose_link)
    tab.wait_for_selector('div[role="textbox"], input[name="subjectbox"]', timeout_sec=5.0)

    summary = f"Drafted email to {to or 'recipient'} with subject '{subject}'. Left open in composer for review."
    return ToolResult.ok(
        summary,
        data={"to": to, "subject": subject, "body": body, "cc": cc, "draft_open": True},
        verified=True,
    ).to_action_result("gmail_draft")


# ── 4. gmail_send ─────────────────────────────────────────────────────────────

def gmail_send(parameters: dict, **kwargs) -> str:
    """Send drafted email. STRICTLY GATED behind human confirmation."""
    p = parameters or {}
    is_authorized = bool(p.get("_execution_authorized") or p.get("confirmed"))

    tab = _get_gmail_tab()
    logged_in, reason = SessionGuard.check_logged_in(tab)
    if not logged_in:
        return ToolResult.needs_login(reason).to_action_result("gmail_send")

    # Read active draft fields from the open composer
    js_read_compose = """
    () => {
        const toEl = document.querySelector('input[name="to"], textarea[name="to"], span[email]');
        const subjEl = document.querySelector('input[name="subjectbox"]');
        const bodyEl = document.querySelector('div[role="textbox"][aria-label*="Message"]');
        return {
            to: toEl ? (toEl.value || toEl.innerText || toEl.getAttribute('email') || '') : '',
            subject: subjEl ? subjEl.value : '',
            body_preview: bodyEl ? (bodyEl.innerText || '').slice(0, 100) : ''
        };
    }
    """
    compose_info = tab.evaluate(js_read_compose) or {}
    to = compose_info.get("to") or p.get("to") or "recipient"
    subject = compose_info.get("subject") or p.get("subject") or "(No Subject)"
    preview = compose_info.get("body_preview") or p.get("body", "")[:60]

    action_summary = f"Send email to {to} with subject '{subject}' (Preview: '{preview}')? (yes/no)"

    def _do_send():
        # Click the send button (Ctrl+Enter or click Send)
        send_candidates = [
            'div[role="button"][data-tooltip*="Send"]',
            'div[role="button"][aria-label*="Send"]',
            'div[role="button"]:has-text("Send")',
        ]
        ok = tab.click_candidate("send_button", send_candidates)
        if not ok:
            tab.page.keyboard.press("Control+Enter")

        # Verify via "Message sent" toast
        verified, v_msg = AppVerifier.verify_dom_toast(
            tab,
            toast_selector='span.bAq, div[role="alert"], div:has-text("Message sent")',
            expected_snippet="Message sent",
            timeout=5.0,
        )
        if verified:
            return ToolResult.ok(
                f"Email sent successfully to {to}: '{subject}'.",
                data={"to": to, "subject": subject, "verified": True},
                verified=True,
            )
        return ToolResult.uncertain(
            f"Send button clicked for {to}, but 'Message sent' toast could not be confirmed in DOM.",
            data={"to": to, "subject": subject, "verified": False},
        )

    if not is_authorized:
        res = AppConfirmationGate.request_confirmation(
            tool_name="gmail_send",
            action_summary=action_summary,
            run_callable=_do_send,
            parameters=p,
            title=f"Send Email to {to}",
        )
        return res.to_action_result("gmail_send")

    return _do_send().to_action_result("gmail_send")


# ── 5. gmail_reply ────────────────────────────────────────────────────────────

def gmail_reply(parameters: dict, **kwargs) -> str:
    """Reply to an existing email thread. Typing is allowed; send is GATED."""
    p = parameters or {}
    thread_id = str(p.get("thread_id", "")).strip()
    body = str(p.get("body", "")).strip()
    is_authorized = bool(p.get("_execution_authorized") or p.get("confirmed"))

    if not thread_id or not body:
        return ToolResult.failed("Both 'thread_id' and 'body' are required.").to_action_result("gmail_reply")

    tab = _get_gmail_tab()
    logged_in, reason = SessionGuard.check_logged_in(tab)
    if not logged_in:
        return ToolResult.needs_login(reason).to_action_result("gmail_reply")

    # Navigate to thread
    deep_link = f"{GMAIL_BASE_URL}/#all/{thread_id}"
    tab.goto(deep_link)
    tab.wait_for_selector('span[role="link"]:has-text("Reply"), div[role="button"]:has-text("Reply")', timeout_sec=5.0)

    # Click reply box
    reply_clicked = tab.click_candidate("reply_button", [
        'span[role="link"]:has-text("Reply")',
        'div[role="button"]:has-text("Reply")',
        'div[aria-label="Reply"]',
    ])
    if reply_clicked:
        time.sleep(0.3)
        tab.type_candidate("reply_body", body, ['div[role="textbox"][aria-label*="Message Body"]', 'div[role="textbox"]'], clear_first=False)

    action_summary = f"Send reply to thread {thread_id} with text: '{body[:60]}'? (yes/no)"

    def _do_reply_send():
        tab.click_candidate("send_button", [
            'div[role="button"][data-tooltip*="Send"]',
            'div[role="button"][aria-label*="Send"]',
        ])
        verified, _ = AppVerifier.verify_dom_toast(
            tab,
            toast_selector='span.bAq, div:has-text("Message sent")',
            expected_snippet="Message sent",
            timeout=5.0,
        )
        if verified:
            return ToolResult.ok(
                f"Reply sent to thread {thread_id}.",
                data={"thread_id": thread_id, "verified": True},
                verified=True,
            )
        return ToolResult.uncertain(
            f"Reply triggered for thread {thread_id}, but delivery toast unconfirmed.",
            data={"thread_id": thread_id, "verified": False},
        )

    if not is_authorized:
        res = AppConfirmationGate.request_confirmation(
            tool_name="gmail_reply",
            action_summary=action_summary,
            run_callable=_do_reply_send,
            parameters=p,
            title=f"Send Reply to Thread {thread_id}",
        )
        return res.to_action_result("gmail_reply")

    return _do_reply_send().to_action_result("gmail_reply")


# ── 6. gmail_manage ───────────────────────────────────────────────────────────

def gmail_manage(parameters: dict, **kwargs) -> str:
    """Manage thread: archive, mark_read, mark_unread, star. No confirmation."""
    p = parameters or {}
    thread_id = str(p.get("thread_id", "")).strip()
    action = str(p.get("action", "")).strip().lower()

    if not thread_id or not action:
        return ToolResult.failed("Both 'thread_id' and 'action' ('archive', 'star', 'read', 'unread') required.").to_action_result("gmail_manage")

    tab = _get_gmail_tab()
    deep_link = f"{GMAIL_BASE_URL}/#all/{thread_id}"
    tab.goto(deep_link)
    tab.wait_for_selector('div[role="main"]', timeout_sec=5.0)

    if action in ("archive", "e"):
        tab.page.keyboard.press("e")
        summary = f"Archived thread {thread_id}."
    elif action in ("star", "s"):
        tab.page.keyboard.press("s")
        summary = f"Starred thread {thread_id}."
    elif action in ("unread", "mark_unread", "_"):
        tab.page.keyboard.press("_")
        summary = f"Marked thread {thread_id} as unread."
    elif action in ("read", "mark_read", "shift+i"):
        tab.page.keyboard.press("Shift+I")
        summary = f"Marked thread {thread_id} as read."
    else:
        return ToolResult.failed(f"Unknown manage action '{action}'.").to_action_result("gmail_manage")

    return ToolResult.ok(summary, data={"thread_id": thread_id, "action": action}, verified=True).to_action_result("gmail_manage")


# ── 7. gmail_delete ───────────────────────────────────────────────────────────

def gmail_delete(parameters: dict, **kwargs) -> str:
    """Delete / Move thread to trash. STRICTLY GATED."""
    p = parameters or {}
    thread_id = str(p.get("thread_id", "")).strip()
    is_authorized = bool(p.get("_execution_authorized") or p.get("confirmed"))

    if not thread_id:
        return ToolResult.failed("'thread_id' is required.").to_action_result("gmail_delete")

    tab = _get_gmail_tab()
    action_summary = f"Move email thread {thread_id} to Trash? (yes/no)"

    def _do_delete():
        deep_link = f"{GMAIL_BASE_URL}/#all/{thread_id}"
        tab.goto(deep_link)
        tab.wait_for_selector('div[role="main"]', timeout_sec=5.0)
        # Press # (Gmail delete shortcut) or click trash button
        tab.page.keyboard.press("#")
        verified, _ = AppVerifier.verify_dom_toast(
            tab,
            toast_selector='span.bAq, div:has-text("Conversation moved to Trash")',
            expected_snippet="Trash",
            timeout=4.0,
        )
        if verified:
            return ToolResult.ok(f"Moved thread {thread_id} to Trash.", data={"thread_id": thread_id, "deleted": True}, verified=True)
        return ToolResult.uncertain(f"Delete triggered for thread {thread_id}.", data={"thread_id": thread_id, "deleted": True})

    if not is_authorized:
        res = AppConfirmationGate.request_confirmation(
            tool_name="gmail_delete",
            action_summary=action_summary,
            run_callable=_do_delete,
            parameters=p,
            title=f"Delete Thread {thread_id}",
        )
        return res.to_action_result("gmail_delete")

    return _do_delete().to_action_result("gmail_delete")


# ── Registration Declarations for core/action_loader ──────────────────────────

TOOLS = [
    {
        "name": "gmail_search",
        "description": "Fast deep-link search in Gmail. Returns list of matching email threads (sender, subject, snippet, thread_id).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Search query or sender/subject filter."},
                "max": {"type": "INTEGER", "description": "Maximum threads to return (default 10)."},
            },
            "required": ["query"],
        },
        "handler": gmail_search,
        "permission": "read_only",
    },
    {
        "name": "gmail_read",
        "description": "Read full email body and attachment names by thread_id. Content is safety-wrapped as untrusted.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "thread_id": {"type": "STRING", "description": "The Gmail thread ID to open and read."},
            },
            "required": ["thread_id"],
        },
        "handler": gmail_read,
        "permission": "read_only",
    },
    {
        "name": "gmail_draft",
        "description": "Create an email draft using compose deep link. Leaves composer open for review. Requires NO confirmation.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "to": {"type": "STRING", "description": "Recipient email address."},
                "subject": {"type": "STRING", "description": "Email subject line."},
                "body": {"type": "STRING", "description": "Email body text."},
                "cc": {"type": "STRING", "description": "Optional CC recipient."},
            },
            "required": ["to", "subject", "body"],
        },
        "handler": gmail_draft,
        "permission": "normal",
    },
    {
        "name": "gmail_send",
        "description": "Send the drafted email. STRICTLY GATED: Requires explicit human confirmation before sending.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "to": {"type": "STRING", "description": "Recipient email address for confirmation display."},
                "subject": {"type": "STRING", "description": "Subject line for confirmation display."},
            },
        },
        "handler": gmail_send,
        "permission": "sensitive",
    },
    {
        "name": "gmail_reply",
        "description": "Reply to an existing email thread. Types draft into thread; send step is gated behind confirmation.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "thread_id": {"type": "STRING", "description": "Thread ID to reply to."},
                "body": {"type": "STRING", "description": "Reply message body text."},
            },
            "required": ["thread_id", "body"],
        },
        "handler": gmail_reply,
        "permission": "sensitive",
    },
    {
        "name": "gmail_manage",
        "description": "Manage email thread: 'archive', 'star', 'mark_read', or 'mark_unread'.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "thread_id": {"type": "STRING", "description": "Thread ID."},
                "action": {
                    "type": "STRING",
                    "enum": ["archive", "star", "mark_read", "mark_unread"],
                    "description": "Management action.",
                },
            },
            "required": ["thread_id", "action"],
        },
        "handler": gmail_manage,
        "permission": "normal",
    },
    {
        "name": "gmail_delete",
        "description": "Delete email thread (move to Trash). STRICTLY GATED behind human confirmation.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "thread_id": {"type": "STRING", "description": "Thread ID to move to Trash."},
            },
            "required": ["thread_id"],
        },
        "handler": gmail_delete,
        "permission": "destructive",
    },
]
