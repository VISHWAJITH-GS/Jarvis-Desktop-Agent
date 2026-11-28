"""
plugins/email_manager.py — Universal IMAP/SMTP Email Manager for JARVIS.

Supports multi-provider email operations:
- email_list: List recent emails (newest first), with unread/search filter.
- email_read: Read an email by message ID, wrapping body with untrusted markers.
- email_send: Send an email with strict confirmation gating (SENSITIVE).
- email_search: Search inbox by subject/sender/query.

Built-in provider presets for Gmail, Outlook, Yahoo, and iCloud.
Credentials retrieved safely via memory.config_manager.get_plugin_setting.
"""

from __future__ import annotations

import email
import imaplib
import logging
import smtplib
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

from core import outcome
from memory.config_manager import get_plugin_setting

logger = logging.getLogger("email_manager")

PRESETS = {
    "gmail": {"imap": "imap.gmail.com", "imap_port": 993, "smtp": "smtp.gmail.com", "smtp_port": 587},
    "outlook": {"imap": "outlook.office365.com", "imap_port": 993, "smtp": "smtp.office365.com", "smtp_port": 587},
    "yahoo": {"imap": "imap.mail.yahoo.com", "imap_port": 993, "smtp": "smtp.mail.yahoo.com", "smtp_port": 587},
    "icloud": {"imap": "imap.mail.me.com", "imap_port": 993, "smtp": "smtp.mail.me.com", "smtp_port": 587},
}


def _get_credentials() -> Tuple[Optional[str], Optional[str], str, int, str, int]:
    provider = (get_plugin_setting("email_manager", "provider", "gmail") or "gmail").lower()
    preset = PRESETS.get(provider, PRESETS["gmail"])

    email_user = get_plugin_setting("email_manager", "email_address", None)
    email_pass = get_plugin_setting("email_manager", "password", None)

    imap_host = get_plugin_setting("email_manager", "imap_host", preset["imap"])
    imap_port = int(get_plugin_setting("email_manager", "imap_port", preset["imap_port"]))
    smtp_host = get_plugin_setting("email_manager", "smtp_host", preset["smtp"])
    smtp_port = int(get_plugin_setting("email_manager", "smtp_port", preset["smtp_port"]))

    return email_user, email_pass, imap_host, imap_port, smtp_host, smtp_port


def _decode_str(header_val: str) -> str:
    if not header_val:
        return ""
    decoded_fragments = decode_header(header_val)
    parts = []
    for frag, enc in decoded_fragments:
        if isinstance(frag, bytes):
            parts.append(frag.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(str(frag))
    return "".join(parts)


def email_list(
    limit: int = 10,
    unread_only: bool = False,
    query: Optional[str] = None,
) -> str:
    email_user, email_pass, imap_host, imap_port, _, _ = _get_credentials()
    if not email_user or not email_pass:
        return outcome.failed(
            "email_list",
            "Email credentials not configured. Please set email_address and password in email_manager settings.",
        )

    try:
        mail = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=10.0)
        mail.login(email_user, email_pass)
        mail.select("INBOX", readonly=True)

        criteria = "UNSEEN" if unread_only else "ALL"
        if query:
            criteria = f'(OR (SUBJECT "{query}") (FROM "{query}"))'

        status, msg_nums = mail.search(None, criteria)
        if status != "OK" or not msg_nums[0]:
            mail.logout()
            return outcome.done("email_list", count=0, messages=[])

        ids = msg_nums[0].split()
        target_ids = ids[-limit:] if len(ids) > limit else ids
        target_ids.reverse()

        items: List[Dict[str, Any]] = []
        for mid in target_ids:
            res, data = mail.fetch(mid, "(RFC822.HEADER)")
            if res != "OK" or not data or not data[0]:
                continue
            raw = data[0][1] if isinstance(data[0], tuple) else b""
            msg = email.message_from_bytes(raw)
            items.append({
                "id": mid.decode("utf-8", errors="ignore"),
                "from": _decode_str(msg.get("From", "")),
                "subject": _decode_str(msg.get("Subject", "")),
                "date": msg.get("Date", ""),
            })

        mail.logout()
        return outcome.done("email_list", count=len(items), messages=items)
    except Exception as e:
        logger.error("email_list error: %s", e)
        return outcome.failed("email_list", f"IMAP connection or search error: {e}")


