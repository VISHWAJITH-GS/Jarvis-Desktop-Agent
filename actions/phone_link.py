"""
actions/phone_link.py — Windows-Only Microsoft Phone Link Controller for JARVIS.

Single action-parameterized tool (`phone_link`) driving Phone Link strictly via
Windows UI Automation (UIA) and documented keyboard shortcuts.
All feature groups (calls, messages, notifications, photos, apps, watcher) are
disabled by default in config and gated by UI-issued confirmations.
"""

from __future__ import annotations

from typing import Any

from actions import _phone_link_driver as drv
from core import outcome
from memory import config_manager

# Per-action tier metadata (used for documentation & permission routing)
ACTION_TIERS: dict[str, str] = {
    "status": "READ_ONLY",
    "launch": "READ_ONLY",
    "open_feature": "READ_ONLY",
    "diagnose": "READ_ONLY",
    "recent_calls": "READ_ONLY",
    "call": "SENSITIVE",
    "answer": "NORMAL",
    "decline": "NORMAL",
    "end_call": "NORMAL",
    "dial_digits": "NORMAL",
    "call_status": "READ_ONLY",
    "list_conversations": "READ_ONLY",
    "read_conversation": "READ_ONLY",
    "send_sms": "SENSITIVE",
    "send_mms_attachment": "SENSITIVE",
    "list_notifications": "READ_ONLY",
    "dismiss_notification": "NORMAL",
    "clear_all": "NORMAL",
    "dnd": "NORMAL",
    "mute_phone": "NORMAL",
    "list_recent_photos": "READ_ONLY",
    "copy_photo": "NORMAL",
    "save_photo": "NORMAL",
    "share_photo": "SENSITIVE",
    "delete_photo": "DESTRUCTIVE",
    "list_apps": "READ_ONLY",
    "open_app": "NORMAL",
    "open_phone_screen": "NORMAL",
    "dial_via_tel_uri": "SENSITIVE",
    "watcher_status": "READ_ONLY",
    "watcher_start": "NORMAL",
    "watcher_stop": "NORMAL",
}

_ALL_ACTIONS = list(ACTION_TIERS.keys())


def _require_group(action: str, group: str) -> str | None:
    if not config_manager.is_phone_link_feature_enabled(group):
        return drv.unsupported_result(
            action,
            f"Phone Link '{group}' feature is disabled by default. Enable it in Settings > Phone Link.",
        )
    return None


