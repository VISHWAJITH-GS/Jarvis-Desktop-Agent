"""
core/tasks/security.py — Safety boundaries, tool allowlists, and prompt injection defense.

Design reference: Mark LVII (CC BY-NC 4.0).
Enforces:
1. Every tool call passes through ActionExecutionGuard with current task ID.
2. Background tasks CANNOT auto-confirm sensitive or destructive actions (fails closed to needs_confirmation).
3. Tool allowlist enforcement (default unattended is read-only + workspace folder writes).
4. Content read from web or files is wrapped as untrusted data to prevent prompt injection.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Optional, Set

from core import outcome, permissions
from core.permissions import ActionExecutionGuard, set_current_task_id, set_untrusted_context

DEFAULT_UNATTENDED_ALLOWLIST: set[str] = {
    "web_search",
    "weather_report",
    "file_processor",
    "observe_page",
    "task_status",
    "list_tasks",
    "workspace_write_file",
    "workspace_read_file",
}

READ_TOOLS: set[str] = {
    "web_search",
    "file_processor",
    "workspace_read_file",
}


def wrap_untrusted(source: str, content: str) -> str:
    """Safely defang and wrap external content in untrusted delimiters."""
    return outcome.untrusted(source, content)


def is_tool_allowed(tool_name: str, allowlist: Optional[list[str]] = None) -> bool:
    """Verify tool is permitted under the task's allowlist."""
    effective_allowlist = set(allowlist) if allowlist else DEFAULT_UNATTENDED_ALLOWLIST
    return tool_name in effective_allowlist


def validate_workspace_path(path: str | Path, workspace_dir: str | Path) -> bool:
    """Enforce that file operations remain strictly inside the task's workspace directory."""
    try:
        ws_resolved = Path(workspace_dir).resolve()
        target_resolved = (ws_resolved / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        return target_resolved == ws_resolved or ws_resolved in target_resolved.parents
    except Exception:
        return False


def execute_task_tool(
    task_id: str,
    tool_name: str,
    parameters: dict,
    allowlist: Optional[list[str]] = None,
    workspace_dir: str = "",
) -> outcome.ActionResult:
    """
    Execute a background task tool under strict security sandboxing.
    Passes execution to ActionExecutionGuard with task audit tracking.
    """
    # 1. Check Allowlist
    if not is_tool_allowed(tool_name, allowlist):
        return outcome.failed(
            tool_name,
            f"Tool '{tool_name}' is not in the task's allowed tools list.",
            allowed=list(allowlist or DEFAULT_UNATTENDED_ALLOWLIST),
        )

    # 2. Check Workspace containment for file write tools
    if tool_name in ("workspace_write_file", "write_file"):
        file_path = parameters.get("path") or parameters.get("filename") or ""
        if not workspace_dir or not validate_workspace_path(file_path, workspace_dir):
            return outcome.failed(
                tool_name,
                "File writes in background tasks must remain strictly within the task workspace directory.",
                workspace_dir=workspace_dir,
            )

    # 3. Bind task context and mark environment untrusted
    set_current_task_id(task_id)
    set_untrusted_context(True)
    try:
        from core.action_loader import get_default_registry

        reg = get_default_registry()
        # 4. Route through centralized ActionRegistry and ActionExecutionGuard
        res = reg.execute(tool_name, parameters, ctx={"player": None})

        # 5. Wrap external content from read tools to defang prompt injection
        if res.status == outcome.STATUS_DONE and tool_name in READ_TOOLS:
            if isinstance(res.data, dict) and "text" in res.data:
                res.data["text"] = wrap_untrusted(tool_name, str(res.data["text"]))
            elif isinstance(res.data, str):
                res.data = wrap_untrusted(tool_name, res.data)
            if res.message:
                res.message = wrap_untrusted(tool_name, res.message)

        return res
    finally:
        set_current_task_id(None)
