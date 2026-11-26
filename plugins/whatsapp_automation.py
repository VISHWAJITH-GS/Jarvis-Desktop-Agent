"""
plugins/whatsapp_automation.py

Auto-discovered JARVIS plugin for sending formal and flexible WhatsApp messages.
"""

from core import gemini, outcome
from plugins import _whatsapp_web


PLUGIN = {
    "name": "whatsapp_send",
    "description": (
        "Send WhatsApp messages to contacts via WhatsApp Web in the dedicated Jarvis browser. "
        "CRITICAL: If the user has NOT provided the actual message content yet (e.g. they only said "
        "'send a message to Mom' or 'text Rahul'), DO NOT call this tool with placeholder text. "
        "Instead, ask the user first: 'What message would you like me to send to <contact>?' "
        "Only call this tool once you know the exact message the user wants to send. "
        "Requires user confirmation before execution. Attachments/media are unsupported."
    ),
    "brief": "Sends a WhatsApp message to a contact after user confirmation.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action to perform: 'send_message', 'read_chat', 'read_latest', or 'summarize'. Defaults to 'send_message'.",
                "enum": ["send_message", "read_chat", "read_latest", "summarize"],
            },
            "contact": {
                "type": "STRING",
                "description": "The recipient's contact name or phone number (e.g. 'Mom', 'Rahul', '+919876543210').",
            },
            "message": {
                "type": "STRING",
                "description": "The actual message content specified by the user to send. Must NOT be placeholder text.",
            },
            "formal": {
                "type": "BOOLEAN",
                "description": "Whether to polish/format the message formally and politely. Defaults to True.",
            },
        },
        "required": ["contact", "message"],
    },
}


def _polish_message_formal(raw_message: str) -> str:
    """Make the message formal and polite using LLM or smart rule-based polishing."""
    raw = (raw_message or "").strip()
    if not raw:
        return raw

    # Try asking Gemini / LLM for quick polish
    try:
        prompt = (
            f"Rewrite the following message in a polite, formal, and clear tone suitable for WhatsApp. "
            f"Preserve the exact meaning, do not add filler or quotes, return ONLY the polished message text:\n\n{raw}"
        )
        polished = gemini.text(prompt, tier=gemini.FAST, timeout_ms=4000)
        if polished and polished.strip() and len(polished.strip()) < len(raw) * 3:
            return polished.strip().strip('"').strip("'")
    except Exception:
        pass

    # Clean fallback: capitalize first letter and ensure proper punctuation
    cleaned = raw[0].upper() + raw[1:] if len(raw) > 1 else raw.upper()
    if not cleaned.endswith((".", "!", "?")):
        cleaned += "."
    return cleaned


_PLACEHOLDERS = (
    "enter your message",
    "message for",
    "insert message",
    "your message here",
    "placeholder",
    "default message",
)