def phone_link(parameters: dict | None = None, player: Any = None, **kwargs: Any) -> str:
    """Action-parameterized entry point for Microsoft Phone Link control."""
    params = dict(parameters or {})
    params.update(kwargs)
    action = str(params.get("action", "")).strip().lower()

    if not action:
        return outcome.failed("phone_link", "Missing required parameter 'action'.")

    if action not in ACTION_TIERS:
        return outcome.failed(
            "phone_link",
            f"Unsupported action '{action}'. Allowed: {', '.join(_ALL_ACTIONS)}",
        )

    if not drv.is_windows_platform():
        return drv.unsupported_result(
            action,
            "Microsoft Phone Link is only supported on Windows.",
        )

    # Phase 1: Status, Launch, Open Feature, Diagnose
    if action == "status":
        return drv.op_status()
    if action == "launch":
        return drv.op_launch()
    if action == "open_feature":
        feature = str(params.get("feature", "")).strip()
        if not feature:
            return outcome.failed(
                "open_feature",
                "Missing 'feature' (calls|messages|photos|apps|notifications|settings).",
            )
        return drv.op_open_feature(feature)
    if action == "diagnose":
        return drv.op_diagnose()

    # Phase 2+: Feature-group gated actions (disabled by default)
    group_map = {
        "list_notifications": "notifications",
        "dismiss_notification": "notifications",
        "clear_all": "notifications",
        "dnd": "notifications",
        "mute_phone": "notifications",
        "list_conversations": "messages",
        "read_conversation": "messages",
        "send_sms": "messages",
        "send_mms_attachment": "messages",
        "recent_calls": "calls",
        "call": "calls",
        "answer": "calls",
        "decline": "calls",
        "end_call": "calls",
        "dial_digits": "calls",
        "call_status": "calls",
        "dial_via_tel_uri": "dial_via_tel_uri",
        "list_recent_photos": "photos",
        "copy_photo": "photos",
        "save_photo": "photos",
        "share_photo": "share_photo",
        "delete_photo": "delete_photo",
        "list_apps": "apps",
        "open_app": "apps",
        "open_phone_screen": "apps",
        "watcher_status": "watcher",
        "watcher_start": "watcher",
        "watcher_stop": "watcher",
    }
    grp = group_map.get(action)
    if grp:
        blocked = _require_group(action, grp)
        if blocked:
            return blocked

    # Phase 2: Notifications & Phone Controls
    if action == "list_notifications":
        return drv.op_list_notifications(limit=int(params.get("limit", 15)))
    if action == "dismiss_notification":
        idx_val = params.get("index")
        return drv.op_dismiss_notification(
            app=params.get("app"),
            index=int(idx_val) if idx_val is not None else None,
        )
    if action == "clear_all":
        return drv.op_clear_all_notifications()
    if action == "dnd":
        return drv.op_toggle_phone_control("dnd", "phone_dnd_toggle", str(params.get("state", "")))
    if action == "mute_phone":
        return drv.op_toggle_phone_control("mute_phone", "phone_mute_toggle", str(params.get("state", "")))

    # Phase 3: Messages (SMS & Optional MMS)
    if action == "list_conversations":
        return drv.op_list_conversations(limit=int(params.get("limit", 15)))
    if action == "read_conversation":
        return drv.op_read_conversation(
            contact=str(params.get("contact") or params.get("target") or ""),
            limit=int(params.get("limit", 10)),
            include_otp=bool(params.get("include_otp", False)),
        )
    if action == "send_sms":
        return drv.op_send_sms(
            target=str(params.get("target") or params.get("contact") or ""),
            text=str(params.get("text", "")),
            confirmed=bool(params.get("confirmed", False)),
        )
    if action == "send_mms_attachment":
        return drv.op_send_mms_attachment(
            target=str(params.get("target") or params.get("contact") or ""),
            file_path=str(params.get("file", "")),
            confirmed=bool(params.get("confirmed", False)),
        )

    return drv.unsupported_result(action, f"Action '{action}' is scheduled for a subsequent phase.")


TOOL = {
    "name": "phone_link",
    "permission": "sensitive",
    "tier": "extra",
    "brief": "Control Microsoft Phone Link on Windows (calls, SMS, notifications, photos, apps).",
    "description": (
        "Windows-only Microsoft Phone Link controller via UI Automation. "
        "Supports status/launch/open_feature/diagnose, notifications/DND/mute, "
        "SMS/MMS messages, calls, Android photos, and mobile apps. "
        "Feature groups are disabled by default until enabled in Settings."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": _ALL_ACTIONS,
                "description": "Phone Link operation to execute.",
            },
            "feature": {
                "type": "STRING",
                "description": "Feature page for open_feature: calls | messages | photos | apps | notifications | settings.",
            },
            "target": {
                "type": "STRING",
                "description": "Contact name or phone number for call / send_sms / send_mms_attachment.",
            },
            "contact": {
                "type": "STRING",
                "description": "Contact name or number for read_conversation.",
            },
            "text": {
                "type": "STRING",
                "description": "Message body for send_sms.",
            },
            "file": {
                "type": "STRING",
                "description": "Local file path for send_mms_attachment.",
            },
            "digits": {
                "type": "STRING",
                "description": "DTMF keypad digits for dial_digits during an active call.",
            },
            "state": {
                "type": "STRING",
                "description": "Target state ('on' or 'off') for dnd / mute_phone.",
            },
            "app": {
                "type": "STRING",
                "description": "App name for dismiss_notification.",
            },
            "name": {
                "type": "STRING",
                "description": "Mobile app name for open_app.",
            },
            "index": {
                "type": "INTEGER",
                "description": "0-based item index for dismiss_notification or photo actions.",
            },
            "folder": {
                "type": "STRING",
                "description": "Destination folder for save_photo.",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Maximum items to return (calls/messages/notifications/photos).",
            },
            "confirmed": {
                "type": "BOOLEAN",
                "description": "Set only by UI confirmation gate for sensitive/destructive actions.",
            },
        },
        "required": ["action"],
    },
    "handler": phone_link,
}
