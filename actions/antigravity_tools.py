"""
actions/antigravity_tools.py — High-Level Verified Antigravity IDE Actions.

Provides:
- antigravity_open(path) — Launch workspace or file via agy / antigravity CLI launcher.
- antigravity_prompt(text) — Forward prompt instruction to the running Antigravity IDE agent.
- antigravity_status() — Report running IDE instance, active workspace, and agent status.
File edits route through file_controller and dev_agent, never GUI keystrokes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from actions import antigravity_launcher
from actions import antigravity_ide
from actions import antigravity_cdp
from core.apps import ToolResult

logger = logging.getLogger("antigravity_tools")


# ── 1. antigravity_open ───────────────────────────────────────────────────────

def antigravity_open(parameters: dict, **kwargs) -> str:
    """Launch or focus a project folder or file in Antigravity IDE via its CLI launcher."""
    p = parameters or {}
    path = str(p.get("path", "")).strip()

    if not path:
        return ToolResult.failed("A 'path' parameter is required to open Antigravity IDE.").to_action_result("antigravity_open")

    res = antigravity_launcher.launch_ide_folder(path)
    if hasattr(res, "is_success") and res.is_success():
        return ToolResult.ok(f"Opened Antigravity IDE at '{path}'.", data={"path": path}, verified=True).to_action_result("antigravity_open")
    return res


# ── 2. antigravity_prompt ─────────────────────────────────────────────────────

def antigravity_prompt(parameters: dict, **kwargs) -> str:
    """Send an instruction prompt to the active Antigravity IDE Agent panel."""
    p = parameters or {}
    text = str(p.get("text", "")).strip()

    if not text:
        return ToolResult.failed("A 'text' prompt is required.").to_action_result("antigravity_prompt")

    return antigravity_ide.antigravity_ide({"action": "send_prompt", "prompt": text}, **kwargs)


# ── 3. antigravity_status ─────────────────────────────────────────────────────

def antigravity_status(parameters: dict = None, **kwargs) -> str:
    """Check running Antigravity IDE instances, active project folder, and agent connection."""
    win_stat = antigravity_launcher.get_window_status()
    cdp_mgr = antigravity_cdp.get_antigravity_session()
    connected, reason = cdp_mgr.check_connection(timeout=1.0)

    is_running = win_stat.get("is_running", False)
    folder = win_stat.get("active_folder", "None")

    summary = (
        f"Antigravity IDE Status:\n"
        f"- Running: {is_running}\n"
        f"- Active Project: {folder}\n"
        f"- CDP Agent Panel Connected: {connected} ({reason if not connected else 'Ready'})\n"
    )

    return ToolResult.ok(
        summary,
        data={
            "is_running": is_running,
            "active_folder": folder,
            "cdp_connected": connected,
            "window": win_stat,
        },
        verified=True,
    ).to_action_result("antigravity_status")


# ── Registration Declarations for core/action_loader ──────────────────────────

TOOLS = [
    {
        "name": "antigravity_open",
        "description": "Open a project folder or file in Antigravity IDE via CLI launcher (agy).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Project folder or file path."},
            },
            "required": ["path"],
        },
        "handler": antigravity_open,
        "permission": "normal",
    },
    {
        "name": "antigravity_prompt",
        "description": "Send an instruction prompt to the Antigravity IDE Agent.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text": {"type": "STRING", "description": "Prompt text to send to the IDE agent."},
            },
            "required": ["text"],
        },
        "handler": antigravity_prompt,
        "permission": "normal",
    },
    {
        "name": "antigravity_status",
        "description": "Inspect running Antigravity IDE instance, active project folder, and agent panel status.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": antigravity_status,
        "permission": "read_only",
    },
]
