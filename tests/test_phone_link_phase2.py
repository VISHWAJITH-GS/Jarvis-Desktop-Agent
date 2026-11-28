from __future__ import annotations

from unittest.mock import patch

from actions import _phone_link_driver as drv
from actions.phone_link import phone_link
from core import outcome, undo


def _make_notif_window(android: bool = True) -> dict:
    notifs = []
    dnd_node = {"automation_id": "DoNotDisturbToggle", "control_type": "Button", "name": "Do not disturb", "toggle_state": False}
    mute_node = {"automation_id": "MutePhoneToggle", "control_type": "Button", "name": "Mute", "toggle_state": False}

    def _toggle(node):
        node["toggle_state"] = not node["toggle_state"]

    dnd_node["on_click"] = lambda: _toggle(dnd_node)
    mute_node["on_click"] = lambda: _toggle(mute_node)

    notif_list = {
        "automation_id": "NotificationListView",
        "control_type": "List",
        "name": "Notifications",
        "children": notifs,
    }

    def _dismiss(item):
        if item in notifs:
            notifs.remove(item)

    item1 = {
        "automation_id": "",
        "control_type": "ListItem",
        "name": "Messages",
        "children": [
            {"automation_id": "", "control_type": "Text", "name": "Messages"},
            {"automation_id": "", "control_type": "Text", "name": "Bank OTP 918273 for login"},
            {"automation_id": "DismissNotificationButton", "control_type": "Button", "name": "Dismiss", "on_click": lambda: _dismiss(item1)},
        ],
    }
    item2 = {
        "automation_id": "",
        "control_type": "ListItem",
        "name": "Calendar",
        "children": [
            {"automation_id": "", "control_type": "Text", "name": "Calendar"},
            {"automation_id": "", "control_type": "Text", "name": "Standup in 10 mins"},
            {"automation_id": "DismissNotificationButton", "control_type": "Button", "name": "Dismiss", "on_click": lambda: _dismiss(item2)},
        ],
    }
    notifs.extend([item1, item2])

    children = [
        {"automation_id": "CallsNavigationButton", "control_type": "Button", "name": "Calls"},
        {"automation_id": "MessagesNavigationButton", "control_type": "Button", "name": "Messages"},
        notif_list,
        {"automation_id": "ClearAllNotificationsButton", "control_type": "Button", "name": "Clear all", "on_click": lambda: notifs.clear()},
        dnd_node,
        mute_node,
    ]
    if android:
        children.append({"automation_id": "PhotosNavigationButton", "control_type": "Button", "name": "Photos"})

    return {
        "automation_id": "PhoneLinkRoot",
        "control_type": "Window",
        "name": "Phone Link",
        "children": children,
    }


def test_list_dismiss_and_clear_notifications():
    win = _make_notif_window(android=True)
    drv.reset_untrusted_read()
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True), patch(
        "actions._phone_link_driver.find_phone_link_window", return_value=win
    ):
        res_list = phone_link({"action": "list_notifications", "limit": 10})
        assert outcome.normalize_result(res_list, "list_notifications").status == "done"
        assert "UNTRUSTED" in res_list
        assert "918273" not in res_list
        assert "[REDACTED_OTP]" in res_list
        assert drv.has_untrusted_read() is True

        res_dis = phone_link({"action": "dismiss_notification", "app": "Calendar"})
        assert outcome.normalize_result(res_dis, "dismiss_notification").status == "done"
        assert "remaining=1" in res_dis

        res_clr = phone_link({"action": "clear_all"})
        assert outcome.normalize_result(res_clr, "clear_all").status == "done"
        assert "remaining=0" in res_clr


def test_dnd_and_mute_with_undo_and_iphone_unsupported():
    drv.invalidate_capability_cache()
    win_android = _make_notif_window(android=True)
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True), patch(
        "actions._phone_link_driver.find_phone_link_window", return_value=win_android
    ):
        res_dnd = phone_link({"action": "dnd", "state": "on"})
        assert outcome.normalize_result(res_dnd, "dnd").status == "done"
        assert "undoable=true" in res_dnd
        assert "Undone" in undo.undo_last()

        res_mute = phone_link({"action": "mute_phone", "state": "on"})
        assert outcome.normalize_result(res_mute, "mute_phone").status == "done"
        assert "Undone" in undo.undo_last()

    drv.invalidate_capability_cache()
    win_iphone = _make_notif_window(android=False)
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True), patch(
        "actions._phone_link_driver.find_phone_link_window", return_value=win_iphone
    ):
        res_dnd_ios = phone_link({"action": "dnd", "state": "on"})
        assert "unsupported" in res_dnd_ios