def email_read(msg_id: str) -> str:
    if not msg_id:
        return outcome.failed("email_read", "Missing message ID.")

    email_user, email_pass, imap_host, imap_port, _, _ = _get_credentials()
    if not email_user or not email_pass:
        return outcome.failed(
            "email_read",
            "Email credentials not configured.",
        )

    try:
        mail = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=10.0)
        mail.login(email_user, email_pass)
        mail.select("INBOX", readonly=True)

        res, data = mail.fetch(str(msg_id), "(RFC822)")
        if res != "OK" or not data or not data[0]:
            mail.logout()
            return outcome.failed("email_read", f"Message with ID '{msg_id}' not found.")

        raw = data[0][1] if isinstance(data[0], tuple) else b""
        msg = email.message_from_bytes(raw)
        subject = _decode_str(msg.get("Subject", ""))
        sender = _decode_str(msg.get("From", ""))
        date = msg.get("Date", "")

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition", ""))
                if ctype == "text/plain" and "attachment" not in disp:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                        break
        else:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

        mail.logout()

        # Wrap external email body in untrusted markers
        safe_body = outcome.untrusted("email", body.strip()[:4000]) if body else "(No text content)"

        return outcome.done(
            "email_read",
            id=msg_id,
            subject=subject,
            sender=sender,
            date=date,
            body=safe_body,
        )
    except Exception as e:
        logger.error("email_read error: %s", e)
        return outcome.failed("email_read", f"Failed to fetch email: {e}")


def email_send(
    to: str,
    subject: str,
    body: str,
    confirmed: bool = False,
) -> str:
    if not to or not subject:
        return outcome.failed("email_send", "Missing recipient 'to' or 'subject'.")

    if not confirmed:
        return outcome.ask(
            "email_send",
            f"Are you sure you want to send this email to '{to}' with subject '{subject}'? Please confirm.",
            to=to,
            subject=subject,
        )

    email_user, email_pass, _, _, smtp_host, smtp_port = _get_credentials()
    if not email_user or not email_pass:
        return outcome.failed("email_send", "Email credentials not configured.")

    try:
        msg = MIMEMultipart()
        msg["From"] = email_user
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        server = smtplib.SMTP(smtp_host, smtp_port, timeout=12.0)
        server.starttls()
        server.login(email_user, email_pass)
        server.send_message(msg)
        server.quit()

        return outcome.done("email_send", to=to, subject=subject, verified=True)
    except Exception as e:
        logger.error("email_send error: %s", e)
        return outcome.failed("email_send", f"SMTP delivery failure: {e}")


def run(parameters: dict, player: Any = None, session_memory: Any = None) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).lower().strip()

    if action in ("email_list", "list"):
        limit = int(params.get("limit", 10))
        unread = bool(params.get("unread_only", False))
        query = params.get("query")
        return email_list(limit=limit, unread_only=unread, query=query)

    elif action in ("email_read", "read"):
        msg_id = str(params.get("msg_id") or params.get("id", ""))
        return email_read(msg_id)

    elif action in ("email_send", "send"):
        to = str(params.get("to", ""))
        subject = str(params.get("subject", ""))
        body = str(params.get("body", ""))
        confirmed = bool(params.get("confirmed", False))
        return email_send(to=to, subject=subject, body=body, confirmed=confirmed)

    elif action in ("email_search", "search"):
        query = str(params.get("query", ""))
        limit = int(params.get("limit", 10))
        return email_list(limit=limit, unread_only=False, query=query)

    return outcome.failed(
        "email_manager",
        f"Unknown action '{action}'. Supported: email_list, email_read, email_send, email_search",
    )


PLUGIN = {
    "name": "email_manager",
    "description": (
        "Universal email management: list inbox, read emails with untrusted content protection, "
        "search messages, and send emails with confirmation gating across Gmail, Outlook, Yahoo, or custom IMAP/SMTP."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Exact action: email_list | email_read | email_send | email_search",
            },
            "to": {
                "type": "STRING",
                "description": "Recipient email address for email_send.",
            },
            "subject": {
                "type": "STRING",
                "description": "Email subject for email_send.",
            },
            "body": {
                "type": "STRING",
                "description": "Email text content for email_send.",
            },
            "msg_id": {
                "type": "STRING",
                "description": "Message ID for email_read.",
            },
            "query": {
                "type": "STRING",
                "description": "Search keyword or sender for email_list/email_search.",
            },
            "unread_only": {
                "type": "BOOLEAN",
                "description": "Whether to list only unread emails (default: false).",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Maximum emails to fetch (default: 10).",
            },
            "confirmed": {
                "type": "BOOLEAN",
                "description": "Must be true after user confirms sending.",
            },
        },
        "required": ["action"],
    },
    "permissions": ["email_manager"],
}
