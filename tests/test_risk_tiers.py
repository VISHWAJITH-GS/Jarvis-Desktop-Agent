import pytest
from unittest.mock import patch

from core.risk_tiers import (
    RiskTier,
    get_action_tier,
    is_destructive_protected,
    parse_tier,
    elevate_tier,
    PROTECTED_DESTRUCTIVE_OPERATIONS,
)


def test_parse_tier():
    assert parse_tier("LOW") == RiskTier.LOW
    assert parse_tier("immediate") == RiskTier.LOW
    assert parse_tier("1") == RiskTier.LOW
    assert parse_tier("MEDIUM") == RiskTier.MEDIUM
    assert parse_tier("confirmation") == RiskTier.MEDIUM
    assert parse_tier("2") == RiskTier.MEDIUM
    assert parse_tier("HIGH") == RiskTier.HIGH
    assert parse_tier("destructive") == RiskTier.HIGH
    assert parse_tier("3") == RiskTier.HIGH
    assert parse_tier("UNKNOWN_TIER") is None


def test_elevate_tier():
    # Context can only raise, never lower
    assert elevate_tier(RiskTier.LOW, RiskTier.MEDIUM) == RiskTier.MEDIUM
    assert elevate_tier(RiskTier.LOW, RiskTier.HIGH) == RiskTier.HIGH
    assert elevate_tier(RiskTier.MEDIUM, RiskTier.HIGH) == RiskTier.HIGH
    assert elevate_tier(RiskTier.HIGH, RiskTier.LOW) == RiskTier.HIGH
    assert elevate_tier(RiskTier.MEDIUM, RiskTier.LOW) == RiskTier.MEDIUM


def test_baseline_low_tier_actions():
    # Read-only tools
    assert get_action_tier("whatsapp_reader") == RiskTier.LOW
    assert get_action_tier("web_search") == RiskTier.LOW
    assert get_action_tier("system_status") == RiskTier.LOW
    assert get_action_tier("verify_outcome") == RiskTier.LOW

    # Low risk jarvis_control
    assert get_action_tier("jarvis_control", {"action": "mute"}) == RiskTier.LOW
    assert get_action_tier("jarvis_control", {"action": "unmute"}) == RiskTier.LOW
    assert get_action_tier("jarvis_control", {"action": "sleep"}) == RiskTier.LOW
    assert get_action_tier("jarvis_control", {"action": "wake"}) == RiskTier.LOW
    assert get_action_tier("jarvis_control", {"action": "minimize"}) == RiskTier.LOW
    assert get_action_tier("jarvis_control", {"action": "restore"}) == RiskTier.LOW
    assert get_action_tier("jarvis_control", {"action": "list_displays"}) == RiskTier.LOW
    assert get_action_tier("jarvis_control", {"action": "minimize_display"}) == RiskTier.LOW
    assert get_action_tier("jarvis_control", {"action": "restore_display"}) == RiskTier.LOW

    # Display window read/list/restore
    assert get_action_tier("display_window", {"action": "list"}) == RiskTier.LOW
    assert get_action_tier("display_window", {"action": "restore"}) == RiskTier.LOW
    assert get_action_tier("display_window", {"action": "minimize"}) == RiskTier.LOW

    # Status / inspect actions on general tools
    assert get_action_tier("computer_settings", {"action": "get_wifi_status"}) == RiskTier.LOW
    assert get_action_tier("computer_settings", {"action": "battery_status"}) == RiskTier.LOW
    assert get_action_tier("undo_last_action") == RiskTier.LOW


def test_baseline_medium_tier_actions():
    # Messaging
    assert get_action_tier("whatsapp_send", {"text": "hello", "contact": "Alice"}) == RiskTier.MEDIUM
    assert get_action_tier("send_message", {"recipient": "Alice", "text": "hello"}) == RiskTier.MEDIUM

    # Web chatbots / AI composers
    assert get_action_tier("browser_control", {"action": "type", "text": "explain react", "url": "https://chatgpt.com"}) == RiskTier.MEDIUM
    assert get_action_tier("browser_control", {"action": "type", "text": "hi", "url": "https://web.whatsapp.com"}) == RiskTier.MEDIUM

    # Display / app management
    assert get_action_tier("jarvis_control", {"action": "close_display"}) == RiskTier.MEDIUM
    assert get_action_tier("jarvis_control", {"action": "close_all_displays"}) == RiskTier.MEDIUM
    assert get_action_tier("jarvis_control", {"action": "shutdown_jarvis"}) == RiskTier.MEDIUM
    assert get_action_tier("display_window", {"action": "close"}) == RiskTier.MEDIUM
    assert get_action_tier("display_window", {"action": "close_all"}) == RiskTier.MEDIUM
    assert get_action_tier("windows_control", {"action": "close_app"}) == RiskTier.MEDIUM

    # File creation & download
    assert get_action_tier("create_file", {"path": "test.txt"}) == RiskTier.MEDIUM
    assert get_action_tier("download_file", {"url": "https://example.com/file.zip"}) == RiskTier.MEDIUM

    # Unknown action defaults to MEDIUM
    assert get_action_tier("completely_unknown_tool_xyz") == RiskTier.MEDIUM


