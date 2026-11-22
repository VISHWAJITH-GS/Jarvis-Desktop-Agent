"""
actions/antigravity_files.py — Atomic file and directory operations with undo checkpoints for Antigravity IDE.

Guarantees:
- No GUI typing for code/files.
- Atomic writes via temp file in same filesystem + atomic os.replace.
- Automatic undo registration via core.undo before any overwrite.
- Read-back verification of written bytes.
- Protection against silent overwrites of files with unsaved IDE edits.
- Automatic opening in Antigravity IDE after write.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from actions.antigravity_launcher import (
    find_antigravity_launcher,
    get_window_status,
    is_allowed_root,
    open_file,
    resolve_ide_path,
)
from core import outcome
from core.undo import push_undo

logger = logging.getLogger("antigravity_files")


def _atomic_write_text(target: Path, content: str) -> None:
    """Write text atomically using a temp file in the same directory, followed by replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = target.parent
    # Use delete=False so we can atomically rename it
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=temp_dir,
        delete=False,
        prefix=f".tmp_{target.name}_",
    ) as tf:
        tf.write(content)
        tf.flush()
        os.fsync(tf.fileno())
        temp_name = tf.name

    os.replace(temp_name, target)


def create_file(
    path: str,
    content: str = "",
    launcher_path: Optional[str] = None,
    open_in_ide_after: bool = True,
) -> outcome.ActionResult:
    """Create a new file atomically with content and open it in Antigravity IDE.

    Refuses to overwrite an existing file.
    """
    resolved, candidates = resolve_ide_path(path)
    if not resolved:
        resolved = Path(path).expanduser().resolve()

    if not is_allowed_root(resolved):
        return outcome.ActionResult(
            status=outcome.STATUS_BLOCKED,
            action="create_file",
            success=False,
            verified=False,
            message=f"Access denied: '{resolved}' is outside allowed directories.",
            error="Outside allowed roots",
        )

    if resolved.exists():
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="create_file",
            success=False,
            verified=False,
            message=f"File already exists: '{resolved.name}'. Use write_file to overwrite.",
            error="File exists",
        )

    try:
        _atomic_write_text(resolved, content)

        # Register undo: delete created file
        def _undo_create():
            if resolved.exists():
                resolved.unlink()
                return f"Removed created file '{resolved.name}'."
            return f"'{resolved.name}' already deleted."

        push_undo(f"create file '{resolved.name}'", _undo_create)

        # Read-back verification
        read_back = resolved.read_text(encoding="utf-8")
        if read_back != content:
            return outcome.ActionResult(
                status=outcome.STATUS_UNVERIFIED,
                action="create_file",
                success=False,
                verified=False,
                message=f"Read-back verification failed for '{resolved.name}'.",
                error="Content mismatch",
            )

        bytes_written = len(content.encode("utf-8"))

        # Open in IDE
        if open_in_ide_after:
            open_file(str(resolved), launcher_path=launcher_path)

        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="create_file",
            success=True,
            verified=True,
            message=f"Created '{resolved.name}' ({bytes_written} bytes) and opened in Antigravity IDE.",
            data={"path": str(resolved), "bytes": bytes_written},
        )
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="create_file",
            success=False,
            verified=False,
            message=f"Failed to create file '{resolved.name}': {e}",
            error=str(e),
        )


