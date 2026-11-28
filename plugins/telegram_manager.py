"""
plugins/telegram_manager.py — Telegram Bot Integration Plugin for JARVIS.

Allows JARVIS to send alerts, query chat updates, and verify bot connectivity via Telegram Bot API:
- telegram_send: Send a text message to the user or a specific chat_id.
- telegram_status: Verify Telegram bot credentials and connection via getMe.
- telegram_updates: Fetch recent incoming messages from Telegram.

Reads bot_token and default chat_id from memory.config_manager.get_plugin_setting.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Optional

from core import outcome
from memory.config_manager import get_plugin_setting

logger = logging.getLogger("telegram_manager")


def _get_telegram_config() -> tuple[Optional[str], Optional[str]]:
    token = get_plugin_setting("telegram_manager", "bot_token", None)
    chat_id = get_plugin_setting("telegram_manager", "chat_id", None)
    return token, chat_id


def _call_tg_api(method: str, payload: Optional[dict] = None) -> tuple[bool, dict]:
    token, _ = _get_telegram_config()
    if not token:
        return False, {"error": "Telegram bot_token not configured. Please set in telegram_manager settings."}

    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("ok", False), body
    except Exception as e:
        logger.error("Telegram API '%s' error: %s", method, e)
        return False, {"error": str(e)}


def telegram_status() -> str:
    token, chat_id = _get_telegram_config()
    if not token:
        return outcome.failed(
            "telegram_status",
            "Telegram bot token is not configured.",
            configured=False,
        )

    ok, res = _call_tg_api("getMe")
    if ok and "result" in res:
        bot_info = res["result"]
        return outcome.done(
            "telegram_status",
            configured=True,
            bot_username=bot_info.get("username"),
            bot_name=bot_info.get("first_name"),
            can_join_groups=bot_info.get("can_join_groups", False),
            has_default_chat=bool(chat_id),
        )
    return outcome.failed("telegram_status", res.get("error", "Failed to connect to Telegram."))


def telegram_send(
    message: str,
    chat_id: Optional[str] = None,
    parse_mode: str = "Markdown",
) -> str:
    if not message:
        return outcome.failed("telegram_send", "Missing message text.")

    _, default_chat = _get_telegram_config()
    target_chat = chat_id or default_chat
    if not target_chat:
        return outcome.failed(
            "telegram_send",
            "No chat_id specified and no default chat_id configured.",
        )

    payload = {
        "chat_id": target_chat,
        "text": message,
        "parse_mode": parse_mode,
    }

    ok, res = _call_tg_api("sendMessage", payload)
    if ok and "result" in res:
        msg_id = res["result"].get("message_id")
        return outcome.done("telegram_send", chat_id=target_chat, message_id=msg_id, verified=True)
    return outcome.failed("telegram_send", res.get("error", "Failed to send message via Telegram."))


def telegram_updates(limit: int = 5) -> str:
    ok, res = _call_tg_api(f"getUpdates?limit={limit}")
    if ok and "result" in res:
        updates = []
        for u in res["result"]:
            msg = u.get("message", {})
            sender = msg.get("from", {}).get("username") or msg.get("from", {}).get("first_name", "Unknown")
            text = msg.get("text", "")
            safe_text = outcome.untrusted("telegram", text) if text else ""
            updates.append({
                "update_id": u.get("update_id"),
                "sender": sender,
                "text": safe_text,
                "date": msg.get("date"),
            })
        return outcome.done("telegram_updates", count=len(updates), updates=updates)
    return outcome.failed("telegram_updates", res.get("error", "Failed to fetch updates."))


def run(parameters: dict, player: Any = None, session_memory: Any = None) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).lower().strip()

    if action in ("telegram_status", "status"):
        return telegram_status()

    elif action in ("telegram_send", "send", "send_message"):
        msg = str(params.get("message") or params.get("text", ""))
        chat = params.get("chat_id")
        return telegram_send(message=msg, chat_id=str(chat) if chat else None)

    elif action in ("telegram_updates", "updates", "get_updates"):
        limit = int(params.get("limit", 5))
        return telegram_updates(limit=limit)

    return outcome.failed(
        "telegram_manager",
        f"Unknown action '{action}'. Supported: telegram_status, telegram_send, telegram_updates",
    )


PLUGIN = {
    "name": "telegram_manager",
    "description": (
        "Telegram messenger integration: verify bot status, send direct notifications to user's Telegram, "
        "and fetch incoming Telegram chat updates with untrusted message sanitation."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Exact action: telegram_status | telegram_send | telegram_updates",
            },
            "message": {
                "type": "STRING",
                "description": "Text message to send to Telegram.",
            },
            "chat_id": {
                "type": "STRING",
                "description": "Target Telegram chat ID (optional if configured in settings).",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Number of updates to fetch (default: 5).",
            },
        },
        "required": ["action"],
    },
    "permissions": ["telegram_manager"],
}
