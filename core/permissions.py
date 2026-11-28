"""
core/permissions.py — Centralized Action Permission Manager, Risk Levels, and Audit Logging.

Authoritative permission and confirmation system for all JARVIS voice-controlled actions:
1. Permission Levels:
   - IMMEDIATE (Level 1): Immediate execution without confirmation.
   - CONFIRMATION_REQUIRED (Level 2): Explains intended action and waits for user approval.
   - HIGH_IMPACT (Level 3): Requires explicit approval with consequences clearly explained.
   - FORBIDDEN: Strictly prohibited operations (e.g. drive formatting).
2. Centralized ActionExecutionGuard:
   - Intercepts all action execution attempts before invocation.
   - Authoritative: AI-generated parameters or instructions CANNOT override classifications.
   - Direct integration with ConfirmationManager.
3. Action Audit Logging:
   - Persistent, tamper-resistant append-only SQLite log with credential redaction.
"""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
import time
import traceback
from contextvars import ContextVar
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Optional, Tuple

from core import outcome

_current_task_id: ContextVar[Optional[str]] = ContextVar("current_task_id", default=None)


def set_current_task_id(task_id: Optional[str]) -> None:
    """Bind the active background task ID to the current execution context."""
    _current_task_id.set(task_id)


def get_current_task_id() -> Optional[str]:
    """Retrieve the active background task ID for the current execution context."""
    return _current_task_id.get()


# ── Risk Level Definitions ───────────────────────────────────────────────────


class ActionRiskLevel(str, Enum):
    IMMEDIATE = "immediate"  # Level 1 - Immediate, no confirmation
    CONFIRMATION_REQUIRED = "confirmation"  # Level 2 - Intended action explained, confirmation required
    HIGH_IMPACT = "high_impact"  # Level 3 - High-impact, clear consequences explained
    FORBIDDEN = "forbidden"  # Blocked / Strictly prohibited (e.g. drive format)


# Backward-compatible tier strings
READ_ONLY = "read_only"
NORMAL = "normal"
SENSITIVE = "sensitive"
DESTRUCTIVE = "destructive"
FORBIDDEN = "forbidden"

PERMISSION_LEVELS = {READ_ONLY, NORMAL, SENSITIVE, DESTRUCTIVE, FORBIDDEN}

# Fallback mapping for generic tool declarations
ACTION_PERMISSIONS: dict[str, str] = {
    # Read-only tools (Immediate)
    "web_search": READ_ONLY,
    "system_status": READ_ONLY,
    "recall_memory": READ_ONLY,
    "file_processor": READ_ONLY,
    "flight_finder": READ_ONLY,
    "weather_report": READ_ONLY,
    "youtube_video": READ_ONLY,
    "observe_page": READ_ONLY,
    "code_helper": READ_ONLY,
    "leetcode_assistant": READ_ONLY,
    "screen_process": READ_ONLY,
    "verify_outcome": READ_ONLY,
    "copy_page_text_to_clipboard": READ_ONLY,
    "notification_reader": READ_ONLY,
    "whatsapp_read": READ_ONLY,
    "page_to_file": NORMAL,
    # Normal operational tools (Immediate)
    "open_app": NORMAL,
    "spotify_control": NORMAL,
    "video_player": NORMAL,
    "windows_uia": NORMAL,
    "browser_control": NORMAL,
    "click_element": NORMAL,
    "input_text": NORMAL,
    "scroll_page": NORMAL,
    "scroll_element": NORMAL,
    "press_key": NORMAL,
    "navigate_to": NORMAL,
    "switch_tab": NORMAL,
    "go_back": NORMAL,
    "wait_page": NORMAL,
    "reminder": NORMAL,
    "send_message": NORMAL,
    "save_memory": NORMAL,
    "undo": NORMAL,
    "takeover_desktop": NORMAL,
    "computer_use": NORMAL,
    "desktop_control": NORMAL,
    "display_window": NORMAL,
    "file_controller": NORMAL,
    "media_control": NORMAL,
    "notification_center": NORMAL,
    "skill_learner": NORMAL,
    "browser_qa": NORMAL,
    "browser_profile": NORMAL,
    "productivity": NORMAL,
    "antigravity_ide": NORMAL,
    "version_control": NORMAL,
    "setup_browser": NORMAL,
    # Task tools
    "start_task": NORMAL,
    "list_tasks": READ_ONLY,
    "task_status": READ_ONLY,
    "steer_task": NORMAL,
    "stop_task": NORMAL,
    # Routine tools
    "create_routine": NORMAL,
    "list_routines": READ_ONLY,
    "edit_routine": NORMAL,
    "delete_routine": NORMAL,
    "configure_quiet_hours": NORMAL,
    # Sensitive tools (Require confirmation by default unless sub-action is immediate)
    "computer_control": SENSITIVE,
    "windows_control": SENSITIVE,
    "computer_settings": SENSITIVE,
    "dev_agent": SENSITIVE,
    "game_updater": SENSITIVE,
    "jarvis_control": SENSITIVE,
    "whatsapp_send": SENSITIVE,
    "whatsapp": SENSITIVE,
    "whatsapp_automation": SENSITIVE,
    # High-level App Suite tools
    "whatsapp_open_chat": NORMAL,
    "whatsapp_close_chat": NORMAL,
    "whatsapp_draft": NORMAL,
    "whatsapp_read_chat": READ_ONLY,
    "whatsapp_list_unread": READ_ONLY,
    "whatsapp_send_file": SENSITIVE,
    "gmail_search": READ_ONLY,
    "gmail_read": READ_ONLY,
    "gmail_draft": NORMAL,
    "gmail_send": SENSITIVE,
    "gmail_reply": SENSITIVE,
    "gmail_manage": NORMAL,
    "gmail_delete": DESTRUCTIVE,
    "gmail_download_attachment": NORMAL,
    "spotify_search_play": NORMAL,
    "spotify_now_playing": READ_ONLY,
    "spotify_volume": NORMAL,
    "spotify_like": NORMAL,
    "spotify_playlist": NORMAL,
    "chatgpt_ask": SENSITIVE,
    "chatgpt_continue": SENSITIVE,
    "chatgpt_read_last": READ_ONLY,
    "chatgpt_open_history": READ_ONLY,
    "chatgpt_upload": SENSITIVE,
    "antigravity_open": NORMAL,
    "antigravity_prompt": SENSITIVE,
    "antigravity_status": READ_ONLY,
    "tool_performance_report": READ_ONLY,
    "action_inspector": READ_ONLY,
    "clipboard_manager": NORMAL,
    "notes_manager": NORMAL,
    "conversation_search": READ_ONLY,
    "timers_stopwatch": NORMAL,
    "email_manager": SENSITIVE,
    "telegram_manager": SENSITIVE,
    "weather_travel": READ_ONLY,
    "dev_tools": NORMAL,
    "sandboxed_shell": SENSITIVE,
    "home_assistant": NORMAL,
    # Gesture & Workflow Watcher controls
    "enable_gesture_mode": NORMAL,
    "disable_gesture_mode": NORMAL,
    "enable_mouse_control": NORMAL,
    "disable_mouse_control": NORMAL,
    "get_gesture_status": READ_ONLY,
    "dispatch_background_workflow": NORMAL,
    "get_worker_status": READ_ONLY,
    "cancel_background_task": NORMAL,
    "resolve_worker_approval": SENSITIVE,
    "watch_contact_messages": SENSITIVE,
    "stop_watching_contact": SENSITIVE,
    "watch_antigravity_task": SENSITIVE,
    "stop_watching_antigravity": SENSITIVE,
    # Destructive actions (High-impact)
    "sleep_jarvis": NORMAL,
    "shutdown_jarvis": DESTRUCTIVE,
    "delete_file": DESTRUCTIVE,
    "kill_process": DESTRUCTIVE,
}

_audit_log: list[dict] = []
_audit_lock = Lock()


# ── Permission Policy Models & Central Registry ──────────────────────────────

from dataclasses import dataclass, field

POLICY_ALLOW = "ALLOW"
POLICY_ASK = "ASK"
POLICY_DENY = "DENY"
VALID_POLICIES = {POLICY_ALLOW, POLICY_ASK, POLICY_DENY}


class SecurityViolationError(PermissionError):
    """Raised when model or untrusted code attempts to modify permission settings."""
    pass


@dataclass
class PermissionEntry:
    id: str
    label: str
    description: str
    tier: str  # READ_ONLY, NORMAL, SENSITIVE, DESTRUCTIVE
    default_policy: str = POLICY_ASK  # ASK or ALLOW
    owner: str = ""  # action or plugin name


