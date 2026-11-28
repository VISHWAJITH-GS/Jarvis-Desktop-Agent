from __future__ import annotations

from unittest.mock import patch

from actions import _phone_link_driver as drv
from actions.phone_link import phone_link
from core import outcome


def _make_calls_window(initial_state: str = "idle") -> dict:
    children: list[dict] = []
    win = {
        "automation_id": "PhoneLinkRoot",
        "control_type": "Window",
        "name": "Phone Link",
        "children": children,
    }
    dial_input = {"automation_id": "DialerInputBox", "control_type": "Edit", "name": "Phone number", "value": ""}
    end_btn = {"automation_id": "EndCallButton", "control_type": "Button", "name": "End call"}
    ans_btn = {"automation_id": "AnswerCallButton", "control_type": "Button", "name": "Answer"}
    dec_btn = {"automation_id": "DeclineCallButton", "control_type": "Button", "name": "Decline"}

    def _to_in_call():
        for b in (ans_btn, dec_btn):
            if b in children:
                children.remove(b)
        if end_btn not in children:
            children.append(end_btn)

    def _to_idle():
        for b in (ans_btn, dec_btn, end_btn):
            if b in children:
                children.remove(b)

    end_btn["on_click"] = _to_idle
    ans_btn["on_click"] = _to_in_call
    dec_btn["on_click"] = _to_idle

    calls_list = {
        "automation_id": "CallHistoryListView",
        "control_type": "List",
        "name": "Recent calls",
        "children": [
            {
                "automation_id": "",
                "control_type": "ListItem",
                "name": "Mom",
                "children": [
                    {"automation_id": "", "control_type": "Text", "name": "Mom +1-555-777-8888"},
                    {"automation_id": "", "control_type": "Text", "name": "Incoming · 2m"},
                ],
            }
        ],
    }
    children.extend([
        calls_list,
        dial_input,
        {"automation_id": "DialButton", "control_type": "Button", "name": "Call", "on_click": _to_in_call},
    ])
    if initial_state == "incoming":
        children.extend([ans_btn, dec_btn])
    elif initial_state == "in_call":
        children.append(end_btn)
    return win


def test_recent_calls_emergency_block_and_call_flow():
    win = _make_calls_window("idle")
    drv._RATE_TIMESTAMPS["outbound"].clear()
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True), patch(
        "actions._phone_link_driver.find_phone_link_window", return_value=win
    ):
        res_rec = phone_link({"action": "recent_calls", "limit": 10})
        assert outcome.normalize_result(res_rec, "recent_calls").status == "done"
        assert "UNTRUSTED" in res_rec
        assert "555-777-8888" not in res_rec

        # Emergency number blocked
        res_911 = phone_link({"action": "call", "target": "911", "confirmed": True})
        assert outcome.normalize_result(res_911, "call").status == "failed"
        assert "blocked" in res_911

        # dial_digits refused when idle
        res_dig_idle = phone_link({"action": "dial_digits", "digits": "123#"})
        assert outcome.normalize_result(res_dig_idle, "dial_digits").status == "failed"

        # Unconfirmed call asks
        res_ask = phone_link({"action": "call", "target": "Mom"})
        assert "status=ask" in res_ask

        # Confirmed call transitions to in_call
        res_call = phone_link({"action": "call", "target": "Mom", "confirmed": True})
        assert outcome.normalize_result(res_call, "call").status == "done"
        assert "call_state=in_call" in res_call

        # dial_digits works while in_call
        res_dig = phone_link({"action": "dial_digits", "digits": "123#"})
        assert outcome.normalize_result(res_dig, "dial_digits").status == "done"
        assert win.get("_dialed_digits") == ["123#"]

        # end_call returns to idle
        res_end = phone_link({"action": "end_call"})
        assert outcome.normalize_result(res_end, "end_call").status == "done"
        assert "call_state=idle" in res_end


def test_answer_and_decline_incoming_call():
    win_in = _make_calls_window("incoming")
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True), patch(
        "actions._phone_link_driver.find_phone_link_window", return_value=win_in
    ):
        assert "call_state=incoming" in phone_link({"action": "call_status"})
        res_ans = phone_link({"action": "answer"})
        assert outcome.normalize_result(res_ans, "answer").status == "done"
        assert "call_state=in_call" in res_ans

    win_dec = _make_calls_window("incoming")
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True), patch(
        "actions._phone_link_driver.find_phone_link_window", return_value=win_dec
    ):
        res_dec = phone_link({"action": "decline"})
        assert outcome.normalize_result(res_dec, "decline").status == "done"
        assert "call_state=idle" in res_dec
