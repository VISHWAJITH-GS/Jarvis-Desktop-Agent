"""
plugins/whatsapp_automation.py

Auto-discovered JARVIS plugin for sending formal and flexible WhatsApp messages.
"""

from core import outcome
from core import gemini
from plugins import _whatsapp_core as wa


PLUGIN = {
    "name": "whatsapp_send",
    "description": (
        "Send WhatsApp messages to contacts formally, accurately, and flexibly. "
        "CRITICAL: If the user has NOT provided the actual message content yet (e.g. they only said "
        "'send a message to Mom' or 'text Rahul'), DO NOT call this tool with placeholder text. "
        "Instead, ask the user first: 'What message would you like me to send to <contact>?' "
        "Only call this tool once you know the exact message the user wants to send. "
        "Ask for confirmation at most once per action. Never demand a stricter confirmation phrase."
    ),
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
                "description": "The recipient's contact name (e.g. 'Mom', 'Rahul', 'Boss').",
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

    # If action indicates reading, delegate directly
    if action in ("read", "read_chat", "read_latest", "summarize"):
        from plugins import whatsapp_reader

        return whatsapp_reader.run(parameters, player=player, session_memory=session_memory)

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

    # Format message formally
    final_message = _polish_message_formal(raw_message) if formal else raw_message

    # 1. Deterministic code-level confirmation gate
    # Do NOT open WhatsApp before user confirmation!
    is_authorized = bool(p.get("_execution_authorized") or p.get("confirmed"))
    if not is_authorized:
        from core import confirm

        def _do_send_action() -> outcome.ActionResult:
            return _execute_send(contact, final_message, player=player)

        pending = confirm.save_pending(
            tool="whatsapp_send",
            params={
                "contact": contact,
                "text": final_message,
                "formal": formal,
                "receiver": contact,
                "message": final_message,
            },
            run=_do_send_action,
            expires_in=60.0,
            title=f"Send WhatsApp message to {contact}",
            detail=f"Message: '{final_message}'",
        )
        return outcome.result_needs_confirmation(
            "whatsapp_send",
            message=f"Shall I proceed with sending WhatsApp message to {contact}: '{final_message}'? Say confirm to execute, or no to cancel.",
            data={"id": pending.id, "tool": "whatsapp_send", "params": {"contact": contact, "text": final_message}},
        )

    # 2. Authorized execution
    return _execute_send(contact, final_message, player=player)


def _execute_send(contact: str, message: str, player=None) -> outcome.ActionResult:
    """Execute WhatsApp message sending and return honest ActionResult contract."""
    if player and hasattr(player, "write_log"):
        try:
            player.write_log(f"JARVIS: Opening WhatsApp for {contact}…")
        except Exception:
            pass

    driver, why = wa.get()
    if not driver:
        return outcome.result_failed(
            "whatsapp_send",
            error=why,
            message=f"Could not initialize WhatsApp driver: {why}",
            data={"contact": contact},
        )

    sent, info = driver.send_message_to(contact, message)
    if not sent:
        return outcome.result_failed(
            "whatsapp_send",
            error=info,
            message=f"Failed to send the message to {contact}: {info}",
            data={"contact": contact},
        )

    msg_preview = f'"{message}"'
    info_str = str(info or "").lower()

    # Honest status: "done" ONLY after sent tick is verified, otherwise "unverified"
    has_negative = any(neg in info_str for neg in ("could not", "not confirmed", "unconfirmed", "not verified", "failed", "unverified"))
    is_tick_verified = (
        not has_negative
        and (
            "sent tick" in info_str
            or "tick verified" in info_str
            or "tick confirmed" in info_str
            or "verified sent tick" in info_str
            or "delivered" in info_str
            or "read" in info_str
            or "confirmed visible" in info_str
        )
    )

    if is_tick_verified:
        if player and hasattr(player, "write_log"):
            try:
                player.write_log(f"JARVIS: Message sent to {contact}: {msg_preview}")
            except Exception:
                pass
        return outcome.result_done(
            "whatsapp_send",
            message=f"I have sent the message to {contact}: {msg_preview}",
            verified=True,
            data={"contact": contact, "text": message, "to": contact, "verified": True},
        )
    else:
        # Keystrokes went out but sent tick was NOT verified
        unverified_msg = f"The message was typed for {contact}, but delivery could not be confirmed."
        if player and hasattr(player, "write_log"):
            try:
                player.write_log(f"JARVIS: {unverified_msg}")
            except Exception:
                pass
        return outcome.result_unverified(
            "whatsapp_send",
            message=unverified_msg,
            data={"contact": contact, "text": message, "to": contact, "verified": False},
        )