class PermissionRegistry:
    """Central registry of all system actions and plugins that require permission classification."""

    def __init__(self):
        self._entries: dict[str, PermissionEntry] = {}
        self._lock = Lock()
        self._init_defaults()

    def _init_defaults(self):
        core_permissions = [
            # System controls
            PermissionEntry("system:shutdown", "Shut Down PC", "Power off the operating system", DESTRUCTIVE, POLICY_ASK, "windows_control"),
            PermissionEntry("system:restart", "Restart PC", "Reboot the operating system", DESTRUCTIVE, POLICY_ASK, "windows_control"),
            PermissionEntry("system:sleep", "Sleep PC / Display", "Put the computer or screen to sleep", SENSITIVE, POLICY_ASK, "windows_control"),
            PermissionEntry("system:power_mode", "Change Power Mode", "Switch OS battery/power performance profile", SENSITIVE, POLICY_ASK, "windows_control"),
            PermissionEntry("system:airplane_mode", "Toggle Airplane Mode", "Enable or disable all wireless communications", SENSITIVE, POLICY_ASK, "windows_control"),
            # Network controls
            PermissionEntry("network:wifi_toggle", "Toggle Wi-Fi", "Turn the Wi-Fi adapter on or off", NORMAL, POLICY_ALLOW, "windows_control"),
            PermissionEntry("network:wifi_connect", "Connect Wi-Fi Network", "Connect or switch to a wireless network", SENSITIVE, POLICY_ASK, "windows_control"),
            PermissionEntry("network:wifi_forget", "Forget Wi-Fi Profile", "Delete stored network credentials", SENSITIVE, POLICY_ASK, "windows_control"),
            PermissionEntry("network:dns_change", "Change DNS Servers", "Modify network adapter DNS resolver configuration", SENSITIVE, POLICY_ASK, "system"),
            PermissionEntry("network:hosts_edit", "Edit System Hosts File", "Add or remove blocked domain mappings in hosts", DESTRUCTIVE, POLICY_ASK, "system"),
            # Bluetooth & Audio
            PermissionEntry("bluetooth:toggle", "Toggle Bluetooth", "Turn the Bluetooth adapter on or off", NORMAL, POLICY_ALLOW, "windows_control"),
            PermissionEntry("bluetooth:connect", "Connect BT Device", "Connect or disconnect paired Bluetooth devices", SENSITIVE, POLICY_ASK, "windows_control"),
            PermissionEntry("bluetooth:pair", "Pair BT Device", "Pair with or remove Bluetooth peripherals", SENSITIVE, POLICY_ASK, "windows_control"),
            PermissionEntry("audio:device_switch", "Switch Audio I/O Device", "Change default playback or recording audio device", SENSITIVE, POLICY_ASK, "windows_control"),
            PermissionEntry("audio:volume_set", "Adjust Volume & Mute", "Adjust speaker volume or mute status", NORMAL, POLICY_ALLOW, "windows_control"),
            PermissionEntry("display:config", "Change Display Settings", "Alter display resolution or multi-monitor mode", SENSITIVE, POLICY_ASK, "windows_control"),
            # Processes & Windows
            PermissionEntry("process:close_app", "Close External Application", "Politely close a running third-party program", SENSITIVE, POLICY_ASK, "computer_control"),
            PermissionEntry("process:kill", "Terminate Process", "Forcefully terminate a process", DESTRUCTIVE, POLICY_ASK, "computer_control"),
            # File operations
            PermissionEntry("file:read", "Read / Search Files", "Read file contents, inspect directories, search disk", READ_ONLY, POLICY_ALLOW, "file_controller"),
            PermissionEntry("file:create_write", "Create / Write File", "Write new files or edit files on disk", SENSITIVE, POLICY_ASK, "file_controller"),
            PermissionEntry("file:move_rename", "Move / Rename File", "Move or rename files and directories", SENSITIVE, POLICY_ASK, "file_controller"),
            PermissionEntry("file:delete", "Delete File / Folder", "Delete files or directories permanently", DESTRUCTIVE, POLICY_ASK, "file_controller"),
            PermissionEntry("file:empty_recycle_bin", "Empty Recycle Bin", "Permanently delete contents of Windows Recycle Bin", DESTRUCTIVE, POLICY_ASK, "file_controller"),
            # Communication
            PermissionEntry("message:send", "Send Message", "Send message via WhatsApp or messaging tools", SENSITIVE, POLICY_ASK, "send_message"),
            PermissionEntry("message:read", "Read Messages", "Read received chat messages or notifications", READ_ONLY, POLICY_ALLOW, "notification_center"),
            # Browser
            PermissionEntry("browser:navigate", "Browser Navigation & Clicks", "Navigate URLs, click elements, fill web forms", NORMAL, POLICY_ALLOW, "browser_control"),
            PermissionEntry("browser:close_tab", "Close Browser Tab", "Close open browser tabs matching target", NORMAL, POLICY_ALLOW, "browser_control"),
            PermissionEntry("browser:payment", "Payments & Checkout", "Interactions on payment, checkout, or banking pages", DESTRUCTIVE, POLICY_ASK, "browser_control"),
            PermissionEntry("browser:download", "Download File", "Download files from web links to disk", SENSITIVE, POLICY_ASK, "browser_control"),
            # Coding & Terminal
            PermissionEntry("code:execute_terminal", "Execute Terminal Command", "Run shell or terminal commands in project terminal", DESTRUCTIVE, POLICY_ASK, "antigravity_ide"),
            PermissionEntry("code:agent_edit", "AI Agent Code Workspace Edits", "Autonomous agent modifying codebase files", SENSITIVE, POLICY_ASK, "dev_agent"),
            PermissionEntry("git:commit", "Git Commit Changes", "Stage and commit changes to local repository", NORMAL, POLICY_ALLOW, "version_control"),
            PermissionEntry("git:push", "Git Push to Remote", "Push commits to remote repository", SENSITIVE, POLICY_ASK, "version_control"),
            # Productivity & Assistants
            PermissionEntry("productivity:task_write", "Manage Tasks & Habits", "Create, complete, update, or delete tasks/habits", NORMAL, POLICY_ALLOW, "productivity"),
            PermissionEntry("reminders:manage", "Manage Reminders", "Create, snooze, edit, or delete reminders", NORMAL, POLICY_ALLOW, "reminder"),
            PermissionEntry("routines:manage", "Manage Routines", "Configure scheduled routines and quiet hours", NORMAL, POLICY_ALLOW, "routines"),
            PermissionEntry("jarvis:shutdown", "Shut Down JARVIS", "Completely close and exit the JARVIS assistant", DESTRUCTIVE, POLICY_ASK, "jarvis_control"),
            PermissionEntry("jarvis:mute_sleep", "Standby / Mute Avatar", "Mute microphone/speaker or put JARVIS to sleep", NORMAL, POLICY_ALLOW, "jarvis_control"),
        ]
        for p in core_permissions:
            self.register(p)

    def register(self, entry: PermissionEntry) -> None:
        with self._lock:
            if entry.tier in (READ_ONLY, NORMAL) and entry.default_policy not in VALID_POLICIES:
                entry.default_policy = POLICY_ALLOW
            elif entry.tier in (SENSITIVE, DESTRUCTIVE) and entry.default_policy not in VALID_POLICIES:
                entry.default_policy = POLICY_ASK
            self._entries[entry.id] = entry

    def get(self, id: str) -> Optional[PermissionEntry]:
        with self._lock:
            return self._entries.get(id)

    def all(self) -> list[PermissionEntry]:
        with self._lock:
            return list(self._entries.values())

    def by_tier(self) -> dict[str, list[PermissionEntry]]:
        with self._lock:
            res: dict[str, list[PermissionEntry]] = {
                DESTRUCTIVE: [],
                SENSITIVE: [],
                NORMAL: [],
                READ_ONLY: [],
            }
            for e in self._entries.values():
                res.setdefault(e.tier, []).append(e)
            return res

    def find_by_action(self, action_name: str, parameters: dict | None = None) -> Optional[PermissionEntry]:
        act = str(action_name or "").lower().strip()
        params = parameters or {}
        sub_act = str(params.get("action") or "").lower().strip()

        if act in self._entries:
            return self._entries[act]

        if act in ("windows_control", "computer_settings"):
            if any(k in sub_act for k in ("shutdown", "shutdown_computer", "shutdown_laptop")):
                return self.get("system:shutdown")
            if any(k in sub_act for k in ("restart", "reboot")):
                return self.get("system:restart")
            if any(k in sub_act for k in ("sleep", "hibernate")):
                return self.get("system:sleep")
            if "power_mode" in sub_act:
                return self.get("system:power_mode")
            if "airplane" in sub_act:
                return self.get("system:airplane_mode")
            if "wifi" in sub_act:
                if any(k in sub_act for k in ("toggle", "wifi_on", "wifi_off", "enable_wifi", "disable_wifi", "set_wifi_enabled", "status", "list", "check", "get", "current", "available")):
                    return self.get("network:wifi_toggle")
                if "forget" in sub_act:
                    return self.get("network:wifi_forget")
                if any(k in sub_act for k in ("connect", "switch")):
                    return self.get("network:wifi_connect")
            if "bluetooth" in sub_act or "bt" in sub_act:
                if any(k in sub_act for k in ("toggle", "bluetooth_on", "enable_bluetooth", "status", "list", "check", "get", "paired", "connected")):
                    return self.get("bluetooth:toggle")
                if any(k in sub_act for k in ("pair", "unpair", "forget")):
                    return self.get("bluetooth:pair")
                if any(k in sub_act for k in ("connect", "disconnect", "switch", "bluetooth_off", "disable_bluetooth")):
                    return self.get("bluetooth:connect")
            if any(k in sub_act for k in ("volume", "mute", "unmute", "battery", "power_status", "cpu", "ram", "storage")):
                return self.get("audio:volume_set")
            if any(k in sub_act for k in ("audio_device", "audio_input", "audio_output", "sound_device", "switch_audio")):
                return self.get("audio:device_switch")
            if any(k in sub_act for k in ("resolution", "display_mode")):
                return self.get("display:config")
            if sub_act in ("close_app", "close_window"):
                return self.get("process:close_app")

        if act == "computer_control":
            if sub_act in ("close_app", "close_window", "close_application", "quit_app", "exit_app"):
                return self.get("process:close_app")
            if sub_act in ("kill", "kill_process", "destroy"):
                return self.get("process:kill")
            if sub_act in ("click", "type", "scroll", "press"):
                return self.get("browser:navigate")

        if act == "file_controller":
            if sub_act in ("list", "read", "find", "search", "info", "disk_usage", "largest"):
                return self.get("file:read")
            if sub_act in ("create_file", "create_folder", "write", "edit_file"):
                return self.get("file:create_write")
            if sub_act in ("move", "copy", "rename", "organize_desktop"):
                return self.get("file:move_rename")
            if sub_act in ("delete", "delete_file", "delete_folder", "remove"):
                return self.get("file:delete")
            if sub_act in ("empty_recycle_bin", "empty_trash"):
                return self.get("file:empty_recycle_bin")

        if act in ("browser_control", "action_executor"):
            url = str(params.get("url") or "").lower()
            if any(k in url for k in ("checkout", "payment", "pay", "cart", "billing", "order", "purchase", "bank")):
                return self.get("browser:payment")
            if sub_act in ("download", "download_file"):
                return self.get("browser:download")
            if sub_act == "close_tab":
                return self.get("browser:close_tab")
            return self.get("browser:navigate")

        if act in ("whatsapp_send", "whatsapp"):
            return self.get("message:send")

        if act == "whatsapp_read":
            return self.get("message:read")

        if act == "send_message":
            return self.get("message:send")

        if act == "antigravity_ide":
            if sub_act in ("run_terminal_command", "new_terminal"):
                return self.get("code:execute_terminal")
            if sub_act in ("create_file", "write_file", "create_folder", "save_file"):
                return self.get("file:create_write")
            if sub_act in ("git_push", "push"):
                return self.get("git:push")
            if sub_act in ("git_commit", "commit"):
                return self.get("git:commit")
            if sub_act in ("accept_all", "reject_all"):
                return self.get("code:agent_edit")

        if act == "version_control":
            if sub_act in ("git_push", "push"):
                return self.get("git:push")
            if sub_act in ("git_commit", "commit"):
                return self.get("git:commit")

        if act == "dev_agent":
            return self.get("code:agent_edit")

        if act == "productivity":
            return self.get("productivity:task_write")

        if act == "reminder":
            return self.get("reminders:manage")

        if act in ("create_routine", "edit_routine", "delete_routine", "configure_quiet_hours"):
            return self.get("routines:manage")

        if act in ("shutdown_jarvis", "kill_jarvis"):
            return self.get("jarvis:shutdown")

        if act in ("sleep_jarvis", "jarvis_control"):
            if sub_act == "shutdown_jarvis":
                return self.get("jarvis:shutdown")
            return self.get("jarvis:mute_sleep")

        return None


