"""
plugins/whatsapp_reader.py — Auto-discovered JARVIS plugin for reading and summarizing WhatsApp messages.

Capabilities:
- Read recent incoming WhatsApp messages.
- Identify sender and read message content aloud.
- Read latest message from a specified contact.
- Summarize recent conversations with a contact.
"""

from __future__ import annotations

from core import outcome
from plugins import _whatsapp_web


PLUGIN = {
    "name": "whatsapp_read",
    "description": (
        "Reads recent incoming WhatsApp messages or chat history with a specified contact on WhatsApp Web. "
        "Capabilities: "
        "1. Read latest message from a contact: action='read_chat', contact='Mom' "
        "2. Read recent incoming messages across chats: action='read_latest', limit=5 "
        "3. Summarize recent conversation with a contact: action='summarize', contact='Rahul' "
        "Use whenever the user asks 'what did my friend send', 'read my latest WhatsApp message', "
        "'read latest conversation with mom', or 'summarize my chat with X'."
    ),
    "brief": "Reads recent incoming WhatsApp messages, contact chat history, and summarizes conversations.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "read_chat | read_latest | summarize",
            },
            "contact": {
                "type": "STRING",
                "description": "The contact name (e.g. 'Mom', 'Rahul', 'Friend'). Optional for read_latest.",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Number of recent messages to inspect (default 5).",
            },
        },
        "required": [],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> outcome.ActionResult | str:
    params = parameters or {}
    action = (params.get("action") or "read_chat").lower().strip()
    contact = (params.get("contact") or "").strip()
    limit = int(params.get("limit") or 5)

    driver = _whatsapp_web.get_driver()

    # 1. Summarize conversation
    if action in ("summarize", "summarize_chat", "summary"):
        if not contact:
            return outcome.result_failed("whatsapp_read", "Please specify which contact's conversation to summarize.")
        ok, summary = driver.summarize_conversation(contact, limit=max(limit, 10))
        if ok:
            return outcome.result_done(
                "whatsapp_read",
                f"Summary of conversation with {contact}: {summary}",
                verified=True,
                data={"contact": contact, "summary": summary},
            )
        return outcome.result_failed("whatsapp_read", f"Could not summarize chat with {contact}: {summary}")

    # 2. Read specific contact chat or latest
    res = driver.read_messages(contact_name=contact if contact else None, limit=limit, player=player)
    if not res.is_success():
        if res.status == outcome.STATUS_BLOCKED:
            return outcome.result_blocked("whatsapp_read", res.message or "WhatsApp Web is not signed in; scan QR code in Jarvis Chrome.")
        return outcome.result_failed("whatsapp_read", res.error or res.message)

    msgs = res.data.get("messages", [])
    if not msgs:
        return outcome.result_done(
            "whatsapp_read",
            f"No messages found for {contact or 'the current chat'}.",
            verified=True,
            data={"contact": contact, "count": 0, "messages": []},
        )

    # Format output with untrusted-content markers preserved
    lines = []
    for m in msgs:
        sender = m.get("sender", contact or "Contact")
        raw_text = m.get("raw_text", m.get("text", ""))
        safe_wrapped = outcome.untrusted("whatsapp", raw_text)
        lines.append(f"{sender}: {safe_wrapped}")

    spoken_summary = f"Recent messages with {contact or 'WhatsApp'}:\n" + "\n".join(lines)
    return outcome.result_done(
        "whatsapp_read",
        spoken_summary,
        verified=True,
        data={
            "contact": contact,
            "count": len(msgs),
            "messages": msgs,
            "timestamp_range": res.data.get("timestamp_range", "recent"),
        },
    )
