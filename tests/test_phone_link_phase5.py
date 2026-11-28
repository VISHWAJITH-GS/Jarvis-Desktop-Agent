from __future__ import annotations

from unittest.mock import patch

from actions import _phone_link_driver as drv
from actions.phone_link import phone_link
from core import outcome


def _make_photos_apps_window(android: bool = True) -> dict:
    photos = [
        {"automation_id": "", "control_type": "ListItem", "name": "Photo 2026-11-28 12:00", "on_click": lambda: None},
        {"automation_id": "", "control_type": "ListItem", "name": "Photo 2026-11-27 09:30", "on_click": lambda: None},
    ]
    opened_apps: list[str] = []
    apps = [
        {"automation_id": "", "control_type": "ListItem", "name": "Spotify", "on_click": lambda: opened_apps.append("Spotify")},
        {"automation_id": "", "control_type": "ListItem", "name": "Calculator", "on_click": lambda: opened_apps.append("Calculator")},
    ]
    children = [
        {"automation_id": "CallsNavigationButton", "control_type": "Button", "name": "Calls"},
    ]
    if android:
        children.extend([
            {"automation_id": "PhotosNavigationButton", "control_type": "Button", "name": "Photos"},
            {"automation_id": "AppsNavigationButton", "control_type": "Button", "name": "Apps"},
            {"automation_id": "PhotosGridView", "control_type": "List", "name": "Photos", "children": photos},
            {"automation_id": "AppsGridView", "control_type": "List", "name": "Apps", "children": apps},
            {"automation_id": "OpenPhoneScreenButton", "control_type": "Button", "name": "Open phone screen", "on_click": lambda: opened_apps.append("PhoneScreen")},
        ])
    return {
        "automation_id": "PhoneLinkRoot",
        "control_type": "Window",
        "name": "Phone Link",
        "children": children,
        "_opened_apps": opened_apps,
    }


def test_photos_and_apps_android_vs_iphone(tmp_path):
    drv.invalidate_capability_cache()
    win_android = _make_photos_apps_window(android=True)
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True), patch(
        "actions._phone_link_driver.find_phone_link_window", return_value=win_android
    ):
        res_ph = phone_link({"action": "list_recent_photos", "limit": 5})
        assert outcome.normalize_result(res_ph, "list_recent_photos").status == "done"
        assert "count=2" in res_ph

        res_cp = phone_link({"action": "copy_photo", "index": 0})
        assert outcome.normalize_result(res_cp, "copy_photo").status == "done"

        # Outside approved folder rejected
        res_bad_dir = phone_link({"action": "save_photo", "index": 0, "folder": str(tmp_path)})
        assert outcome.normalize_result(res_bad_dir, "save_photo").status == "failed"

        # share_photo and delete_photo require confirmation when enabled
        assert "status=ask" in phone_link({"action": "share_photo", "index": 0})
        assert "status=ask" in phone_link({"action": "delete_photo", "index": 0})

        # Apps
        res_apps = phone_link({"action": "list_apps"})
        assert outcome.normalize_result(res_apps, "list_apps").status == "done"
        assert "Spotify" in res_apps

        res_open = phone_link({"action": "open_app", "name": "Spotify"})
        assert outcome.normalize_result(res_open, "open_app").status == "done"
        assert "Spotify" in win_android["_opened_apps"]

        res_scr = phone_link({"action": "open_phone_screen"})
        assert outcome.normalize_result(res_scr, "open_phone_screen").status == "done"
        assert "PhoneScreen" in win_android["_opened_apps"]

    # iPhone / limited -> unsupported
    drv.invalidate_capability_cache()
    win_ios = _make_photos_apps_window(android=False)
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True), patch(
        "actions._phone_link_driver.find_phone_link_window", return_value=win_ios
    ):
        assert "unsupported" in phone_link({"action": "list_recent_photos"})
        assert "unsupported" in phone_link({"action": "list_apps"})


def test_watcher_lifecycle():
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True):
        res_start = phone_link({"action": "watcher_start", "poll_interval_sec": 2.0})
        assert outcome.normalize_result(res_start, "watcher_start").status == "done"
        assert "running=true" in phone_link({"action": "watcher_status"})
        res_stop = phone_link({"action": "watcher_stop"})
        assert outcome.normalize_result(res_stop, "watcher_stop").status == "done"
        assert "running=false" in phone_link({"action": "watcher_status"})