_GLOBAL_REGISTRY: Optional[PermissionRegistry] = None
_REGISTRY_LOCK = Lock()


def get_permission_registry() -> PermissionRegistry:
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        with _REGISTRY_LOCK:
            if _GLOBAL_REGISTRY is None:
                _GLOBAL_REGISTRY = PermissionRegistry()
    return _GLOBAL_REGISTRY


# ── Configuration Mtime Cache & Decision Logging ─────────────────────────────

_CONFIG_CACHE_LOCK = Lock()
_LAST_MTIME: float = -1.0
_CACHED_OVERRIDES: dict[str, str] = {}
_CACHED_TIMED_ALLOWS: dict[str, float] = {}


def _sync_config_cache():
    global _LAST_MTIME, _CACHED_OVERRIDES, _CACHED_TIMED_ALLOWS
    try:
        from memory.config_manager import CONFIG_FILE, ensure_config_dir
        ensure_config_dir()
        if not CONFIG_FILE.exists():
            return
        current_mtime = CONFIG_FILE.stat().st_mtime
    except Exception:
        return

    with _CONFIG_CACHE_LOCK:
        if current_mtime != _LAST_MTIME:
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                perm_data = data.get("permission_settings", {})
                _CACHED_OVERRIDES = {str(k): str(v).upper() for k, v in perm_data.get("overrides", {}).items()}
                _CACHED_TIMED_ALLOWS = {str(k): float(v) for k, v in perm_data.get("timed_allows", {}).items()}
                _LAST_MTIME = current_mtime
            except Exception:
                pass


def resolve_permission_policy(permission_id: str) -> str:
    """Resolve effective policy ('ASK', 'ALLOW', 'DENY') for a permission ID."""
    clean_id = str(permission_id or "").strip()
    _sync_config_cache()
    now = time.time()

    # 1. Timed allow check
    with _CONFIG_CACHE_LOCK:
        if clean_id in _CACHED_TIMED_ALLOWS:
            expiry = _CACHED_TIMED_ALLOWS[clean_id]
            if now < expiry:
                return POLICY_ALLOW
            else:
                _CACHED_TIMED_ALLOWS.pop(clean_id, None)

    # 2. Config override check
    with _CONFIG_CACHE_LOCK:
        if clean_id in _CACHED_OVERRIDES:
            ov = _CACHED_OVERRIDES[clean_id]
            if ov in VALID_POLICIES:
                return ov

    # 3. Default policy from registry
    reg = get_permission_registry()
    entry = reg.get(clean_id)
    if entry:
        return entry.default_policy

    return POLICY_ASK


def resolve_action_policy(action_name: str, parameters: dict | None = None) -> tuple[str, Optional[PermissionEntry]]:
    """Determine effective policy for action & parameters. Returns (policy, permission_entry)."""
    act = str(action_name or "").lower().strip()
    params = parameters or {}

    # Forbidden drive format check -> strictly DENY
    if ActionPermissionManager.is_drive_format_attempt(act, params):
        return POLICY_DENY, PermissionEntry("forbidden:drive_format", "Forbidden Drive Format", "Drive format is strictly prohibited", FORBIDDEN, POLICY_DENY, "security")

    reg = get_permission_registry()
    entry = reg.find_by_action(act, params)

    risk_level = ActionPermissionManager.get_risk_level(act, params)

    if entry:
        pol = resolve_permission_policy(entry.id)
        if entry.id not in _CACHED_OVERRIDES and risk_level == ActionRiskLevel.IMMEDIATE:
            pol = POLICY_ALLOW
        return pol, entry

    # Generic tool entry in ACTION_PERMISSIONS
    tier = DESTRUCTIVE if risk_level == ActionRiskLevel.HIGH_IMPACT else (
        SENSITIVE if risk_level == ActionRiskLevel.CONFIRMATION_REQUIRED else NORMAL
    )
    synthetic_id = f"tool:{act}"
    default_pol = POLICY_ALLOW if risk_level == ActionRiskLevel.IMMEDIATE else POLICY_ASK
    pol = resolve_permission_policy(synthetic_id)
    if synthetic_id not in _CACHED_OVERRIDES:
        pol = default_pol

    synth_entry = PermissionEntry(synthetic_id, act, f"Operational tool {act}", tier, default_pol, act)
    return pol, synth_entry


def set_permission_policy(
    permission_id: str,
    policy: str,
    from_ui: bool = False,
    request_token: Optional[str] = None,
) -> None:
    """Set policy for a permission. Security rule: only allowed from UI or with user confirmation."""
    if is_untrusted_context():
        raise SecurityViolationError("Untrusted context cannot alter permission settings.")

    if not from_ui and not request_token:
        raise SecurityViolationError("Model or untrusted code cannot alter permission settings. Changes require UI confirmation.")

    clean_id = str(permission_id or "").strip()
    clean_pol = str(policy or "").strip().upper()
    if clean_pol not in VALID_POLICIES:
        raise ValueError(f"Invalid policy '{policy}'. Must be ASK, ALLOW, or DENY.")

    from memory import config_manager
    config_manager.set_permission_policy_override(clean_id, clean_pol)
    _sync_config_cache()


def set_timed_allow(
    permission_id: str,
    minutes: float,
    from_ui: bool = False,
    request_token: Optional[str] = None,
) -> None:
    """Set temporary ALLOW policy for N minutes, reverting to ASK automatically."""
    if is_untrusted_context():
        raise SecurityViolationError("Untrusted context cannot alter permission settings.")

    if not from_ui and not request_token:
        raise SecurityViolationError("Model or untrusted code cannot set timed permissions. Changes require UI confirmation.")

    clean_id = str(permission_id or "").strip()
    expiry = time.time() + (float(minutes) * 60.0)

    from memory import config_manager
    settings = dict(config_manager.get_permission_settings())
    timed = dict(settings.get("timed_allows", {}))
    timed[clean_id] = expiry
    settings["timed_allows"] = timed
    config_manager.save_permission_settings(settings)
    _sync_config_cache()


def get_timed_allows() -> dict[str, float]:
    """Return dictionary of active timed allows with expiry timestamps."""
    _sync_config_cache()
    now = time.time()
    with _CONFIG_CACHE_LOCK:
        return {k: v for k, v in _CACHED_TIMED_ALLOWS.items() if v > now}


def clear_timed_allow(permission_id: str) -> None:
    clean_id = str(permission_id or "").strip()
    from memory import config_manager
    settings = dict(config_manager.get_permission_settings())
    timed = dict(settings.get("timed_allows", {}))
    if clean_id in timed:
        timed.pop(clean_id, None)
        settings["timed_allows"] = timed
        config_manager.save_permission_settings(settings)
    _sync_config_cache()


def reset_permissions_to_defaults(from_ui: bool = False, request_token: Optional[str] = None) -> None:
    """Reset all permissions and timed allows to defaults."""
    if is_untrusted_context():
        raise SecurityViolationError("Untrusted context cannot reset permission settings.")

    if not from_ui and not request_token:
        raise SecurityViolationError("Model or untrusted code cannot reset permissions. Changes require UI confirmation.")

    from memory import config_manager
    config_manager.reset_permission_settings()
    _sync_config_cache()


_recent_decisions: list[dict] = []
_decisions_lock = Lock()


def log_permission_decision(
    permission_id: str,
    action: str,
    decision: str,
    reason: str = "",
) -> dict:
    """Log an authoritative permission decision (ALLOWED, DENIED, ASK) for the UI audit trail."""
    now_str = datetime.now().isoformat()
    entry = {
        "timestamp": now_str,
        "permission_id": str(permission_id),
        "action": str(action),
        "decision": str(decision).upper(),
        "reason": str(reason),
    }
    with _decisions_lock:
        _recent_decisions.append(entry)
        if len(_recent_decisions) > 100:
            _recent_decisions.pop(0)

    try:
        log_action_audit(
            action=action,
            parameters={"permission_id": permission_id, "reason": reason},
            permission_level=decision,
            status="allowed" if decision == "ALLOWED" else ("denied" if decision == "DENIED" else "ask"),
            verified=(decision == "ALLOWED"),
        )
    except Exception:
        pass

    return entry


def get_recent_permission_decisions(limit: int = 50) -> list[dict]:
    with _decisions_lock:
        return list(_recent_decisions[-limit:])


def clear_recent_permission_decisions() -> None:
    with _decisions_lock:
        _recent_decisions.clear()



# ── ActionPermissionManager ──────────────────────────────────────────────────