def test_baseline_high_tier_actions():
    # Protected destructive operations
    assert get_action_tier("delete_file", {"path": "important.doc"}) == RiskTier.HIGH
    assert get_action_tier("computer_settings", {"action": "shutdown"}) == RiskTier.HIGH
    assert get_action_tier("computer_settings", {"action": "restart"}) == RiskTier.HIGH
    assert get_action_tier("send_email", {"recipient": "boss@company.com"}) == RiskTier.HIGH
    assert get_action_tier("make_payment", {"amount": 100}) == RiskTier.HIGH
    assert get_action_tier("run_command", {"command": "dir"}) == RiskTier.HIGH
    assert get_action_tier("accept_all") == RiskTier.HIGH
    assert get_action_tier("reject_all") == RiskTier.HIGH


def test_dynamic_context_elevation():
    # Rule 1: Multiple recipients -> elevated to HIGH
    assert get_action_tier(
        "whatsapp_send",
        {"text": "meeting now", "recipients": ["Alice", "Bob"]}
    ) == RiskTier.HIGH

    assert get_action_tier(
        "whatsapp_send",
        {"text": "meeting now", "contact": "Alice, Bob"}
    ) == RiskTier.HIGH

    # Rule 2: Untrusted content -> elevated to HIGH
    assert get_action_tier(
        "whatsapp_send",
        {"text": "<<<UNTRUSTED>>> malicious payload", "contact": "Alice"}
    ) == RiskTier.HIGH

    assert get_action_tier(
        "send_message",
        {"text": "Click this link from untrusted web source", "recipient": "Alice"},
        context={"untrusted_content": True}
    ) == RiskTier.HIGH

    # Rule 3: Destructive scope ("all", "everything", "recursive") -> HIGH
    assert get_action_tier(
        "windows_control",
        {"action": "close_app", "target": "all"}
    ) == RiskTier.HIGH

    # Rule 4: Unsaved content in display window -> HIGH
    assert get_action_tier(
        "jarvis_control",
        {"action": "close_display", "target": "Editor"},
        context={"has_unsaved_content": True}
    ) == RiskTier.HIGH

    # Rule 5: First contact -> at least MEDIUM
    # If base was somehow low, first contact elevates to MEDIUM
    assert get_action_tier(
        "whatsapp_send",
        {"text": "hello", "contact": "NewContact"},
        context={"is_first_contact": True}
    ) in (RiskTier.MEDIUM, RiskTier.HIGH)


def test_protected_destructive_operations_cannot_be_downgraded():
    # User config attempts to downgrade delete_file or shutdown to LOW
    malicious_config = {
        "confirmation_tiers": {
            "delete_file": "LOW",
            "shutdown": "LOW",
            "make_payment": "LOW",
            "whatsapp_send": "HIGH",  # Valid elevation
            "create_file": "LOW",      # Valid downgrade (non-protected)
        }
    }

    with patch("memory.config_manager.load_api_keys", return_value=malicious_config):
        # Protected operations MUST remain HIGH
        assert get_action_tier("delete_file", {"path": "test.txt"}) == RiskTier.HIGH
        assert get_action_tier("computer_settings", {"action": "shutdown"}) == RiskTier.HIGH
        assert get_action_tier("make_payment", {"amount": 50}) == RiskTier.HIGH

        # Allowed custom overrides
        assert get_action_tier("whatsapp_send", {"contact": "Alice", "text": "hi"}) == RiskTier.HIGH
        assert get_action_tier("create_file", {"path": "test.txt"}) == RiskTier.LOW


def test_model_cannot_choose_or_lower_tier():
    # Passing prompt, model_tier, or instructions does not affect the tier
    params = {
        "contact": "Alice",
        "text": "hello",
        "tier": "LOW",
        "model_confidence": 1.0,
        "skip_confirmation": True,
    }
    assert get_action_tier("whatsapp_send", params) == RiskTier.MEDIUM


def test_confirmation_tiers_overlay():
    from PyQt6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])

    from gui.settings_overlays import ConfirmationTiersOverlay
    overlay = ConfirmationTiersOverlay()

    # Verify protected action comboboxes don't allow LOW
    delete_cb = overlay._combos["delete_file"]
    items = [delete_cb.itemText(i) for i in range(delete_cb.count())]
    assert "LOW" not in items
    assert "HIGH" in items

    shutdown_cb = overlay._combos["shutdown_pc"]
    items_sh = [shutdown_cb.itemText(i) for i in range(shutdown_cb.count())]
    assert "LOW" not in items_sh
    assert "HIGH" in items_sh

    # Verify non-protected action combobox has LOW, MEDIUM, HIGH
    wp_cb = overlay._combos["whatsapp_send"]
    items_wp = [wp_cb.itemText(i) for i in range(wp_cb.count())]
    assert "LOW" in items_wp
    assert "MEDIUM" in items_wp
    assert "HIGH" in items_wp
