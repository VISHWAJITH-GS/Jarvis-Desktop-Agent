from __future__ import annotations

from unittest.mock import patch

from actions import _phone_link_driver as drv
from actions.phone_link import phone_link
from core import outcome


def _make_messages_window(drop_send: bool = False) -> dict:
    bubbles = [
        {"automation_id": "", "control_type": "ListItem", "name": "Alice: Hey, are we still on?"},
        {"automation_id": "", "control_type": "ListItem", "name": "Alice: Your verification code is 551199"},
    ]
    thread_list = {
        "automation_id": "MessageListView",
        "control_type": "List",
        "name": "Messages",
        "children": bubbles,
    }
    composer = {"automation_id": "MessageTextBox", "control_type": "Edit", "name": "Type a message", "value": ""}

    def _on_send():
        if not drop_send and composer.get("value"):
            bubbles.append({"automation_id": "", "control_type": "ListItem", "name": f"You: {composer['value']}"})

    convs = [
        {
            "automation_id": "",
            "control_type": "ListItem",
            "name": "Alice Smith",
            "on_click": lambda: None,
            "children": [
                {"automation_id": "", "control_type": "Text", "name": "Alice Smith +1-555-200-1111"},
                {"automation_id": "", "control_type": "Text", "name": "OTP 551199"},
            ],
        },
        {
            "automation_id": "",
            "control_type": "ListItem",
            "name": "Alice Jones",
            "on_click": lambda: None,
            "children": [
                {"automation_id": "", "control_type": "Text", "name": "Alice Jones +1-555-300-2222"},
                {"automation_id": "", "control_type": "Text", "name": "See you tomorrow"},
            ],
        },
        {
            "automation_id": "",
            "control_type": "ListItem",
            "name": "Bob",
            "on_click": lambda: None,
            "children": [
                {"automation_id": "", "control_type": "Text", "name": "Bob +1-555-400-3333"},
                {"automation_id": "", "control_type": "Text", "name": "Lunch?"},
            ],
        },
    ]

    return {
        "automation_id": "PhoneLinkRoot",
        "control_type": "Window",
        "name": "Phone Link",
        "children": [
            {"automation_id": "PhotosNavigationButton", "control_type": "Button", "name": "Photos"},
            {"automation_id": "MessagesNavigationButton", "control_type": "Button", "name": "Messages"},
            {"automation_id": "ConversationListView", "control_type": "List", "name": "Conversations", "children": convs},
            thread_list,
            composer,
            {"automation_id": "SendMessageButton", "control_type": "Button", "name": "Send", "on_click": _on_send},
            {"automation_id": "AttachButton", "control_type": "Button", "name": "Attach", "on_click": lambda: None},
        ],
    }


def test_list_and_read_conversations_with_disambiguation_and_otp_redaction():
    win = _make_messages_window()
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True), patch(
        "actions._phone_link_driver.find_phone_link_window", return_value=win
    ):
        res_list = phone_link({"action": "list_conversations", "limit": 5})
        assert outcome.normalize_result(res_list, "list_conversations").status == "done"
        assert "UNTRUSTED" in res_list
        assert "551199" not in res_list

        # Ambiguous "Alice" -> needs_confirmation with masked numbers
        res_ambig = phone_link({"action": "read_conversation", "contact": "Alice"})
        assert "status=ask" in res_ambig
        assert "***-***-1111" in res_ambig
        assert "555-200-1111" not in res_ambig

        # Unambiguous "Bob" -> done & redacted OTP
        res_bob = phone_link({"action": "read_conversation", "contact": "Bob"})
        assert outcome.normalize_result(res_bob, "read_conversation").status == "done"
        assert "[REDACTED_OTP]" in res_bob
        assert "551199" not in res_bob


def test_send_sms_confirmation_verification_otp_block_and_rate_limit():
    win = _make_messages_window(drop_send=False)
    drv._RATE_TIMESTAMPS["outbound"].clear()
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True), patch(
        "actions._phone_link_driver.find_phone_link_window", return_value=win
    ):
        # Unconfirmed must ask
        res_ask = phone_link({"action": "send_sms", "target": "Bob", "text": "On my way"})
        assert "status=ask" in res_ask

        # Outbound OTP refused
        res_otp = phone_link({"action": "send_sms", "target": "Bob", "text": "My OTP is 123456", "confirmed": True})
        assert outcome.normalize_result(res_otp, "send_sms").status == "failed"

        # Confirmed & verified in thread
        res_ok = phone_link({"action": "send_sms", "target": "Bob", "text": "On my way", "confirmed": True})
        assert outcome.normalize_result(res_ok, "send_sms").status == "done"
        assert "verified=true" in res_ok
        assert "undoable=false" in res_ok

    # Unverified when bubble doesn't appear
    win_drop = _make_messages_window(drop_send=True)
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True), patch(
        "actions._phone_link_driver.find_phone_link_window", return_value=win_drop
    ):
        res_unv = phone_link({"action": "send_sms", "target": "Bob", "text": "Hello?", "confirmed": True})
        assert "status=unverified" in res_unv


def test_send_mms_folder_guard(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True):
        res = phone_link({"action": "send_mms_attachment", "target": "Bob", "file": str(outside), "confirmed": True})
        assert outcome.normalize_result(res, "send_mms_attachment").status == "failed"
        assert "approved folders" in res