def write_file(
    path: str,
    content: str,
    mode: str = "overwrite",
    allow_overwrite: bool = True,
    launcher_path: Optional[str] = None,
    open_in_ide_after: bool = True,
) -> outcome.ActionResult:
    """Write or append content to a file atomically, with backup to undo stack."""
    resolved, candidates = resolve_ide_path(path)
    if not resolved:
        resolved = Path(path).expanduser().resolve()

    if not is_allowed_root(resolved):
        return outcome.ActionResult(
            status=outcome.STATUS_BLOCKED,
            action="write_file",
            success=False,
            verified=False,
            message=f"Access denied: '{resolved}' is outside allowed directories.",
            error="Outside allowed roots",
        )

    # Check if file has unsaved changes in IDE
    status = get_window_status(launcher_path=launcher_path)
    if status.get("is_running") and status.get("has_unsaved_changes"):
        active = (status.get("active_file") or "").lower()
        if active == resolved.name.lower():
            return outcome.ActionResult(
                status=outcome.STATUS_NEEDS_CONFIRMATION,
                action="write_file",
                success=False,
                verified=False,
                message=f"'{resolved.name}' has unsaved changes in Antigravity IDE. Overwriting will discard your edits. Please save or confirm.",
                data={"path": str(resolved), "dirty": True},
            )

    old_content: Optional[str] = None
    if resolved.exists():
        if mode == "overwrite" and not allow_overwrite:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="write_file",
                success=False,
                verified=False,
                message=f"File '{resolved.name}' already exists. Overwrite permission required.",
                error="Overwrite refused",
            )
        try:
            old_content = resolved.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"Failed to read previous contents for undo backup: {e}")

    final_content = content
    if mode == "append" and old_content is not None:
        final_content = old_content + content

    try:
        _atomic_write_text(resolved, final_content)

        # Register undo backup
        if old_content is not None:
            def _undo_write():
                if resolved.exists():
                    _atomic_write_text(resolved, old_content)
                    return f"Restored previous contents of '{resolved.name}'."
                return f"'{resolved.name}' no longer exists."

            push_undo(f"write to '{resolved.name}'", _undo_write)
        else:
            def _undo_new():
                if resolved.exists():
                    resolved.unlink()
                    return f"Removed created file '{resolved.name}'."
                return f"'{resolved.name}' already deleted."

            push_undo(f"create file '{resolved.name}'", _undo_new)

        # Read-back verification
        read_back = resolved.read_text(encoding="utf-8")
        if read_back != final_content:
            return outcome.ActionResult(
                status=outcome.STATUS_UNVERIFIED,
                action="write_file",
                success=False,
                verified=False,
                message=f"Read-back verification failed for '{resolved.name}'.",
                error="Content mismatch",
            )

        bytes_written = len(content.encode("utf-8"))

        if open_in_ide_after:
            open_file(str(resolved), launcher_path=launcher_path)

        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="write_file",
            success=True,
            verified=True,
            message=f"Wrote {bytes_written} bytes to '{resolved.name}' and opened in Antigravity IDE.",
            data={"path": str(resolved), "bytes": bytes_written, "mode": mode},
        )
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="write_file",
            success=False,
            verified=False,
            message=f"Failed to write file '{resolved.name}': {e}",
            error=str(e),
        )


def create_folder(
    path: str,
    launcher_path: Optional[str] = None,
) -> outcome.ActionResult:
    """Create a folder inside allowed directories and record undo."""
    resolved, candidates = resolve_ide_path(path)
    if not resolved:
        resolved = Path(path).expanduser().resolve()

    if not is_allowed_root(resolved):
        return outcome.ActionResult(
            status=outcome.STATUS_BLOCKED,
            action="create_folder",
            success=False,
            verified=False,
            message=f"Access denied: '{resolved}' is outside allowed directories.",
            error="Outside allowed roots",
        )

    if resolved.exists():
        if resolved.is_dir():
            return outcome.ActionResult(
                status=outcome.STATUS_DONE,
                action="create_folder",
                success=True,
                verified=True,
                message=f"Folder already exists: '{resolved.name}'.",
                data={"path": str(resolved)},
            )
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="create_folder",
            success=False,
            verified=False,
            message=f"Path exists and is not a folder: '{resolved.name}'.",
            error="Path is a file",
        )

    try:
        resolved.mkdir(parents=True, exist_ok=True)

        def _undo_mkdir():
            if resolved.exists() and resolved.is_dir():
                if not any(resolved.iterdir()):
                    resolved.rmdir()
                    return f"Removed folder '{resolved.name}'."
                return f"Folder '{resolved.name}' is not empty — not deleted."
            return f"Folder '{resolved.name}' already deleted."

        push_undo(f"create folder '{resolved.name}'", _undo_mkdir)

        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="create_folder",
            success=True,
            verified=True,
            message=f"Created folder '{resolved.name}'.",
            data={"path": str(resolved)},
        )
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="create_folder",
            success=False,
            verified=False,
            message=f"Failed to create folder '{resolved.name}': {e}",
            error=str(e),
        )


def save_file(
    path: Optional[str] = None,
    save_all: bool = False,
    launcher_path: Optional[str] = None,
) -> outcome.ActionResult:
    """Trigger save shortcut in IDE via hotkey or CDP."""
    try:
        import pyautogui

        # Focus window or send save shortcut
        if save_all:
            pyautogui.hotkey("ctrl", "k", "s")
            msg = "Triggered Save All in Antigravity IDE."
        else:
            pyautogui.hotkey("ctrl", "s")
            msg = f"Triggered Save in Antigravity IDE" + (f" for '{path}'" if path else "") + "."

        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="save_file",
            success=True,
            verified=True,
            message=msg,
            data={"save_all": save_all, "path": path},
        )
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="save_file",
            success=False,
            verified=False,
            message=f"Failed to execute save shortcut: {e}",
            error=str(e),
        )