def run(parameters: dict, player=None, session_memory=None) -> outcome.ActionResult | str:
    p = parameters or {}
    action = (p.get("action") or "").lower().strip()
    contact = (p.get("contact") or p.get("receiver") or p.get("recipient") or "").strip()
    raw_message = (p.get("message") or p.get("message_text") or p.get("text") or "").strip()
    formal = p.get("formal", True)

    # If action indicates reading, delegate directly (never requires confirmation)
    if action in ("read", "read_chat", "read_latest", "summarize", "status", "list", "check"):
        from plugins import whatsapp_reader

        return whatsapp_reader.run(parameters, player=player, session_memory=session_memory)

    # Attachments guard
    if any(p.get(k) for k in ("attachment", "attachments", "file", "media", "image", "photo", "audio")):
        return outcome.ActionResult(
            status=outcome.STATUS_UNSUPPORTED,
            action="whatsapp_send",
            success=False,
            verified=False,
            message="WhatsApp Web attachment sending not yet supported; send text only.",
            error="Attachments unsupported on WhatsApp Web",
        )

    if not contact:
        return outcome.result_failed(
            "whatsapp_send",
            error="No contact specified",
            message="No contact name was specified for the WhatsApp message. Please tell me who you want to message.",
            data={"contact": ""},
        )
    if not raw_message or any(placeholder in raw_message.lower() for placeholder in _PLACEHOLDERS):
        return outcome.ActionResult.ask(
            "whatsapp_send",
            f"What message would you like me to send to {contact}?",
            data={"contact": contact},
        )

    # Composing help / Drafting mode (Part 3.5)
    is_authorized = bool(p.get("_execution_authorized") or p.get("confirmed"))
    is_draft_request = action in ("draft", "compose", "help") or any(
        raw_message.lower().startswith(prefix)
        for prefix in ("draft ", "compose ", "write a message ", "i need a message ")
    )
    if is_draft_request and not is_authorized:
        topic = raw_message
        for prefix in ("draft ", "compose ", "write a message ", "i need a message "):
            if topic.lower().startswith(prefix):
                topic = topic[len(prefix) :].strip()
        try:
            prompt = (
                f"Draft a clear, polite, and concise WhatsApp message to {contact} for: {topic}. "
                f"Return ONLY the drafted message text without quotes or explanation."
            )
            drafted = gemini.text(prompt, tier=gemini.FAST, timeout_ms=5000)
            if drafted and drafted.strip():
                final_message = drafted.strip().strip('"').strip("'")
            else:
                final_message = topic
        except Exception:
            final_message = topic
    else:
        # Format message formally
        final_message = _polish_message_formal(raw_message) if formal else raw_message

    # 1. Send-time confirmation gate (Tasks 4 & 5)
    # Search contact, open chat, type draft into composer first; confirm ONLY before sending.
    if not is_authorized:
        from core import confirm

        # Check if an equivalent action was already executed recently
        recent = confirm.get_recent_execution(
            "whatsapp_send",
            {"contact": contact, "text": final_message},
            seconds=60.0,
        )
        if recent is not None:
            return outcome.result_done(
                "whatsapp_send",
                message=f"WhatsApp message to {contact} has already been sent: '{final_message}'",
                verified=True,
                data={"contact": contact, "text": final_message, "already_sent": True, "id": recent.id},
            )

        # Search contact, open chat, and type message into composer without submitting
        driver = _whatsapp_web.get_driver()
        open_contact = contact
        if hasattr(driver, "prepare_draft"):
            draft_res = driver.prepare_draft(contact, final_message)
            if isinstance(draft_res, outcome.ActionResult) and not draft_res.is_success():
                return draft_res
            if isinstance(draft_res, outcome.ActionResult) and draft_res.data:
                open_contact = draft_res.data.get("open_contact") or contact

        if is_draft_request:
            message_prompt = f"I've drafted the following message for {contact}: '{final_message}'. Shall I send it? Say confirm to proceed or no to cancel."
        else:
            from actions.whatsapp_gate import format_confirmation_prompt
            message_prompt = format_confirmation_prompt(open_contact, final_message, intended_contact=contact)

        def _do_send_action() -> outcome.ActionResult:
            return _execute_send(contact, final_message, player=player)

        pending = confirm.save_pending(
            tool="whatsapp_send",
            params={
                "contact": open_contact or contact,
                "text": final_message,
                "formal": formal,
                "receiver": contact,
                "message": final_message,
                "_execution_authorized": True,
                "confirmed": True,
            },
            run=_do_send_action,
            expires_in=60.0,
            title=f"Send WhatsApp message to {open_contact or contact}",
            detail=f"Message: '{final_message}'",
        )

        return outcome.result_needs_confirmation(
            "whatsapp_send",
            message=message_prompt,
            data={
                "id": pending.id,
                "tool": "whatsapp_send",
                "params": {"contact": open_contact or contact, "text": final_message},
                "contact": open_contact,
                "is_draft": is_draft_request,
                "draft": final_message if is_draft_request else None,
            },
        )

    # 2. Authorized execution
    return _execute_send(contact, final_message, player=player)


def _execute_send(contact: str, message: str, player=None) -> outcome.ActionResult:
    """Execute WhatsApp message sending via the unified WhatsApp Web CDP driver."""
    if player and hasattr(player, "write_log"):
        try:
            player.write_log(f"JARVIS: Sending WhatsApp message to {contact}…")
        except Exception:
            pass

    driver = _whatsapp_web.get_driver()
    if hasattr(driver, "send_message"):
        res = driver.send_message(contact, message, player=player)
        if isinstance(res, outcome.ActionResult):
            return res
    if hasattr(driver, "send_message_to"):
        sent, info = driver.send_message_to(contact, message)
        info_str = str(info or "").lower()
        has_negative = any(neg in info_str for neg in ("could not", "not confirmed", "unconfirmed", "not verified", "failed", "unverified"))
        if sent and not has_negative and any(term in info_str for term in ("tick", "delivered", "read", "sent")):
            return outcome.result_done(
                "whatsapp_send",
                f"I have sent the message to {contact}: \"{message}\"",
                verified=True,
                data={"contact": contact, "text": message, "to": contact, "verified": True},
            )
        return outcome.result_unverified(
            "whatsapp_send",
            message=f"The message was typed for {contact}, but delivery could not be confirmed.",
            data={"contact": contact, "text": message, "to": contact, "verified": False},
        )
    return outcome.result_failed("whatsapp_send", "WhatsApp Web driver is unavailable.")
