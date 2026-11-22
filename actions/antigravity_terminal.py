"""
actions/antigravity_terminal.py — Terminal instances, programmatic command execution, and editor management for Antigravity IDE.

Covers:
1. Closing all open files (with dirty/unsaved changes guard).
2. Opening new terminal instances in Antigravity IDE.
3. Executing terminal commands programmatically in the project workspace with safety checks.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from actions.antigravity_cdp import get_antigravity_cdp_manager
from actions.antigravity_files import save_file
from actions.antigravity_launcher import get_window_status, is_allowed_root, resolve_ide_path
from core import outcome

logger = logging.getLogger("antigravity_terminal")

_SYSTEM = platform.system()  # "Windows" | "Darwin" | "Linux"

FORBIDDEN_COMMAND_PATTERNS = [
    r"\bformat\s+[a-z]:",
    r"format-volume",
    r"\bdiskpart\b",
    r"\brm\s+-rf\s+/\b",
    r"\brm\s+-rf\s+c:\\\b",
]


def _is_forbidden_command(cmd: str) -> bool:
    clean = cmd.lower().strip()
    for pat in FORBIDDEN_COMMAND_PATTERNS:
        if re.search(pat, clean):
            return True
    return False


def close_all_files(
    save_first: bool = False,
    force: bool = False,
    player=None,
) -> outcome.ActionResult:
    """Close all open editor tabs in Antigravity IDE (Ctrl+K W).

    Guards against losing unsaved changes:
    - If unsaved changes exist and not force and not save_first, requests confirmation.
    - If save_first=True, saves all files before closing.
    """
    st = get_window_status()
    if st.get("is_running") and st.get("has_unsaved_changes") and not force and not save_first:
        return outcome.ActionResult(
            status=outcome.STATUS_NEEDS_CONFIRMATION,
            action="close_all_files",
            success=False,
            verified=False,
            message=(
                f"There are unsaved changes in Antigravity IDE ({st.get('active_file') or 'open files'}). "
                "Specify save_first=True to save before closing, or force=True to close anyway."
            ),
            data={"has_unsaved_changes": True, "active_file": st.get("active_file")},
        )

    if save_first:
        save_file(save_all=True)
        time.sleep(0.3)

    # 1. Try via CDP
    closed_via_cdp = False
    try:
        mgr = get_antigravity_cdp_manager()
        page = mgr.get_workbench_page()
        if page:
            if _SYSTEM == "Darwin":
                page.keyboard.press("Meta+k")
                time.sleep(0.05)
                page.keyboard.press("w")
            else:
                page.keyboard.press("Control+k")
                time.sleep(0.05)
                page.keyboard.press("w")
            closed_via_cdp = True
    except Exception as e:
        logger.debug(f"CDP close all editors attempt failed: {e}")

    # 2. Hotkey fallback via pyautogui if CDP was unavailable or failed
    if not closed_via_cdp:
        try:
            import pyautogui
            if _SYSTEM == "Darwin":
                pyautogui.hotkey("command", "k")
                time.sleep(0.05)
                pyautogui.press("w")
            else:
                pyautogui.hotkey("ctrl", "k")
                time.sleep(0.05)
                pyautogui.press("w")
        except Exception as e:
            logger.debug(f"PyAutoGUI close all editors failed: {e}")

    time.sleep(0.5)
    msg = "Closed all open editor files in Antigravity IDE."
    if player and hasattr(player, "write_log"):
        player.write_log(f"AGY: {msg}")

    return outcome.ActionResult(
        status=outcome.STATUS_DONE,
        action="close_all_files",
        success=True,
        verified=True,
        message=msg,
    )


def new_terminal(
    cwd: Optional[str] = None,
    player=None,
) -> outcome.ActionResult:
    """Open a new terminal instance in Antigravity IDE (Ctrl+Shift+`)."""
    target_cwd: Optional[Path] = None
    if cwd:
        resolved, _ = resolve_ide_path(cwd)
        if resolved and resolved.is_dir():
            target_cwd = resolved
            if not is_allowed_root(target_cwd):
                return outcome.ActionResult(
                    status=outcome.STATUS_BLOCKED,
                    action="new_terminal",
                    success=False,
                    verified=False,
                    message=f"Access denied: '{target_cwd}' is outside allowed project directories.",
                    error="Outside allowed roots",
                )

    opened = False
    # 1. Try via CDP
    try:
        mgr = get_antigravity_cdp_manager()
        page = mgr.get_workbench_page()
        if page:
            if _SYSTEM == "Darwin":
                page.keyboard.press("Control+Shift+Backquote")
            else:
                page.keyboard.press("Control+Shift+Backquote")
            opened = True
    except Exception as e:
        logger.debug(f"CDP new terminal failed: {e}")

    # 2. Hotkey fallback
    if not opened:
        try:
            import pyautogui
            pyautogui.hotkey("ctrl", "shift", "`")
            opened = True
        except Exception as e:
            logger.debug(f"PyAutoGUI new terminal failed: {e}")

    cwd_info = f" in '{target_cwd}'" if target_cwd else ""
    msg = f"Opened a new terminal instance in Antigravity IDE{cwd_info}."
    if player and hasattr(player, "write_log"):
        player.write_log(f"AGY: {msg}")

    return outcome.ActionResult(
        status=outcome.STATUS_DONE,
        action="new_terminal",
        success=True,
        verified=True,
        message=msg,
        data={"cwd": str(target_cwd) if target_cwd else None},
    )


def run_terminal_command(
    command: str,
    cwd: Optional[str] = None,
    timeout: float = 60.0,
    player=None,
) -> outcome.ActionResult:
    """Execute a terminal command programmatically in the project workspace."""
    clean_cmd = str(command or "").strip()
    if not clean_cmd:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="run_terminal_command",
            success=False,
            verified=False,
            message="Command cannot be empty.",
            error="Empty command",
        )

    if _is_forbidden_command(clean_cmd):
        return outcome.ActionResult(
            status=outcome.STATUS_BLOCKED,
            action="run_terminal_command",
            success=False,
            verified=False,
            message="Command is strictly prohibited by security policy (destructive operation).",
            error="FORBIDDEN_COMMAND",
        )

    # Determine working directory
    target_dir: Optional[Path] = None
    if cwd:
        resolved, _ = resolve_ide_path(cwd)
        if resolved and resolved.is_dir():
            target_dir = resolved

    if not target_dir:
        st = get_window_status()
        open_f = st.get("open_folder")
        if open_f:
            resolved_proj, _ = resolve_ide_path(open_f)
            if resolved_proj and resolved_proj.is_dir():
                target_dir = resolved_proj

    if not target_dir:
        target_dir = Path.cwd()

    if not is_allowed_root(target_dir):
        return outcome.ActionResult(
            status=outcome.STATUS_BLOCKED,
            action="run_terminal_command",
            success=False,
            verified=False,
            message=f"Access denied: '{target_dir}' is outside allowed directories.",
            error="Outside allowed roots",
        )

    if player and hasattr(player, "write_log"):
        player.write_log(f"AGY Terminal: Running `{clean_cmd}` in {target_dir.name}/")

    try:
        proc = subprocess.run(
            clean_cmd,
            cwd=str(target_dir),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        stdout_txt = proc.stdout.strip()
        stderr_txt = proc.stderr.strip()

        # Build clean output preview
        combined_output = stdout_txt if stdout_txt else stderr_txt
        output_preview = combined_output[:300] + ("..." if len(combined_output) > 300 else "")
        if not output_preview:
            output_preview = "(no output)"

        if proc.returncode == 0:
            msg = f"Terminal command completed successfully (exit 0):\n{output_preview}"
            return outcome.ActionResult(
                status=outcome.STATUS_DONE,
                action="run_terminal_command",
                success=True,
                verified=True,
                message=msg,
                data={
                    "command": clean_cmd,
                    "exit_code": 0,
                    "stdout": stdout_txt,
                    "stderr": stderr_txt,
                    "cwd": str(target_dir),
                },
            )

        err_msg = f"Terminal command failed with exit code {proc.returncode}:\n{output_preview}"
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="run_terminal_command",
            success=False,
            verified=True,
            message=err_msg,
            error=stderr_txt or f"Exit code {proc.returncode}",
            data={
                "command": clean_cmd,
                "exit_code": proc.returncode,
                "stdout": stdout_txt,
                "stderr": stderr_txt,
                "cwd": str(target_dir),
            },
        )
    except subprocess.TimeoutExpired:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="run_terminal_command",
            success=False,
            verified=False,
            message=f"Terminal command timed out after {timeout} seconds.",
            error="Timeout",
        )
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="run_terminal_command",
            success=False,
            verified=False,
            message=f"Failed to execute command: {e}",
            error=str(e),
        )