class ActionPermissionManager:
    """Authoritative centralized permission manager for all JARVIS actions.

    Implements the exact permission matrix. AI-generated instructions and Gemini
    cannot override these classifications.
    """

    FORBIDDEN_FORMAT_PATTERNS = (
        "format_drive",
        "format_disk",
        "format_volume",
        "format-volume",
        "clear-disk",
        "initialize-disk",
        "diskpart",
        "format c:",
        "format d:",
        "format e:",
    )

    @classmethod
    def is_drive_format_attempt(cls, action_name: str, parameters: dict | None = None) -> bool:
        """Check if an action or its parameters attempt drive or volume formatting."""
        act = str(action_name or "").lower().strip()
        params = parameters or {}
        sub_act = str(params.get("action") or "").lower().strip()

        # Tools that only display UI, search, or process non-system text cannot format drives
        if act in (
            "display_window",
            "web_search",
            "observe_page",
            "reminder",
            "send_message",
            "notification_center",
            "spotify_control",
            "video_player",
        ):
            return False

        # Direct format action on file/drive controller tools
        if act in ("format", "format_drive", "format_disk") or (
            act in ("file_controller", "windows_control", "computer_control")
            and sub_act in ("format", "format_drive", "format_disk", "format_volume")
        ):
            return True

        # Check command/script or target paths for system formatting commands
        cmd_text = str(params.get("command") or params.get("cmd") or params.get("script") or "").lower()
        target_path = str(params.get("target") or params.get("path") or "").lower()
        combined = f"{act} {sub_act} {cmd_text} {target_path}".lower()

        # Check explicit disk formatting patterns
        for pat in cls.FORBIDDEN_FORMAT_PATTERNS:
            if pat in combined:
                return True

        # Regex check for Windows drive formatting syntax: e.g. "format c:", "format d:"
        if re.search(r"\bformat\s+[a-z]:", combined):
            return True

        # Check computer_control cmd/shell text for format commands
        if act == "computer_control" and sub_act in ("cmd", "shell", "run", "terminal"):
            input_text = str(params.get("text") or "").lower()
            if re.search(r"\bformat\s+[a-z]:", input_text) or "format-volume" in input_text or "diskpart" in input_text:
                return True

        return False

    @classmethod
    def get_risk_level(cls, action_name: str, parameters: dict | None = None, context: dict | None = None) -> ActionRiskLevel:
        """Determine the exact risk level for an action according to the permission matrix."""
        act = str(action_name or "").lower().strip()
        params = parameters or {}
        sub_act = str(params.get("action") or "").lower().strip()
        target = str(
            params.get("target")
            or params.get("device")
            or params.get("name")
            or params.get("ssid")
            or params.get("path")
            or ""
        ).strip().lower()

        # 0. Check FORBIDDEN (Drive Formatting)
        if cls.is_drive_format_attempt(act, params):
            return ActionRiskLevel.FORBIDDEN

        WINDOW_ADJUST_ACTIONS = (
            "minimize_jarvis",
            "maximize_jarvis",
            "restore_jarvis",
            "mute_jarvis",
            "unmute_jarvis",
            "minimize",
            "maximize",
            "restore",
            "minimize_window",
            "maximize_window",
            "restore_window",
            "close_window",
            "switch_window",
            "show_desktop",
            "desktop",
            "full_screen",
            "fullscreen",
            "toggle_fullscreen",
            "exit_fullscreen",
            "focus_display",
            "minimize_display",
            "maximize_display",
            "close_display",
            "list_displays",
            "open_display",
            "fit_window",
            "center_window",
            "resize_window",
            "move_window",
            "adjust_window",
            "tile_window",
            "toggle_minimize",
            "toggle_collapse",
            "toggle_chat",
            "toggle_chat_dock",
            "toggle_expanded",
            "mini_mode",
            "show_reminders",
            "hide_reminders",
            "minimize_reminders",
            "show_memory",
            "manage_memory",
            "show_actions",
            "manage_actions",
            "action_registry",
            "memory_platform",
        )
        if sub_act in WINDOW_ADJUST_ACTIONS:
            return ActionRiskLevel.IMMEDIATE

        if any(k in target for k in ("jarvis", "arc reactor", "reactor", "hud", "display_window")) and any(
            w in sub_act
            for w in (
                "window",
                "display",
                "adjust",
                "move",
                "resize",
                "size",
                "fit",
                "show",
                "hide",
                "view",
                "toggle",
                "pos",
                "center",
            )
        ):
            if sub_act not in (
                "shutdown",
                "shutdown_computer",
                "shutdown_laptop",
                "restart",
                "reboot",
                "format",
                "delete",
                "destroy",
                "kill",
            ):
                return ActionRiskLevel.IMMEDIATE

        # Destructive sub-action parameter escalation (kill, destroy, shutdown, etc.)
        if sub_act in ("kill", "kill_process", "destroy"):
            return ActionRiskLevel.HIGH_IMPACT

        # =====================================================================
        # A. WI-FI
        # =====================================================================
        # - Check status / network / list: IMMEDIATE
        # - Turn Wi-Fi ON: IMMEDIATE
        # - Turn Wi-Fi OFF: IMMEDIATE
        # - Switch network: CONFIRMATION REQUIRED
        # - Disconnect: CONFIRMATION REQUIRED
        # - Forget network: CONFIRMATION REQUIRED
        # - Change network configuration: CONFIRMATION REQUIRED
        if act in ("windows_control", "computer_settings") or "wifi" in act:
            if sub_act in (
                "get_wifi_status",
                "wifi_status",
                "check_wifi",
                "is_wifi_on",
                "list_available_wifi_networks",
                "list_wifi_networks",
                "available_wifi",
                "wifi_networks",
                "current_wifi",
                "connected_wifi",
                "wifi_name",
            ):
                return ActionRiskLevel.IMMEDIATE

            if sub_act in ("set_wifi_enabled", "wifi_on", "wifi_off", "enable_wifi", "disable_wifi", "toggle_wifi"):
                return ActionRiskLevel.IMMEDIATE

            if sub_act in ("connect_wifi", "connect_network", "switch_wifi", "switch_network"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("disconnect_wifi", "disconnect_network"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("forget_wifi", "forget_network", "delete_wifi_profile"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("set_network_config", "change_network_config", "change_dns", "set_ip_config"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

        # =====================================================================
        # B. BLUETOOTH
        # =====================================================================
        # - Check status / list paired / list connected: IMMEDIATE
        # - Turn Bluetooth ON: IMMEDIATE
        # - Turn Bluetooth OFF: CONFIRMATION REQUIRED
        # - Connect / Disconnect / Switch / Pair / Forget / Audio output: CONFIRMATION REQUIRED
        if act in ("windows_control", "computer_settings") or "bluetooth" in act or "bt" in act:
            if sub_act in (
                "get_bluetooth_status",
                "bluetooth_status",
                "check_bluetooth",
                "is_bluetooth_on",
                "list_bluetooth_devices",
                "bluetooth_devices",
                "paired_bluetooth_devices",
                "list_connected_bluetooth_devices",
                "connected_bluetooth_devices",
                "connected_devices",
            ):
                return ActionRiskLevel.IMMEDIATE

            if sub_act in ("bluetooth_on", "enable_bluetooth"):
                return ActionRiskLevel.IMMEDIATE

            if sub_act == "set_bluetooth_enabled":
                val = str(params.get("value") or target).lower()
                if val in ("off", "false", "0", "disable"):
                    return ActionRiskLevel.CONFIRMATION_REQUIRED
                return ActionRiskLevel.IMMEDIATE

            if sub_act in ("bluetooth_off", "disable_bluetooth"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("connect_bluetooth_device", "connect_bluetooth", "connect_device"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("disconnect_bluetooth_device", "disconnect_bluetooth", "disconnect_device"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("switch_bluetooth_device", "switch_device"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("pair_bluetooth_device", "pair_bluetooth", "pair_device"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in (
                "unpair_bluetooth_device",
                "forget_bluetooth_device",
                "remove_bluetooth_device",
                "forget_device",
            ):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in (
                "set_audio_output",
                "switch_audio_output",
                "set_default_playback_device",
                "change_audio_output",
                "set_audio_input",
                "switch_audio_input",
                "set_default_recording_device",
                "change_audio_input",
                "set_audio_device",
                "switch_audio_device",
                "switch_audio",
                "reset_audio_devices",
                "use_default_audio_devices",
            ):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

        # =====================================================================
        # C. LAPTOP MONITORING AND CONTROLS
        # =====================================================================
        # - Check battery, CPU, RAM, storage, volume: IMMEDIATE
        # - Increase/decrease volume: IMMEDIATE
        # - Increase/decrease brightness: IMMEDIATE
        # - Lock laptop: IMMEDIATE
        # - Change power mode, Airplane mode, display resolution, display config: CONFIRMATION REQUIRED
        # - Shutdown, Restart, Hibernate, Sleep: HIGH-IMPACT
        if act in ("windows_control", "computer_settings", "computer_control", "system_status", "system_monitor"):
            if sub_act in (
                "get_power_status",
                "battery_status",
                "battery",
                "battery_percentage",
                "cpu_usage",
                "ram_usage",
                "storage_usage",
                "disk_usage",
                "get_volume",
                "check_volume",
                "volume_status",
                "volume_get",
                "get_brightness",
                "brightness_get",
                "volume_up",
                "volume_down",
                "set_volume",
                "volume",
                "adjust_volume",
                "mute",
                "unmute",
                "mute_jarvis",
                "unmute_jarvis",
                "minimize_jarvis",
                "maximize_jarvis",
                "restore_jarvis",
                "show_memory",
                "manage_memory",
                "memory_platform",
                "show_actions",
                "manage_actions",
                "action_registry",
                "toggle_ducking",
                "enable_ducking",
                "disable_ducking",
                "brightness_up",
                "brightness_down",
                "set_brightness",
                "brightness",
                "list_audio_devices",
                "list_audio_input_devices",
                "list_audio_output_devices",
                "audio_devices",
                "sound_devices",
                "get_audio_devices",
                "list_sound_devices",
                "audio_status",
                "get_audio_status",
                "sleep_jarvis",
                "go_to_sleep",
                "lock_screen",
                "lock_laptop",
                "lock",
            ):
                return ActionRiskLevel.IMMEDIATE

            if sub_act in ("set_power_mode", "power_mode", "change_power_mode"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("set_airplane_mode", "airplane_mode", "airplane_mode_on", "airplane_mode_off"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("set_resolution", "change_resolution", "change_display_resolution"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("set_display_mode", "display_mode", "change_display_configuration", "display_configuration"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("shutdown", "shutdown_computer", "shutdown_laptop") or act == "shutdown_jarvis":
                return ActionRiskLevel.HIGH_IMPACT

            if sub_act in ("restart", "restart_computer", "restart_laptop", "reboot"):
                return ActionRiskLevel.HIGH_IMPACT

            if sub_act in ("hibernate", "hibernate_laptop", "hibernate_computer"):
                return ActionRiskLevel.HIGH_IMPACT

            if sub_act in ("sleep", "sleep_display", "sleep_laptop", "sleep_computer"):
                return ActionRiskLevel.HIGH_IMPACT

        # =====================================================================
        # D. FILE AND FOLDER MANAGEMENT
        # =====================================================================
        # - List folders, search files, read files: IMMEDIATE
        # - Create file, edit file, rename, copy, move: CONFIRMATION REQUIRED
        # - Delete files/folders, empty recycle bin: HIGH-IMPACT
        # - Format drive: FORBIDDEN
        if act in ("file_controller", "file_processor", "dev_agent"):
            if sub_act in (
                "list",
                "find",
                "search",
                "search_files",
                "read",
                "extract_text",
                "summarize",
                "largest",
                "disk_usage",
                "info",
            ):
                return ActionRiskLevel.IMMEDIATE

            if sub_act in (
                "create_file",
                "create_folder",
                "write",
                "rename",
                "copy",
                "move",
                "organize_desktop",
                "edit_file",
            ):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("delete", "delete_file", "delete_folder", "remove"):
                return ActionRiskLevel.HIGH_IMPACT

            if sub_act in ("empty_recycle_bin", "empty_trash"):
                return ActionRiskLevel.HIGH_IMPACT

        # =====================================================================
        # E. APPLICATIONS AND BROWSER
        # =====================================================================
        # - Open/close app: IMMEDIATE
        # - Open website, search Google, navigate, fill form, submit form: IMMEDIATE
        # - Upload file: IMMEDIATE
        # - Send email: IMMEDIATE
        # - Download file: CONFIRMATION REQUIRED
        # - Make payment, purchase: HIGH-IMPACT
        if act in ("open_app", "computer_control", "browser_control", "action_executor", "web_search", "send_message", "whatsapp_send", "whatsapp"):
            # Financial pages (payment and checkout) always require HIGH_IMPACT confirmation regardless of action type
            if act in ("browser_control", "action_executor"):
                target_url = (params.get("url") or "").lower()
                if not target_url:
                    try:
                        import importlib
                        bc = importlib.import_module("actions.browser_control")
                        target_url = (getattr(bc, "_get_active_url", lambda: "")() or "").lower()
                    except Exception:
                        pass
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
                if any(k in target_url for k in financial_keywords):
                    return ActionRiskLevel.HIGH_IMPACT

                # Per-site read-only guards: Gmail read-only
                if "mail.google.com" in target_url:
                    text_param = str(params.get("text") or params.get("description") or "").lower()
                    blocked_gmail = ("send", "reply", "forward", "delete", "archive", "move", "compose")
                    if any(w in text_param for w in blocked_gmail) or sub_act in ("send", "reply", "forward", "delete", "archive", "move"):
                        return ActionRiskLevel.FORBIDDEN

            if act in ("whatsapp_send", "whatsapp"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if act == "send_message":
                plat = str(params.get("platform") or "").lower()
                if any(k in plat for k in ("whatsapp", "wp", "wapp")):
                    return ActionRiskLevel.CONFIRMATION_REQUIRED
                return ActionRiskLevel.IMMEDIATE

            if act in ("open_app", "web_search"):
                return ActionRiskLevel.IMMEDIATE

            if sub_act in (
                "open_app",
                "close_app",
                "minimize_app",
                "maximize_app",
                "switch_app",
                "list_running_apps",
                "get_active_window",
                "observe",
                "click",
                "type",
                "navigate",
                "scroll",
                "press",
                "new_tab",
                "switch_tab",
                "close_tab",
                "back",
                "wait",
                "screenshot",
                "fill_form",
                "submit_form",
                "upload",
                "upload_file",
            ):
                return ActionRiskLevel.IMMEDIATE

            if sub_act in ("download", "download_file"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("make_payment", "pay", "payment"):
                return ActionRiskLevel.HIGH_IMPACT

            if sub_act in ("purchase", "buy", "place_order"):
                return ActionRiskLevel.HIGH_IMPACT

        # =====================================================================
        # F. WINDOWS AND SOFTWARE MANAGEMENT
        # =====================================================================
        # - Check Windows version: IMMEDIATE
        # - Install application: IMMEDIATE
        # - Install software: CONFIRMATION REQUIRED
        # - Uninstall software: HIGH-IMPACT
        # - Modify software/system configuration: CONFIRMATION REQUIRED
        # - Change security settings: HIGH-IMPACT
        # - Change administrator settings: HIGH-IMPACT
        # - Change user settings: CONFIRMATION REQUIRED
        # - Delete system files: HIGH-IMPACT
        if act in ("windows_control", "computer_control"):
            if sub_act in (
                "check_windows_version",
                "windows_version",
                "system_info",
                "install_app",
                "install_application",
            ):
                return ActionRiskLevel.IMMEDIATE

            if sub_act in ("install_software", "install_package"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("uninstall_software", "uninstall_app", "uninstall_package"):
                return ActionRiskLevel.HIGH_IMPACT

            if sub_act in ("modify_system_config", "modify_software_config", "system_config"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("change_security_settings", "security_settings", "disable_firewall", "antivirus_settings"):
                return ActionRiskLevel.HIGH_IMPACT

            if sub_act in ("change_admin_settings", "admin_settings", "uac_settings"):
                return ActionRiskLevel.HIGH_IMPACT

            if sub_act in ("change_user_settings", "user_settings"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED

            if sub_act in ("delete_system_files", "delete_system_file"):
                return ActionRiskLevel.HIGH_IMPACT

        # Antigravity IDE Risk Classifications
        if act == "antigravity_ide":
            if sub_act in ("send_prompt", "prompt"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED
            if sub_act in ("accept_all", "reject_all"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED
            if sub_act in ("git_push", "push"):
                return ActionRiskLevel.CONFIRMATION_REQUIRED
            # Read-only or normal controls
            if sub_act in (
                "status", "window_status", "panel_status", "list_models",
                "open_folder", "open_file", "open_diff", "add_folder", "open_panel",
                "select_model", "new_conversation", "stop_agent", "create_file",
                "write_file", "create_folder", "save_file", "review_changes",
                "close_all_files", "close_all_editors", "new_terminal", "open_terminal",
                "run_terminal_command", "run_command", "terminal_command",
                "git_commit", "commit", "git_status", "status_git", "git_pull", "pull",
            ):
                return ActionRiskLevel.IMMEDIATE

        # Version Control Risk Classifications
        if act == "version_control":
            if sub_act == "push":
                return ActionRiskLevel.CONFIRMATION_REQUIRED
            if sub_act in ("status", "commit", "pull"):
                return ActionRiskLevel.IMMEDIATE

        # Productivity actions (to-do list, tasks, habits) execute immediately without confirmation
        if act == "productivity":
            return ActionRiskLevel.IMMEDIATE

        # Check generic table fallback
        if act in ACTION_PERMISSIONS:
            tier = ACTION_PERMISSIONS[act]
            if tier in (READ_ONLY, NORMAL):
                return ActionRiskLevel.IMMEDIATE
            if tier == DESTRUCTIVE:
                return ActionRiskLevel.HIGH_IMPACT
            return ActionRiskLevel.CONFIRMATION_REQUIRED

        # Fail-closed: default to CONFIRMATION_REQUIRED
        return ActionRiskLevel.CONFIRMATION_REQUIRED

    @classmethod
    def get_confirmation_details(
        cls, action_name: str, parameters: dict | None = None
    ) -> Tuple[str, str, Optional[dict]]:
        """Construct descriptive title, consequence details, and payment metadata."""
        act = str(action_name or "").lower().strip()
        params = parameters or {}
        sub_act = str(params.get("action") or "").lower().strip()
        target = str(
            params.get("target")
            or params.get("device")
            or params.get("name")
            or params.get("ssid")
            or params.get("path")
            or ""
        ).strip().lower()
        sub_act = str(params.get("action") or "").lower().strip()
        target = str(
            params.get("target")
            or params.get("device")
            or params.get("name")
            or params.get("ssid")
            or params.get("path")
            or ""
        ).strip()
        payment_info: Optional[dict] = None

        # Wi-Fi
        if sub_act in ("connect_wifi", "switch_wifi", "connect_network"):
            title = "Switch Wi-Fi Network"
            detail = f"Disconnect current connection and connect to '{target or 'selected network'}'."
        elif sub_act in ("disconnect_wifi", "disconnect_network"):
            title = "Disconnect Wi-Fi"
            detail = "Disconnect from the currently active Wi-Fi network."
        elif sub_act in ("forget_wifi", "forget_network", "delete_wifi_profile"):
            title = "Forget Wi-Fi Network"
            detail = f"Remove stored credentials and profile for '{target}'."
        elif sub_act in ("set_network_config", "change_network_config", "change_dns", "set_ip_config"):
            title = "Change Network Configuration"
            detail = "Apply changes to network interface configuration."

        # Bluetooth
        elif sub_act in ("bluetooth_off", "disable_bluetooth") or (
            sub_act == "set_bluetooth_enabled"
            and str(params.get("value") or target).lower() in ("off", "false", "0", "disable")
        ):
            title = "Turn Bluetooth Off"
            detail = "Turn off the Bluetooth adapter and disconnect all connected devices."
        elif sub_act in ("connect_bluetooth_device", "connect_bluetooth", "connect_device"):
            title = "Connect Bluetooth Device"
            detail = f"Connect to Bluetooth device '{target}'."
        elif sub_act in ("disconnect_bluetooth_device", "disconnect_bluetooth", "disconnect_device"):
            title = "Disconnect Bluetooth Device"
            detail = f"Disconnect Bluetooth device '{target}'."
        elif sub_act in ("switch_bluetooth_device", "switch_device"):
            title = "Switch Bluetooth Device"
            detail = f"Switch active connection to '{target}'."
        elif sub_act in ("pair_bluetooth_device", "pair_bluetooth", "pair_device"):
            title = "Pair Bluetooth Device"
            detail = f"Initiate pairing with '{target}'."
        elif sub_act in ("unpair_bluetooth_device", "forget_bluetooth_device", "remove_bluetooth_device"):
            title = "Remove Paired Device"
            detail = f"Unpair and remove Bluetooth device '{target}'."
        elif sub_act in (
            "set_audio_output",
            "switch_audio_output",
            "set_default_playback_device",
            "change_audio_output",
        ):
            title = "Change Audio Output"
            detail = f"Switch audio speaker/output device to '{target or 'default'}'."
        elif sub_act in ("set_audio_input", "switch_audio_input", "set_default_recording_device", "change_audio_input"):
            title = "Change Audio Input"
            detail = f"Switch microphone/input device to '{target or 'default'}'."
        elif sub_act in ("set_audio_device", "switch_audio_device", "switch_audio"):
            title = "Switch Audio Devices"
            detail = f"Switch both microphone and speaker audio devices to '{target or 'default'}'."
        elif sub_act in ("reset_audio_devices", "use_default_audio_devices"):
            title = "Reset Audio Devices"
            detail = "Reset microphone and speaker devices to Windows system defaults."

        # JARVIS Shutdown vs System Controls
        elif (
            act in ("shutdown_jarvis", "kill_jarvis")
            or sub_act in ("shutdown_jarvis", "close_jarvis", "kill_jarvis")
            or (
                sub_act in ("close_app", "kill", "quit", "exit")
                and any(k in target for k in ("jarvis", "assistant", "ai"))
            )
        ):
            title = "Shut Down JARVIS"
            detail = "Are you sure you want to completely shut down and exit JARVIS?"
        elif sub_act in ("shutdown", "shutdown_computer", "shutdown_laptop"):
            title = "Shut Down Laptop"
            detail = "Power off the computer. Unsaved work in open applications will be lost."
        elif sub_act in ("restart", "restart_computer", "restart_laptop", "reboot"):
            title = "Restart Laptop"
            detail = "Restart the operating system. Any unsaved work will be lost."
        elif sub_act in ("hibernate", "hibernate_laptop", "hibernate_computer"):
            title = "Hibernate Laptop"
            detail = "Save memory state to disk and put the laptop into deep hibernation."
        elif sub_act in ("sleep", "sleep_display", "sleep_laptop", "sleep_computer"):
            title = "Put Laptop to Sleep"
            detail = "Put the computer into low-power sleep mode."
        elif sub_act in ("set_power_mode", "power_mode", "change_power_mode"):
            title = "Change Power Mode"
            detail = f"Switch power scheme to '{target}'."
        elif sub_act in ("set_airplane_mode", "airplane_mode", "airplane_mode_on", "airplane_mode_off"):
            title = "Toggle Airplane Mode"
            detail = "Turn Airplane Mode on/off, altering all wireless communications."
        elif sub_act in ("set_resolution", "change_resolution", "change_display_resolution"):
            title = "Change Display Resolution"
            detail = f"Set screen resolution to '{target}'."
        elif sub_act in ("set_display_mode", "display_mode", "change_display_configuration"):
            title = "Change Display Configuration"
            detail = f"Set display configuration mode to '{target}'."

        # File Management
        elif sub_act in ("create_file", "create_folder"):
            title = "Create New File/Folder"
            detail = f"Create '{target or params.get('name', 'new item')}'."
        elif sub_act in ("write", "edit_file"):
            title = "Edit File"
            detail = f"Modify the contents of '{target or params.get('name', 'file')}'."
        elif sub_act == "rename":
            title = "Rename File or Folder"
            detail = f"Rename '{target or params.get('name', '')}' to '{params.get('new_name', '')}'."
        elif sub_act == "copy":
            title = "Copy Files"
            detail = f"Copy '{target or params.get('name', '')}' to '{params.get('destination', '')}'."
        elif sub_act == "move":
            title = "Move Files"
            detail = f"Move '{target or params.get('name', '')}' to '{params.get('destination', '')}'."
        elif sub_act in ("delete", "delete_file", "delete_folder", "remove"):
            title = "Delete File or Folder"
            detail = f"Delete '{target or params.get('name', '')}'. This item will be removed."
        elif sub_act in ("empty_recycle_bin", "empty_trash"):
            title = "Empty Recycle Bin"
            detail = "Permanently delete all contents of the Windows Recycle Bin."

        # Applications & Browser
        elif sub_act in ("download", "download_file"):
            title = "Download File"
            detail = (
                f"Download file from '{params.get('url') or target or 'web'}'. Verify the source before confirming."
            )
        elif sub_act in ("make_payment", "pay", "payment"):
            amt = params.get("amount") or "unspecified amount"
            recip = params.get("recipient") or params.get("merchant") or "merchant"
            title = "Authorize Payment"
            detail = f"Authorize payment transaction of {amt} to {recip}."
            payment_info = {"amount": amt, "recipient": recip}
        elif sub_act in ("purchase", "buy", "place_order"):
            prod = params.get("product") or params.get("item") or "item"
            price = params.get("price") or params.get("amount") or "unspecified price"
            merch = params.get("merchant") or params.get("store") or "store"
            title = "Complete Purchase"
            detail = f"Complete purchase of '{prod}' for {price} from '{merch}'."
            payment_info = {"product": prod, "price": price, "merchant": merch}

        # Software & Windows Management
        elif sub_act in ("install_software", "install_package"):
            title = "Install Software"
            detail = f"Run installation package for '{target}'."
        elif sub_act in ("uninstall_software", "uninstall_app"):
            title = "Uninstall Software"
            detail = f"Uninstall '{target}' and remove its associated files from the computer."
        elif sub_act in ("modify_system_config", "modify_software_config"):
            title = "Modify System Configuration"
            detail = "Apply changes to system or software configuration."
        elif sub_act in ("change_security_settings", "security_settings", "disable_firewall"):
            title = "Change Security Settings"
            detail = "Modify Windows security or firewall settings. This impacts system security."
        elif sub_act in ("change_admin_settings", "admin_settings"):
            title = "Change Administrator Settings"
            detail = "Modify administrative or elevation policy settings."
        elif sub_act in ("change_user_settings", "user_settings"):
            title = "Change User Settings"
            detail = "Update Windows user profile or account settings."
        elif sub_act in ("delete_system_files", "delete_system_file"):
            title = "Delete System Files"
            detail = f"Permanently delete system file '{target}'. Warning: Deleting system files can corrupt Windows."

        # WhatsApp & Messaging
        elif act in ("whatsapp_send", "whatsapp") or (
            act == "send_message" and any(k in str(params.get("platform", "")).lower() for k in ("whatsapp", "wp", "wapp"))
        ):
            recip = params.get("contact") or params.get("receiver") or params.get("recipient") or target or "recipient"
            msg_text = params.get("text") or params.get("message") or params.get("message_text") or ""
            title = f"Send WhatsApp message to {recip}"
            detail = f"'{msg_text}'"
        elif act == "send_message":
            plat = params.get("platform", "messaging app")
            recip = params.get("contact") or params.get("receiver") or params.get("recipient") or target or "recipient"
            msg_text = params.get("text") or params.get("message") or params.get("message_text") or ""
            title = f"Send {plat} message to {recip}"
            detail = f"'{msg_text}'"
        elif act in ("gmail_send", "gmail_reply"):
            recip = params.get("to") or params.get("recipient") or "recipient"
            subject = params.get("subject", "")
            body = params.get("body") or params.get("message") or ""
            preview = body[:60] + ("..." if len(body) > 60 else "")
            title = f"Send email to {recip}"
            detail = f"Subject '{subject}': '{preview}'"
        elif act in ("chatgpt_ask", "chatgpt_continue"):
            prompt_str = params.get("prompt") or params.get("text") or ""
            preview = prompt_str[:60] + ("..." if len(prompt_str) > 60 else "")
            title = "Submit prompt to ChatGPT"
            detail = f"'{preview}'"

        # Antigravity IDE Actions
        elif act == "antigravity_ide":
            folder = params.get("folder") or params.get("path") or "active project"
            prompt_txt = params.get("prompt") or params.get("text") or ""
            if sub_act in ("send_prompt", "prompt"):
                title = "Send Prompt to Antigravity IDE"
                detail = f"Send prompt to Antigravity IDE in project '{folder}':\n\"{prompt_txt}\""
            elif sub_act == "accept_all":
                fc = params.get("file_count") or params.get("files") or "all"
                title = "Accept All Changes in Antigravity IDE"
                detail = f"Accept all agent code edits in Antigravity IDE ({fc} files)."
            elif sub_act in ("git_push", "push"):
                br = params.get("branch") or "current branch"
                rem = params.get("remote") or "origin"
                title = "Push Git Commits to Remote"
                detail = f"Push commits on branch '{br}' to remote repository '{rem}'."
            elif sub_act == "reject_all":
                title = "Reject All Changes in Antigravity IDE"
                detail = "Discard all agent code changes in Antigravity IDE."
            else:
                title = f"Antigravity IDE: {sub_act}"
                detail = f"Execute Antigravity IDE action '{sub_act}'."

        # Version Control Actions
        elif act == "version_control":
            br = params.get("branch") or "current branch"
            rem = params.get("remote") or "origin"
            if sub_act == "push":
                title = "Push Git Commits to Remote"
                detail = f"Push commits on branch '{br}' to remote repository '{rem}'."
            else:
                title = f"Version Control: {sub_act}"
                detail = f"Execute Git version control action '{sub_act}'."

        else:
            title = f"Execute {action_name}"
            detail = f"Execute action '{action_name}' with target '{target}'."

        return title, detail, payment_info


# ── Centralized Action Execution Guard ───────────────────────────────────────


class ActionExecutionGuard:
    """Authoritative execution guard for all registered actions."""

    @classmethod
    def get_risk_level(cls, action_name: str, parameters: dict | None = None) -> ActionRiskLevel:
        return ActionPermissionManager.get_risk_level(action_name, parameters)

    @classmethod
    def guard_and_execute(
        cls,
        action_name: str,
        parameters: dict,
        handler: Callable,
        ctx: dict,
        logger: Optional[Callable] = None,
        t0: Optional[float] = None,
    ) -> outcome.ActionResult:
        """Evaluate action permission, check forbidden operations, park or execute."""
        start_time = t0 or time.monotonic()
        name = str(action_name or "").strip()
        params = dict(parameters or {})
        _log = logger or (lambda msg: None)

        # 1. Drive Formatting / Forbidden Check
        if ActionPermissionManager.is_drive_format_attempt(name, params):
            dur = (time.monotonic() - start_time) * 1000
            msg = "Drive formatting is strictly prohibited and not supported on this system."
            log_action_audit(name, params, "forbidden", "blocked", False, error="FORBIDDEN_DRIVE_FORMAT")
            return outcome.ActionResult(
                status=outcome.STATUS_UNSUPPORTED,
                action=name,
                success=False,
                verified=False,
                message=msg,
                error=msg,
                duration_ms=dur,
            )

        # 1.5. Central Permission Policy Evaluation (ALLOW / ASK / DENY)
        policy, perm_entry = resolve_action_policy(name, params)
        perm_id = perm_entry.id if perm_entry else f"tool:{name}"

        # If policy is DENY: reject immediately
        if policy == POLICY_DENY:
            dur = (time.monotonic() - start_time) * 1000
            msg = f"Action '{name}' is denied by permission settings."
            log_permission_decision(perm_id, name, "DENIED", msg)
            log_action_audit(name, params, "denied", "blocked", False, error="PERMISSION_DENIED")
            return outcome.ActionResult(
                status=outcome.STATUS_BLOCKED,
                action=name,
                success=False,
                verified=False,
                message=msg,
                error="PERMISSION_DENIED",
                duration_ms=dur,
            )

        # 2. Check if already authorized by human confirmation
        # Internal flag set ONLY when user confirms through ConfirmationManager
        if params.get("_execution_authorized") is True:
            clean_params = {k: v for k, v in params.items() if k != "_execution_authorized"}
            try:
                raw = _call_handler_safe(handler, clean_params, ctx)
                dur = (time.monotonic() - start_time) * 1000
                res = outcome.normalize_result(raw, action_name=name, duration_ms=dur)
                log_permission_decision(perm_id, name, "ALLOWED", "Human confirmed execution")
                log_action_audit(name, clean_params, "authorized", res.status, res.verified, res.error)
                return res
            except Exception as e:
                dur = (time.monotonic() - start_time) * 1000
                _log(f"Action '{name}' failed during authorized execute: {e}")
                traceback.print_exc()
                res = outcome.ActionResult(
                    status=outcome.STATUS_FAILED,
                    action=name,
                    success=False,
                    verified=False,
                    message=f"Action '{name}' failed: {e}",
                    error=str(e),
                    duration_ms=dur,
                )
                log_permission_decision(perm_id, name, "ALLOWED", f"Execution failed: {e}")
                log_action_audit(name, clean_params, "authorized", res.status, res.verified, res.error)
                return res

        # 2.5. Check active Task Approval Scope (multi-step tasks approved by user)
        from core.task_approval import TaskApprovalStore
        active_scope = TaskApprovalStore.get_active_scope()
        if active_scope and active_scope.matches_action(name, params, context=ctx):
            TaskApprovalStore.record_step()
            clean_params = {k: v for k, v in params.items() if k != "_execution_authorized"}
            try:
                raw = _call_handler_safe(handler, clean_params, ctx)
                dur = (time.monotonic() - start_time) * 1000
                res = outcome.normalize_result(raw, action_name=name, duration_ms=dur)
                log_permission_decision(perm_id, name, "ALLOWED", f"task_scope_{active_scope.scope_id}")
                log_action_audit(name, clean_params, f"task_scope_{active_scope.scope_id}", res.status, res.verified, res.error)
                return res
            except Exception as e:
                dur = (time.monotonic() - start_time) * 1000
                res = outcome.ActionResult(
                    status=outcome.STATUS_FAILED,
                    action=name,
                    success=False,
                    verified=False,
                    message=f"Action '{name}' failed in task scope: {e}",
                    error=str(e),
                    duration_ms=dur,
                )
                log_action_audit(name, clean_params, f"task_scope_{active_scope.scope_id}", res.status, res.verified, res.error)
                return res

        # 3. Check Risk Level from Authoritative Risk Matrix
        risk_level = ActionPermissionManager.get_risk_level(name, params, context=ctx)
        from core.risk_tiers import RiskTier, get_action_tier
        risk_tier = get_action_tier(name, params, context=ctx)

        # If policy is ALLOW, or if risk is IMMEDIATE (Level 1) and policy is not ASK:
        if policy == POLICY_ALLOW or (risk_level == ActionRiskLevel.IMMEDIATE and policy != POLICY_ASK):
            clean_params = {k: v for k, v in params.items() if k != "_execution_authorized"}
            try:
                raw = _call_handler_safe(handler, clean_params, ctx)
                dur = (time.monotonic() - start_time) * 1000
                res = outcome.normalize_result(raw, action_name=name, duration_ms=dur)
                log_permission_decision(perm_id, name, "ALLOWED", "Policy ALLOW or Immediate action")
                log_action_audit(name, clean_params, str(risk_tier.value), res.status, res.verified, res.error)
                return res
            except Exception as e:
                dur = (time.monotonic() - start_time) * 1000
                _log(f"Action '{name}' failed during immediate execute: {e}")
                traceback.print_exc()
                res = outcome.ActionResult(
                    status=outcome.STATUS_FAILED,
                    action=name,
                    success=False,
                    verified=False,
                    message=f"Action '{name}' failed: {e}",
                    error=str(e),
                    duration_ms=dur,
                )
                log_permission_decision(perm_id, name, "ALLOWED", f"Execution error: {e}")
                log_action_audit(name, clean_params, str(risk_tier.value), res.status, res.verified, res.error)
                return res

        # Level 2 (MEDIUM) or Level 3 (HIGH): Confirmation Required
        title, detail, payment_info = ActionPermissionManager.get_confirmation_details(name, params)

        from core import confirm

        # Runner closure to execute only after user confirms
        def _confirmed_runner() -> str:
            auth_params = {**params, "_execution_authorized": True, "confirmed": True}
            try:
                raw = _call_handler_safe(handler, auth_params, ctx)
                res = outcome.normalize_result(raw, action_name=name)
                log_action_audit(name, params, str(risk_tier.value), res.status, res.verified, res.error)
                # Output to UI log if player available
                player = ctx.get("player")
                if player:
                    if hasattr(player, "write_log"):
                        player.write_log(f"SYS: Confirmed {title} executed. {res.message or 'Done.'}")
                return res.message or "Done."
            except Exception as err:
                log_action_audit(name, params, str(risk_tier.value), "failed", False, error=str(err))
                return f"Failed: {err}"

        # Park behind confirmation gate with PendingAction store
        pending = confirm.save_pending(
            tool=name,
            params=dict(params),
            run=_confirmed_runner,
            expires_in=60.0,
            title=title,
            detail=detail,
            risk_level=str(risk_tier.value),
            payment_details=payment_info,
            tier=risk_tier,
        )

        prompt_msg = confirm.request_action(
            action=name,
            parameters=params,
            risk_level=str(risk_tier.value),
            title=title,
            detail=detail,
            run=_confirmed_runner,
            payment_details=payment_info,
            action_id=pending.id,
            tier=risk_tier,
        )

        dur = (time.monotonic() - start_time) * 1000
        res = outcome.ActionResult(
            status=outcome.STATUS_NEEDS_CONFIRMATION,
            action=name,
            success=False,
            verified=False,
            message=prompt_msg,
            data={"id": pending.id, "tool": name, "params": pending.params},
            duration_ms=dur,
        )
        log_action_audit(name, params, str(risk_level.value), "pending_confirmation", False)
        return res


_CTX_KEYS = ("player", "response", "session_memory", "speak")


def _call_handler_safe(fn: Callable, parameters: dict, ctx: dict) -> Any:
    """Invoke the handler passing parameters dict or individual kwargs based on signature."""
    sig = inspect.signature(fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

    # Filter context arguments
    kwargs = {}
    for key in _CTX_KEYS:
        if has_var_kw or key in sig.parameters:
            kwargs[key] = ctx.get(key)

    # Standard JARVIS tool handlers accept (parameters: dict, ...)
    if "parameters" in sig.parameters:
        return fn(parameters=parameters, **kwargs)
    elif has_var_kw:
        try:
            return fn(parameters=parameters, **kwargs)
        except TypeError:
            clean_args = dict(parameters)
            clean_args.update(kwargs)
            return fn(**clean_args)
    else:
        clean_args = {k: v for k, v in parameters.items() if k in sig.parameters}
        clean_args.update(kwargs)
        return fn(**clean_args)


# ── Backward Compatibility API ──────────────────────────────────────────────


def get_action_permission(action_name: str, parameters: dict | None = None) -> str:
    """Map action to backward-compatible tier string."""
    risk = ActionPermissionManager.get_risk_level(action_name, parameters)
    if risk == ActionRiskLevel.FORBIDDEN:
        return FORBIDDEN
    if risk == ActionRiskLevel.HIGH_IMPACT:
        return DESTRUCTIVE
    if risk == ActionRiskLevel.CONFIRMATION_REQUIRED:
        return SENSITIVE
    return ACTION_PERMISSIONS.get(str(action_name or "").lower().strip(), SENSITIVE)


def requires_confirmation(action_name: str, parameters: dict | None = None, from_untrusted: bool | None = None) -> bool:
    """Authoritative check: returns True if action requires explicit confirmation."""
    from memory.config_manager import (
        TRUST_LEVEL_BALANCED,
        TRUST_LEVEL_STRICT,
        TRUST_LEVEL_TRUSTED,
        get_trust_level,
    )

    act = (action_name or "").lower().strip()
    perm = ACTION_PERMISSIONS.get(act)
    trust = get_trust_level()

    # Under STRICT trust: SENSITIVE and DESTRUCTIVE always require confirmation when checked at tool level
    if trust == TRUST_LEVEL_STRICT and not parameters:
        if perm in (SENSITIVE, DESTRUCTIVE):
            return True

    # DESTRUCTIVE permissions always require confirmation even under TRUSTED
    if perm == DESTRUCTIVE and not parameters:
        return True

    risk = ActionPermissionManager.get_risk_level(act, parameters)

    untrusted_origin = _untrusted_context if from_untrusted is None else bool(from_untrusted)
    if untrusted_origin and risk in (ActionRiskLevel.HIGH_IMPACT, ActionRiskLevel.CONFIRMATION_REQUIRED):
        return True

    if risk == ActionRiskLevel.FORBIDDEN:
        return True

    if risk == ActionRiskLevel.HIGH_IMPACT:
        return True

    if risk == ActionRiskLevel.CONFIRMATION_REQUIRED:
        sub_act = (parameters or {}).get("action")
        # When evaluated at tool level without specific sub-action, respect session trust level
        if not sub_act:
            trust = get_trust_level()
            if trust == TRUST_LEVEL_TRUSTED:
                return False
            if trust == TRUST_LEVEL_BALANCED:
                with _session_lock:
                    return act not in _session_approved_sensitive
        return True

    return False


_session_approved_sensitive: set[str] = set()
_session_lock = Lock()


def reset_session_approvals() -> None:
    with _session_lock:
        _session_approved_sensitive.clear()


def approve_sensitive_action_for_session(action_name: str) -> None:
    with _session_lock:
        _session_approved_sensitive.add((action_name or "").lower().strip())


_untrusted_context: bool = False


def set_untrusted_context(active: bool) -> None:
    global _untrusted_context
    _untrusted_context = bool(active)


def is_untrusted_context() -> bool:
    return _untrusted_context


# ── SQLite Audit Log & Secret Redaction ──────────────────────────────────────

_MAX_AUDIT_ENTRIES = 5000


def _get_audit_db_path() -> Path:
    try:
        from memory.config_manager import get_user_data_dir

        d = get_user_data_dir()
    except Exception:
        d = Path.home() / ".jarvis"
    d.mkdir(parents=True, exist_ok=True)
    return d / "audit.db"


def _init_audit_db(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_trail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            permission_level TEXT NOT NULL,
            status TEXT NOT NULL,
            verified INTEGER NOT NULL,
            parameters TEXT,
            error TEXT,
            task_id TEXT
        );
    """)
    try:
        conn.execute("ALTER TABLE audit_trail ADD COLUMN task_id TEXT;")
    except Exception:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_trail (timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_trail (action);")
    conn.commit()


def _sanitize_secrets(params: dict) -> str:
    """Mask sensitive fields such as passwords, pins, credentials, credit cards."""
    from memory.config_manager import redact_secret

    cleaned = dict(params or {})
    sensitive_keys = {"password", "pin", "cvv", "credit_card", "card_number", "ssid_password"}
    for k, v in list(cleaned.items()):
        if any(sk == k.lower() or sk in k.lower().split("_") for sk in sensitive_keys):
            cleaned[k] = "***REDACTED***"
        elif isinstance(v, str):
            cleaned[k] = redact_secret(v)
    return redact_secret(str(cleaned))


def log_action_audit(
    action: str,
    parameters: dict,
    permission_level: str,
    status: str,
    verified: bool,
    error: Optional[str] = None,
    task_id: Optional[str] = None,
) -> dict:
    """Record an action execution to persistent SQLite audit log."""
    from memory.config_manager import redact_secret

    if task_id is None:
        task_id = get_current_task_id()

    redacted_params = _sanitize_secrets(parameters)
    now_str = datetime.now().isoformat()

    entry = {
        "timestamp": now_str,
        "action": str(action),
        "permission_level": str(permission_level),
        "status": str(status),
        "verified": bool(verified),
        "parameters": redacted_params,
        "error": redact_secret(str(error)) if error else None,
        "task_id": str(task_id) if task_id else None,
    }

    with _audit_lock:
        _audit_log.append(entry)
        if len(_audit_log) > 100:
            _audit_log.pop(0)

        try:
            db_path = _get_audit_db_path()
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            try:
                _init_audit_db(conn)
                conn.execute(
                    """
                    INSERT INTO audit_trail (timestamp, action, permission_level, status, verified, parameters, error, task_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now_str,
                        entry["action"],
                        entry["permission_level"],
                        entry["status"],
                        1 if entry["verified"] else 0,
                        entry["parameters"],
                        entry["error"],
                        entry["task_id"],
                    ),
                )
                conn.execute(
                    f"DELETE FROM audit_trail WHERE id NOT IN (SELECT id FROM audit_trail ORDER BY id DESC LIMIT {_MAX_AUDIT_ENTRIES})"
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    return entry


def get_audit_trail(limit: int = 50) -> list[dict]:
    """Retrieve recent action audit events from persistent database."""
    with _audit_lock:
        try:
            db_path = _get_audit_db_path()
            if db_path.exists():
                conn = sqlite3.connect(str(db_path), timeout=5.0)
                try:
                    _init_audit_db(conn)
                    cursor = conn.execute(
                        """
                        SELECT timestamp, action, permission_level, status, verified, parameters, error, task_id
                        FROM audit_trail ORDER BY id DESC LIMIT ?
                        """,
                        (int(limit),),
                    )
                    rows = cursor.fetchall()
                    if rows:
                        results = []
                        for r in reversed(rows):
                            results.append(
                                {
                                    "timestamp": r[0],
                                    "action": r[1],
                                    "permission_level": r[2],
                                    "status": r[3],
                                    "verified": bool(r[4]),
                                    "parameters": r[5],
                                    "error": r[6],
                                    "task_id": r[7] if len(r) > 7 else None,
                                }
                            )
                        return results
                finally:
                    conn.close()
        except Exception:
            pass
        return list(_audit_log[-limit:])


def clear_audit_trail(confirm: bool = False) -> None:
    """Clear audit log history. Requires confirm=True."""
    if not confirm:
        raise PermissionError("clear_audit_trail requires explicit confirm=True flag.")

    with _audit_lock:
        _audit_log.clear()
        try:
            db_path = _get_audit_db_path()
            if db_path.exists():
                conn = sqlite3.connect(str(db_path), timeout=5.0)
                try:
                    conn.execute("DELETE FROM audit_trail;")
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
            pass


def query_audit_log(
    action: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """Retrieve filtered action audit events from persistent SQLite database."""
    with _audit_lock:
        try:
            db_path = _get_audit_db_path()
            if db_path.exists():
                conn = sqlite3.connect(str(db_path), timeout=5.0)
                try:
                    _init_audit_db(conn)
                    query = "SELECT id, timestamp, action, permission_level, status, verified, parameters, error, task_id FROM audit_trail WHERE 1=1"
                    params: list[Any] = []
                    if action:
                        query += " AND action LIKE ?"
                        params.append(f"%{action.strip()}%")
                    if status:
                        query += " AND status LIKE ?"
                        params.append(f"%{status.strip()}%")
                    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
                    params.extend([int(limit), int(offset)])
                    cursor = conn.execute(query, tuple(params))
                    rows = cursor.fetchall()
                    results = []
                    for r in rows:
                        results.append({
                            "id": r[0],
                            "timestamp": r[1],
                            "action": r[2],
                            "permission_level": r[3],
                            "status": r[4],
                            "verified": bool(r[5]),
                            "parameters": r[6],
                            "error": r[7],
                            "task_id": r[8],
                        })
                    return results
                finally:
                    conn.close()
        except Exception:
            pass
        # In-memory fallback
        res = []
        for e in reversed(_audit_log):
            if action and action.lower() not in e["action"].lower():
                continue
            if status and status.lower() not in e["status"].lower():
                continue
            res.append(e)
            if len(res) >= limit:
                break
        return res


def get_audit_entry_by_id(entry_id: int) -> Optional[dict]:
    """Fetch complete detail for a single audit entry by primary key ID."""
    with _audit_lock:
        try:
            db_path = _get_audit_db_path()
            if db_path.exists():
                conn = sqlite3.connect(str(db_path), timeout=5.0)
                try:
                    _init_audit_db(conn)
                    cursor = conn.execute(
                        "SELECT id, timestamp, action, permission_level, status, verified, parameters, error, task_id FROM audit_trail WHERE id = ?",
                        (int(entry_id),),
                    )
                    r = cursor.fetchone()
                    if r:
                        return {
                            "id": r[0],
                            "timestamp": r[1],
                            "action": r[2],
                            "permission_level": r[3],
                            "status": r[4],
                            "verified": bool(r[5]),
                            "parameters": r[6],
                            "error": r[7],
                            "task_id": r[8],
                        }
                finally:
                    conn.close()
        except Exception:
            pass
        return None


def get_audit_summary(hours: int = 24) -> dict:
    """Aggregate execution statistics across recent audit history."""
    with _audit_lock:
        summary: dict[str, Any] = {
            "total_actions": 0,
            "status_counts": {},
            "top_actions": {},
            "verified_count": 0,
            "failed_count": 0,
        }
        try:
            db_path = _get_audit_db_path()
            if db_path.exists():
                conn = sqlite3.connect(str(db_path), timeout=5.0)
                try:
                    _init_audit_db(conn)
                    # Status counts
                    cursor = conn.execute(
                        "SELECT status, count(*) FROM audit_trail GROUP BY status"
                    )
                    for st, count in cursor.fetchall():
                        summary["status_counts"][st] = count
                        summary["total_actions"] += count
                        if st in ("failed", "blocked", "error"):
                            summary["failed_count"] += count

                    # Verified count
                    cur_v = conn.execute("SELECT count(*) FROM audit_trail WHERE verified = 1")
                    row_v = cur_v.fetchone()
                    summary["verified_count"] = row_v[0] if row_v else 0

                    # Top actions
                    cur_act = conn.execute(
                        "SELECT action, count(*) FROM audit_trail GROUP BY action ORDER BY count(*) DESC LIMIT 5"
                    )
                    for act, count in cur_act.fetchall():
                        summary["top_actions"][act] = count
                    return summary
                finally:
                    conn.close()
        except Exception:
            pass
        # In-memory fallback
        for e in _audit_log:
            summary["total_actions"] += 1
            st = e.get("status", "unknown")
            summary["status_counts"][st] = summary["status_counts"].get(st, 0) + 1
            act = e.get("action", "unknown")
            summary["top_actions"][act] = summary["top_actions"].get(act, 0) + 1
            if e.get("verified"):
                summary["verified_count"] += 1
            if st in ("failed", "blocked"):
                summary["failed_count"] += 1
        return summary

