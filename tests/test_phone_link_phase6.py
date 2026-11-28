from __future__ import annotations

from unittest.mock import patch

import pytest

from actions import _phone_link_driver as drv
from actions.phone_link import phone_link
from core import outcome


@pytest.mark.parametrize(
    "locale_tag,expected_name",
    [
        ("en-US", "Messages"),
        ("es-ES", "Mensajes"),
        ("fr-FR", "Messages"),
        ("de-DE", "Nachrichten"),
        ("hi-IN", "संदेश"),
    ],
)
def test_multilocale_selector_resolution(locale_tag: str, expected_name: str):
    fake_win = {
        "automation_id": "",
        "control_type": "Window",
        "name": "Phone Link",
        "children": [
            {"automation_id": "", "control_type": "Button", "name": expected_name},
        ],
    }
    elem, matched = drv.resolve_control(fake_win, "nav_messages", preferred_locale=locale_tag)
    assert elem["name"] == expected_name
    assert matched == f"locale:{locale_tag}"


def test_missing_selector_invalidates_cache_and_suggests_diagnose():
    drv._CAP_CACHE["ts"] = 1e12
    drv._CAP_CACHE["data"] = {"window_open": True}
    empty_win = {"automation_id": "PhoneLinkRoot", "control_type": "Window", "name": "Phone Link", "children": []}
    with patch("memory.config_manager.is_phone_link_feature_enabled", return_value=True), patch(
        "actions._phone_link_driver.find_phone_link_window", return_value=empty_win
    ):
        res = phone_link({"action": "list_notifications"})
        assert outcome.normalize_result(res, "list_notifications").status == "failed"
        assert "notifications_list" in res
        assert "diagnose" in res
        assert drv._CAP_CACHE["data"] is None


def test_rate_limit_enforcement():
    drv._RATE_TIMESTAMPS["outbound"].clear()
    with patch("memory.config_manager.get_phone_link_settings", return_value={"rate_limit_per_min": 2, "emergency_numbers": ["911"]}):
        assert drv.check_rate_limit("outbound")[0] is True
        drv.record_rate_event("outbound")
        drv.record_rate_event("outbound")
        allowed, used, lim = drv.check_rate_limit("outbound")
        assert allowed is False
        assert used == 2 and lim == 2
    drv._RATE_TIMESTAMPS["outbound"].clear()
