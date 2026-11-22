"""
actions/antigravity_ide.py — Full Antigravity IDE & Agent Panel Controller for JARVIS.

Integrates:
- (A) Launcher & Window Control: open_folder, open_file, open_diff, add_folder, status, window_status.
- (B) Atomic File Operations: create_file, write_file, create_folder, save_file with undo backups.
- (C) Agent Panel Control: panel_status, open_panel, list_models, select_model, new_conversation, stop_agent.
- (D) Long-Running Execution: send_prompt with background polling and backoff.
- (E) Review & Safety Actions: review_changes, accept_all (with git checkpoints), reject_all, accept_file, reject_file.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from actions import antigravity_cdp
from actions import antigravity_files
from actions import antigravity_launcher
from actions import antigravity_review
from actions import antigravity_terminal
from actions import version_control
from core import outcome

logger = logging.getLogger("antigravity_ide")

TOOL = {
    "name": "antigravity_ide",
    "description": (
        "Control Google's Antigravity IDE (VS Code-based agentic IDE). "
        "Supports opening projects/files/diffs, atomic file writes with undo checkpoints, "
        "and driving the right-side Antigravity Agent Panel (select model, send prompt, "
        "review diffs, Accept all / Reject all). Never say done or sent unless tool confirms."
    ),
    "brief": "Control Antigravity IDE: projects, files, and right-side agent panel.",
    "tier": "core",
    "permission": "normal",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "Action to execute: 'open_folder', 'open_file', 'open_diff', 'add_folder', "
                    "'window_status', 'panel_status', 'open_panel', 'list_models', 'select_model', "
                    "'new_conversation', 'stop_agent', 'send_prompt', 'review_changes', "
                    "'accept_all', 'reject_all', 'accept_file', 'reject_file', 'create_file', "
                    "'write_file', 'create_folder', 'save_file', 'close_all_files', 'new_terminal', "
                    "'run_terminal_command', 'git_commit', 'git_push', 'git_status', 'git_pull'."
                ),
                "enum": [
                    "open_folder",
                    "open_file",
                    "open_diff",
                    "add_folder",
                    "window_status",
                    "panel_status",
                    "open_panel",
                    "list_models",
                    "select_model",
                    "new_conversation",
                    "stop_agent",
                    "send_prompt",
                    "review_changes",
                    "accept_all",
                    "reject_all",
                    "accept_file",
                    "reject_file",
                    "create_file",
                    "write_file",
                    "create_folder",
                    "save_file",
                    "close_all_files",
                    "new_terminal",
                    "run_terminal_command",
                    "git_commit",
                    "git_push",
                    "git_status",
                    "git_pull",
                ],
            },
            "path": {
                "type": "STRING",
                "description": "File or folder path to open, create, write, or inspect.",
            },
            "file_b": {
                "type": "STRING",
                "description": "Second file path when opening a diff comparison.",
            },
            "line": {
                "type": "INTEGER",
                "description": "Optional line number when opening a file.",
            },
            "column": {
                "type": "INTEGER",
                "description": "Optional column number when opening a file.",
            },
            "content": {
                "type": "STRING",
                "description": "Content to write or create in a file.",
            },
            "mode": {
                "type": "STRING",
                "description": "Write mode: 'overwrite' (default) or 'append'.",
                "enum": ["overwrite", "append"],
            },
            "prompt": {
                "type": "STRING",
                "description": "Prompt text to send to the Antigravity agent panel.",
            },
            "model": {
                "type": "STRING",
                "description": "Model name to select in the agent panel (e.g. 'Gemini 2.5 Pro').",
            },
            "filename": {
                "type": "STRING",
                "description": "Single filename to accept or reject during review.",
            },
            "new_window": {
                "type": "BOOLEAN",
                "description": "Force opening in a new IDE window (passes -n flag).",
            },
            "source": {
                "type": "STRING",
                "description": "Origin of prompt text (must be 'user' for safety).",
            },
            "command": {
                "type": "STRING",
                "description": "Terminal command to execute programmatically.",
            },
            "message": {
                "type": "STRING",
                "description": "Commit message for Git commit operations.",
            },
            "branch": {
                "type": "STRING",
                "description": "Target Git branch name.",
            },
            "remote": {
                "type": "STRING",
                "description": "Target Git remote name (default 'origin').",
            },
            "save_first": {
                "type": "BOOLEAN",
                "description": "Whether to save all open files before closing (for close_all_files).",
            },
            "force": {
                "type": "BOOLEAN",
                "description": "Force flag (e.g. force close without saving, or force push).",
            },
            "files": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Specific list of file paths to stage/commit.",
            },
            "cwd": {
                "type": "STRING",
                "description": "Working directory for terminal command or Git operations.",
            },
        },
        "required": ["action"],
    },
    "handler": "antigravity_ide",
}


def antigravity_ide(parameters: dict, player=None) -> outcome.ActionResult:
    """Authoritative handler for Antigravity IDE actions."""
    sub_act = str(parameters.get("action") or "").lower().strip()
    path = parameters.get("path") or parameters.get("folder") or parameters.get("file")
    mgr = antigravity_cdp.get_antigravity_cdp_manager()

    # 1. Launcher Actions
    if sub_act == "open_folder":
        return antigravity_launcher.open_folder(
            path=str(path or ""),
            new_window=bool(parameters.get("new_window", False)),
        )

    if sub_act == "open_file":
        return antigravity_launcher.open_file(
            path=str(path or ""),
            line=parameters.get("line"),
            column=parameters.get("column"),
        )

    if sub_act == "open_diff":
        file_a = str(path or parameters.get("file_a") or "")
        file_b = str(parameters.get("file_b") or "")
        return antigravity_launcher.open_diff(file_a=file_a, file_b=file_b)

    if sub_act == "add_folder":
        return antigravity_launcher.add_folder(path=str(path or ""))

    # 2. Window Awareness
    if sub_act in ("status", "window_status"):
        st = antigravity_launcher.get_window_status()
        running_str = "running" if st.get("is_running") else "not running"
        folder_str = st.get("open_folder") or "none"
        file_str = st.get("active_file") or "none"
        dirty_str = " (has unsaved changes)" if st.get("has_unsaved_changes") else ""
        msg = f"Antigravity IDE is {running_str}. Active folder: '{folder_str}', file: '{file_str}'{dirty_str}."
        if player and hasattr(player, "write_log"):
            player.write_log(f"AGY: {msg}")
        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="window_status",
            success=True,
            verified=True,
            message=msg,
            data=st,
        )

    # 3. File Operations
    if sub_act == "create_file":
        return antigravity_files.create_file(
            path=str(path or ""),
            content=str(parameters.get("content") or ""),
        )

    if sub_act == "write_file":
        return antigravity_files.write_file(
            path=str(path or ""),
            content=str(parameters.get("content") or ""),
            mode=str(parameters.get("mode") or "overwrite"),
        )

    if sub_act == "create_folder":
        return antigravity_files.create_folder(path=str(path or ""))

    if sub_act == "save_file":
        return antigravity_files.save_file(
            path=str(path or "") if path else None,
            save_all=bool(parameters.get("save_all", False)),
        )

    # 4. Agent Panel Actions
    if sub_act == "panel_status":
        pst = mgr.panel_status()
        vis_str = "visible" if pst.get("visible") else "hidden"
        agent_state = "busy (generating)" if pst.get("running") else "idle"
        msg = (
            f"Antigravity panel is {vis_str}. Active model: {pst.get('model')}. "
            f"Agent is {agent_state}. Pending changed files: {pst.get('pending_files')}."
        )
        if pst.get("approval_waiting"):
            msg += f" [APPROVAL WAITING for '{pst.get('approval_command')}']"
        if pst.get("error_banner"):
            msg += f" [ERROR: {pst.get('error_banner')}]"

        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="panel_status",
            success=True,
            verified=True,
            message=msg,
            data=pst,
        )

    if sub_act == "open_panel":
        return mgr.open_agent_panel()

    if sub_act == "list_models":
        models, active_m = mgr.list_models()
        msg = f"Antigravity models: {', '.join(models)}. Currently active: {active_m}."
        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="list_models",
            success=True,
            verified=True,
            message=msg,
            data={"models": models, "active": active_m},
        )

    if sub_act == "select_model":
        model_name = str(parameters.get("model") or parameters.get("name") or "")
        return mgr.select_model(model_name)

    if sub_act == "new_conversation":
        return mgr.new_conversation()

    if sub_act == "stop_agent":
        return mgr.stop_agent()

    if sub_act in ("send_prompt", "prompt"):
        prompt_txt = str(parameters.get("prompt") or parameters.get("text") or "")
        return mgr.send_prompt(
            text=prompt_txt,
            player=player,
            source=parameters.get("source"),
            poll_in_background=True,
        )

    # 5. Review & Safety Actions
    if sub_act == "review_changes":
        return antigravity_review.review_changes(
            player=player,
            project_folder=str(path or "") if path else None,
        )

    if sub_act == "accept_all":
        return antigravity_review.accept_all(
            player=player,
            project_folder=str(path or "") if path else None,
        )

    if sub_act == "reject_all":
        return antigravity_review.reject_all(player=player)

    if sub_act == "accept_file":
        fname = str(parameters.get("filename") or path or "")
        return antigravity_review.accept_file(filename=fname, player=player)

    if sub_act == "reject_file":
        fname = str(parameters.get("filename") or path or "")
        return antigravity_review.reject_file(filename=fname, player=player)

    # 6. Terminal & Editor Shortcuts
    if sub_act in ("close_all_files", "close_all_editors"):
        return antigravity_terminal.close_all_files(
            save_first=bool(parameters.get("save_first", False)),
            force=bool(parameters.get("force", False)),
            player=player,
        )

    if sub_act in ("new_terminal", "open_terminal"):
        return antigravity_terminal.new_terminal(
            cwd=parameters.get("cwd") or path,
            player=player,
        )

    if sub_act in ("run_terminal_command", "run_command", "terminal_command"):
        cmd_str = str(parameters.get("command") or parameters.get("cmd") or "")
        return antigravity_terminal.run_terminal_command(
            command=cmd_str,
            cwd=parameters.get("cwd") or path,
            timeout=float(parameters.get("timeout", 60.0)),
            player=player,
        )

    # 7. Git & Version Control Shortcuts
    if sub_act in ("git_commit", "commit"):
        msg_str = str(parameters.get("message") or "")
        return version_control.git_commit(
            message=msg_str,
            files=parameters.get("files"),
            cwd=parameters.get("cwd") or path,
            player=player,
        )

    if sub_act in ("git_push", "push"):
        return version_control.git_push(
            remote=str(parameters.get("remote") or "origin"),
            branch=parameters.get("branch"),
            force=bool(parameters.get("force", False)),
            cwd=parameters.get("cwd") or path,
            player=player,
        )

    if sub_act in ("git_status", "status_git"):
        return version_control.git_status(
            cwd=parameters.get("cwd") or path,
        )

    if sub_act in ("git_pull", "pull"):
        return version_control.git_pull(
            remote=str(parameters.get("remote") or "origin"),
            branch=parameters.get("branch"),
            cwd=parameters.get("cwd") or path,
            player=player,
        )

    return outcome.ActionResult(
        status=outcome.STATUS_FAILED,
        action=sub_act or "antigravity_ide",
        success=False,
        verified=False,
        message=f"Unknown Antigravity IDE action: '{sub_act}'.",
        error="Unknown action",
    )
