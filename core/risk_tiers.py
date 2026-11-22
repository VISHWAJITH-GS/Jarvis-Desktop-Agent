"""
core/risk_tiers.py — Authoritative Three-Tier Action Risk Classification and Dynamic Context Elevation.

Tiers:
  LOW: No confirmation required. Executes immediately, announces in one short phrase,
       verifies outcome honestly.
  MEDIUM: Voice confirmation required. Parks in PendingActionStore for 60s, speaks a
          precise summary ("Send 'ok' to chat Ram Nath TCE?"), waits for yes/no from mic.
  HIGH: Dual confirmation required (Voice AND on-screen button click). Parks in
        PendingActionStore with HUD dialog and 60s countdown. Requires BOTH spoken confirm
        AND physical button click within expiry window. Fails closed if HUD is unavailable.

Context elevation:
  Context can only RAISE a tier, NEVER lower it:
  - First message to a new contact -> at least MEDIUM.
  - Multiple recipients -> HIGH.
  - Text from untrusted content (<<<UNTRUSTED>>>, web, email, foreign message) -> HIGH.
  - Destructive scope ("all", "everything", "recursive", "force") -> HIGH.
  - Closing display with unsaved content -> HIGH.

Model instructions or prompt text CANNOT choose or lower a tier.
Unknown actions default to MEDIUM.
Config overrides under "confirmation_tiers" can adjust tiers, but protected destructive
actions can NEVER be downgraded to LOW.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, Optional, Set


class RiskTier(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


_TIER_LEVELS = {
    RiskTier.LOW: 1,
    RiskTier.MEDIUM: 2,
    RiskTier.HIGH: 3,
}


def parse_tier(val: Any) -> Optional[RiskTier]:
    """Parse string or enum into RiskTier."""
    if isinstance(val, RiskTier):
        return val
    if not val:
        return None
    s = str(val).strip().upper()
    if s in ("LOW", "IMMEDIATE", "READ_ONLY", "NORMAL", "LEVEL_1", "1"):
        return RiskTier.LOW
    if s in ("MEDIUM", "CONFIRMATION_REQUIRED", "CONFIRMATION", "SENSITIVE", "LEVEL_2", "2"):
        return RiskTier.MEDIUM
    if s in ("HIGH", "HIGH_IMPACT", "DESTRUCTIVE", "HEAVY", "LEVEL_3", "3", "FORBIDDEN"):
        return RiskTier.HIGH
    return None


def elevate_tier(current: RiskTier, proposed: RiskTier) -> RiskTier:
    """Context can only RAISE a tier, never lower it."""
    if _TIER_LEVELS[proposed] > _TIER_LEVELS[current]:
        return proposed
    return current


# ── Protected Destructive Operations ──────────────────────────────────────────
# These actions can NEVER be configured or downgraded to LOW, even in user config.
PROTECTED_DESTRUCTIVE_OPERATIONS: Set[str] = {
    "delete_file",
    "delete_folder",
    "delete",
    "remove",
    "empty_recycle_bin",
    "empty_trash",
    "shutdown",
    "shutdown_computer",
    "shutdown_laptop",
    "restart",
    "reboot",
    "hibernate",
    "sleep_laptop",
    "sleep_computer",
    "format",
    "format_drive",
    "format_disk",
    "format_volume",
    "make_payment",
    "pay",
    "payment",
    "purchase",
    "buy",
    "place_order",
    "change_security_settings",
    "disable_firewall",
    "change_admin_settings",
    "delete_system_files",
    "accept_all",
    "reject_all",
    "execute_code",
    "run_command",
    "cmd",
    "powershell",
    "terminal",
    "send_email",
}


def is_destructive_protected(tool: str, action: str = "") -> bool:
    """Check if action is in the protected destructive operations set."""
    t = str(tool or "").strip().lower()
    a = str(action or "").strip().lower()
    if t in PROTECTED_DESTRUCTIVE_OPERATIONS or a in PROTECTED_DESTRUCTIVE_OPERATIONS:
        return True
    combined = f"{t}:{a}"
    return combined in PROTECTED_DESTRUCTIVE_OPERATIONS


# ── Default Action Tier Mappings ──────────────────────────────────────────────
# (tool, action) -> RiskTier

def _classify_default_tier(tool_name: str, params: Dict[str, Any]) -> RiskTier:
    """Classify the baseline tier for an action and parameter set in code."""
    tool = str(tool_name or "").strip().lower()
    action = str(params.get("action") or "").strip().lower()
    target = str(
        params.get("target")
        or params.get("device")
        or params.get("name")
        or params.get("ssid")
        or params.get("path")
        or params.get("url")
        or ""
    ).strip().lower()

    if is_destructive_protected(tool, action):
        return RiskTier.HIGH

    # 1. Status / read / inspect / list actions -> strictly LOW
    READ_ONLY_TOOLS = {
        "web_search",
        "system_status",
        "recall_memory",
        "save_memory",
        "flight_finder",
        "weather_report",
        "youtube_video",
        "observe_page",
        "code_helper",
        "leetcode_assistant",
        "screen_process",
        "verify_outcome",
        "list_tasks",
        "task_status",
        "list_routines",
        "productivity",
        "whatsapp_reader",
        "whatsapp_read",
        "whatsapp_list_unread",
        "copy_page_text_to_clipboard",
        "page_to_file",
    }
    if tool in READ_ONLY_TOOLS:
        return RiskTier.LOW

    READ_ONLY_ACTIONS = {
        "read",
        "status",
        "list",
        "get_status",
        "check",
        "inspect",
        "observe",
        "get_elements",
        "get_text",
        "read_chat",
        "read_latest",
        "summarize",
        "list_tasks",
        "list_monitors",
        "list_routines",
        "list_networks",
        "list_devices",
        "list_apps",
        "list_running_apps",
        "list_displays",
        "get_wifi_status",
        "wifi_status",
        "get_bluetooth_status",
        "bluetooth_status",
        "get_power_status",
        "battery_status",
        "volume_status",
        "get_audio_status",
        "show_tasks",
        "get_tasks",
        "task_status",
        "show",
        "view",
        "display",
        "find",
        "search",
        "search_files",
        "extract_text",
        "disk_usage",
        "info",
        "get_active_window",
    }
    if action in READ_ONLY_ACTIONS:
        return RiskTier.LOW

    if any(tool.startswith(p) for p in ("list_", "get_", "check_")) or any(
        action.startswith(p) for p in ("list_", "get_", "check_")
    ):
        if not any(
            d in tool or d in action
            for d in ("delete", "kill", "remove", "format", "shutdown", "destroy", "wipe")
        ):
            return RiskTier.LOW

    # 2. Undo
    if tool in ("undo", "undo_last_action", "undo_action") or action in ("undo", "undo_last_action"):
        return RiskTier.LOW

    # 3. JARVIS Self-Control Actions
    if tool in ("jarvis_control", "sleep_jarvis", "windows_control", "computer_settings"):
        if tool == "sleep_jarvis" or action in ("sleep", "go_to_sleep", "sleep_jarvis", "wake", "wake_jarvis"):
            return RiskTier.LOW
        if action in ("mute", "unmute", "mute_speaker", "unmute_speaker", "mute_mic", "unmute_mic"):
            return RiskTier.LOW
        if action in ("minimize", "restore", "minimize_jarvis", "restore_jarvis", "back_to_normal"):
            return RiskTier.LOW
        if action in ("list_displays", "minimize_display", "restore_display"):
            return RiskTier.LOW
        if action in ("close_display", "close_all_displays", "close_all"):
            return RiskTier.MEDIUM
        if action in ("shutdown_jarvis", "close_jarvis", "exit_jarvis", "quit_jarvis"):
            return RiskTier.MEDIUM
        if action in ("close_app", "kill", "quit", "exit") and any(k in target for k in ("jarvis", "assistant", "ai")):
            return RiskTier.MEDIUM

    # 4. Display Window Controls (Jarvis created displays)
    if tool == "display_window":
        if action in (
            "show_image",
            "show_video",
            "show_text",
            "open_display",
            "list_displays",
            "list",
            "focus_display",
            "focus",
            "minimize_display",
            "minimize",
            "restore_display",
            "restore",
            "maximize_display",
            "maximize",
        ):
            return RiskTier.LOW
        if action in ("close_display", "close_all_displays", "close_all", "close"):
            # Closing display with unsaved content is checked in dynamic context elevation
            return RiskTier.MEDIUM

    # 5. External Applications & Navigation
    if tool == "open_app":
        return RiskTier.LOW

    if tool in ("browser_control", "action_executor"):
        # Check financial / checkout keywords
        financial_keywords = (
            "checkout",
            "payment",
            "pay",
            "cart",
            "billing",
            "order",
            "purchase",
            "paypal",
            "stripe",
            "bank",
            "banking",
        )
        if any(k in target for k in financial_keywords):
            return RiskTier.HIGH

        if action in ("switch_tab", "new_tab", "close_tab", "back", "wait", "scroll", "scroll_page", "scroll_element", "navigate", "go_to", "open"):
            return RiskTier.LOW

        if action in ("type", "input", "smart_type", "fill_form", "press", "press_key", "click", "smart_click"):
            # Check if web chat / ChatGPT or web messenger composer
            ai_chat_domains = ("chatgpt.com", "chat.openai.com", "claude.ai", "gemini.google.com")
            if any(dom in target for dom in ai_chat_domains):
                return RiskTier.MEDIUM
            if "web.whatsapp.com" in target or "web.telegram.org" in target:
                return RiskTier.MEDIUM
            return RiskTier.LOW

        if action in ("download", "download_file"):
            return RiskTier.MEDIUM

        if action in ("make_payment", "pay", "payment", "purchase", "buy", "place_order"):
            return RiskTier.HIGH

    # 6. Messaging & External Chat Prompts -> MEDIUM
    if tool in ("whatsapp_send", "whatsapp", "telegram", "telegram_send"):
        return RiskTier.MEDIUM
    if tool == "send_message":
        plat = str(params.get("platform") or "").lower()
        if any(k in plat for k in ("whatsapp", "wp", "wapp", "telegram", "chat")):
            return RiskTier.MEDIUM
        return RiskTier.MEDIUM

    # 7. Antigravity IDE Agent
    if tool == "antigravity_ide":
        if action in ("send_prompt", "prompt"):
            return RiskTier.MEDIUM
        if action in ("accept_all", "reject_all"):
            return RiskTier.HIGH
        if action in ("git_push", "push"):
            return RiskTier.MEDIUM
        return RiskTier.LOW

    # 8. Closing External Applications -> MEDIUM
    if action in ("close_app", "close_window", "kill_app", "kill_process", "terminate_process"):
        return RiskTier.MEDIUM

    # 9. Files & Folders
    if tool in ("file_controller", "file_processor", "dev_agent", "create_file", "write_file", "page_to_file", "copy_page_text_to_clipboard"):
        if tool == "copy_page_text_to_clipboard" or action in ("copy_page_text_to_clipboard", "copy_to_clipboard"):
            return RiskTier.LOW
        if tool == "page_to_file" or action in ("page_to_file", "create_file", "write", "create_folder", "create", "write_file", "save_file"):
            file_path_str = str(params.get("path") or params.get("filepath") or params.get("filename") or params.get("destination") or "")
            folder_str = str(params.get("folder") or "")
            is_overwrite = bool(params.get("overwrite", False))
            if file_path_str:
                try:
                    from pathlib import Path
                    p = Path(file_path_str).expanduser()
                    if folder_str and not p.is_absolute():
                        p = Path(folder_str).expanduser() / p
                    if p.exists() or is_overwrite:
                        return RiskTier.MEDIUM
                    return RiskTier.LOW
                except Exception:
                    pass
            if not is_overwrite:
                return RiskTier.LOW
            return RiskTier.MEDIUM
        if action in ("edit_file", "rename", "move", "organize_desktop"):
            return RiskTier.MEDIUM
        if action in ("delete", "delete_file", "delete_folder", "remove", "empty_recycle_bin", "empty_trash"):
            return RiskTier.HIGH
        if action in ("download", "download_file"):
            return RiskTier.MEDIUM

    # 9b. UI Primitives & Keyboard Shortcuts (Ctrl+A, Ctrl+C, Ctrl+V, clicking, scrolling, focus)
    UI_PRIMITIVE_ACTIONS = {
        "hotkey", "press", "press_key", "key", "key_combination", "shortcut",
        "click", "double_click", "right_click", "middle_click", "mouse_down", "mouse_up",
        "move", "mouse_move", "scroll", "scroll_page", "scroll_element",
        "type", "write", "paste", "copy", "select_all", "drag", "wait", "sleep",
        "screenshot", "get_cursor_pos", "find_elements", "open_file_explorer_item",
        "type_text", "focus", "select", "inspect", "get_text", "read", "uia_open",
    }
    if tool in ("computer_control", "windows_uia", "click_element", "input_text", "scroll_page", "scroll_element", "press_key"):
        if action in ("cmd", "shell", "run", "terminal", "execute", "run_command", "powershell", "execute_code", "execute_script"):
            return RiskTier.HIGH
        if action in UI_PRIMITIVE_ACTIONS or not action:
            return RiskTier.LOW

    # 10. System Power / PC Control -> HIGH
    if action in (
        "shutdown",
        "shutdown_computer",
        "shutdown_laptop",
        "restart",
        "restart_computer",
        "restart_laptop",
        "reboot",
        "hibernate",
        "hibernate_laptop",
        "sleep",
        "sleep_laptop",
        "sleep_computer",
    ) and tool not in ("jarvis_control", "sleep_jarvis"):
        return RiskTier.HIGH

    # 11. Security & Administrative Settings -> HIGH
    if action in (
        "change_security_settings",
        "security_settings",
        "disable_firewall",
        "antivirus_settings",
        "change_admin_settings",
        "admin_settings",
        "uac_settings",
        "delete_system_files",
        "delete_system_file",
        "set_airplane_mode",
        "set_network_config",
        "change_user_settings",
    ):
        return RiskTier.HIGH

    # 12. Shell / Code Execution -> HIGH
    if tool in ("computer_control", "terminal", "powershell", "cmd") and action in (
        "cmd",
        "shell",
        "run",
        "terminal",
        "execute",
        "run_command",
        "powershell",
    ):
        return RiskTier.HIGH
    if action in ("run_command", "run_terminal_command", "execute_code", "execute_script"):
        return RiskTier.HIGH

    # 13. Send Email -> HIGH
    if tool in ("send_email", "email_sender") or action in ("send_email", "email"):
        return RiskTier.HIGH

    # 14. Unknown tools default to MEDIUM (fail-closed principle)
    return RiskTier.MEDIUM


def _load_user_tier_overrides() -> Dict[str, RiskTier]:
    """Load user-configured tier adjustments from config under 'confirmation_tiers'."""
    try:
        from memory.config_manager import load_api_keys

        cfg = load_api_keys() or {}
        raw_tiers = cfg.get("confirmation_tiers", {})
        if not isinstance(raw_tiers, dict):
            return {}
        result: Dict[str, RiskTier] = {}
        for k, v in raw_tiers.items():
            parsed = parse_tier(v)
            if parsed is not None:
                result[str(k).strip().lower()] = parsed
        return result
    except Exception:
        return {}


def get_action_tier(
    tool_name: str,
    parameters: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> RiskTier:
    """
    Authoritatively resolve the risk tier for an action.
    Order of precedence:
      1. Default code-level mapping.
      2. User config overrides (NEVER allowing destructive operations to LOW).
      3. Dynamic context rules (can ONLY raise the tier, NEVER lower it).
    Model prompt text CANNOT override or choose the tier.
    """
    tool = str(tool_name or "").strip().lower()
    params = dict(parameters or {})
    ctx = dict(context or {})
    action = str(params.get("action") or "").strip().lower()

    # 1. Baseline classification
    tier = _classify_default_tier(tool, params)

    # 2. Config overrides
    overrides = _load_user_tier_overrides()
    override_tier = overrides.get(f"{tool}:{action}") or overrides.get(tool)
    if override_tier is not None:
        if override_tier == RiskTier.LOW and is_destructive_protected(tool, action):
            # Prohibit moving destructive operations to LOW
            pass
        else:
            tier = override_tier

    # 3. Dynamic Context Elevation Rules (can ONLY raise tier, NEVER lower it)

    # Context Rule A: Multiple recipients -> HIGH
    recipients = params.get("recipients") or params.get("receivers") or params.get("contacts")
    if isinstance(recipients, (list, tuple, set)) and len(recipients) > 1:
        tier = elevate_tier(tier, RiskTier.HIGH)
    elif isinstance(recipients, str) and ("," in recipients or ";" in recipients):
        tier = elevate_tier(tier, RiskTier.HIGH)
    contact_str = str(params.get("contact") or params.get("receiver") or params.get("recipient") or "")
    if "," in contact_str or ";" in contact_str:
        tier = elevate_tier(tier, RiskTier.HIGH)

    # Context Rule B: Untrusted content -> HIGH
    text_content = str(
        params.get("text")
        or params.get("message")
        or params.get("message_text")
        or params.get("body")
        or params.get("prompt")
        or ""
    )
    if (
        "<<<UNTRUSTED>>>" in text_content
        or ctx.get("untrusted") is True
        or ctx.get("untrusted_content") is True
        or ctx.get("from_untrusted_source") is True
        or params.get("untrusted") is True
        or params.get("untrusted_content") is True
    ):
        tier = elevate_tier(tier, RiskTier.HIGH)

    # Context Rule C: Destructive scope ("all", "everything", "recursive", "force", "wipe") -> HIGH
    scope_str = str(params.get("scope") or params.get("target") or params.get("name") or "").lower()
    if (
        params.get("all") is True
        or params.get("recursive") is True
        or params.get("force") is True
        or scope_str in ("all", "everything")
        or any(w in scope_str for w in ("all files", "delete all", "wipe", "format"))
    ):
        if any(w in f"{tool} {action}" for w in ("delete", "remove", "close", "clean", "empty", "format")):
            tier = elevate_tier(tier, RiskTier.HIGH)

    # Context Rule D: Closing display with unsaved content -> HIGH
    if (tool == "display_window" or tool == "jarvis_control") and action in ("close_display", "close"):
        if (
            params.get("unsaved") is True
            or ctx.get("unsaved_content") is True
            or ctx.get("has_unsaved_content") is True
            or params.get("has_unsaved_content") is True
        ):
            tier = elevate_tier(tier, RiskTier.HIGH)

    # Context Rule E: First message to a new contact -> at least MEDIUM
    if tool in ("whatsapp_send", "whatsapp", "send_message", "telegram"):
        if ctx.get("is_first_message") is True or params.get("first_contact") is True:
            tier = elevate_tier(tier, RiskTier.MEDIUM)

    return tier
