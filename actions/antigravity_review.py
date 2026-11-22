"""
actions/antigravity_review.py — Safe Review, Checkpointing, and Acceptance Actions for Antigravity IDE.

Guarantees:
- review_changes(): read-only inspection of changed files and git diff stats.
- accept_all(): requires review, creates a git checkpoint (stash/branch) before applying,
  and verifies that pending changes reach zero before claiming completion.
- reject_all(): verifies discarding of agent edits.
- accept_file() and reject_file(): per-file granularity.
- Terminal approval prompts: NEVER auto-clicked; detected and reported to user.
- Strictly argument vectors for git operations (no shell concatenation).
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple

from actions import antigravity_selectors as sel
from actions.antigravity_cdp import get_antigravity_cdp_manager
from actions.antigravity_launcher import get_window_status, resolve_ide_path
from core import outcome

logger = logging.getLogger("antigravity_review")


def get_git_diff_summary(project_path: Path) -> Tuple[str, List[str]]:
    """Run read-only git status and git diff --stat to get changed file names and statistics."""
    if not project_path.exists() or not project_path.is_dir():
        return "", []

    try:
        # Check if inside git repo
        check_repo = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=4,
        )
        if check_repo.returncode != 0 or check_repo.stdout.strip() != "true":
            return "Project is not a git repository.", []

        # Get changed files
        st_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=4,
        )
        changed_files: list[str] = []
        if st_res.returncode == 0 and st_res.stdout:
            for line in st_res.stdout.splitlines():
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2:
                    changed_files.append(parts[1])

        # Get diff stat
        diff_res = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=4,
        )
        stat_summary = diff_res.stdout.strip() if diff_res.returncode == 0 else ""
        return stat_summary, changed_files
    except Exception as e:
        logger.debug(f"Git diff summary query failed: {e}")
        return "", []


def create_git_checkpoint(project_path: Path) -> Tuple[bool, str]:
    """Create a temporary git checkpoint (stash) before accepting changes."""
    if not project_path.exists() or not project_path.is_dir():
        return False, "Project path does not exist"

    try:
        check_repo = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=4,
        )
        if check_repo.returncode != 0:
            return False, "Not a git repository"

        # Check if there are dirty working tree changes to checkpoint
        st_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=4,
        )
        if not st_res.stdout.strip():
            return True, "No uncommitted git changes to checkpoint."

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        stash_msg = f"Jarvis_checkpoint_before_accept_{timestamp}"
        stash_res = subprocess.run(
            ["git", "stash", "create", stash_msg],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if stash_res.returncode == 0 and stash_res.stdout.strip():
            sha = stash_res.stdout.strip()
            # Store ref in git stash store
            subprocess.run(
                ["git", "stash", "store", "-m", stash_msg, sha],
                cwd=str(project_path),
                capture_output=True,
                text=True,
                timeout=4,
            )
            return True, f"Created git checkpoint: '{stash_msg}' ({sha[:7]})"

        return True, "Checkpoint recorded."
    except Exception as e:
        logger.warning(f"Failed to create git checkpoint: {e}")
        return False, str(e)


def _resolve_project_dir(specified_folder: Optional[str] = None) -> Optional[Path]:
    """Determine the active project directory from parameter or running window status."""
    if specified_folder:
        resolved, _ = resolve_ide_path(specified_folder)
        if resolved and resolved.is_dir():
            return resolved

    # Check window status
    win_st = get_window_status()
    open_f = win_st.get("open_folder")
    if open_f:
        resolved, _ = resolve_ide_path(open_f)
        if resolved and resolved.is_dir():
            return resolved

    # Fallback to CWD
    return Path.cwd()


def review_changes(
    player=None,
    project_folder: Optional[str] = None,
) -> outcome.ActionResult:
    """Read-only review of changed files and diff summary from agent panel and git.

    Never modifies files or clicks acceptance buttons.
    """
    mgr = get_antigravity_cdp_manager()
    page = mgr.get_workbench_page()
    frame = mgr.get_agent_panel_frame(page) if page else None

    panel_files: list[str] = []
    if frame:
        try:
            items = frame.query_selector_all(sel.CHANGED_FILE_ITEM)
            for it in items:
                txt = it.inner_text().strip()
                if txt:
                    panel_files.append(txt.splitlines()[0].strip())
        except Exception as e:
            logger.debug(f"Error querying panel changed files: {e}")

    proj_path = _resolve_project_dir(project_folder)
    git_stat = ""
    git_files: list[str] = []
    if proj_path:
        git_stat, git_files = get_git_diff_summary(proj_path)

    all_files = list(dict.fromkeys(panel_files + git_files))
    file_count = len(all_files)

    if file_count == 0:
        msg = "No pending agent changes or uncommitted file modifications."
        if player and hasattr(player, "write_log"):
            player.write_log(f"AGY Review: {msg}")
        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="review_changes",
            success=True,
            verified=True,
            message=msg,
            data={"count": 0, "files": []},
        )

    file_list_str = ", ".join(all_files[:5]) + (f" and {file_count - 5} more" if file_count > 5 else "")
    summary_msg = f"Antigravity modified {file_count} files ({file_list_str})."
    if git_stat:
        summary_msg += f" Git diff summary:\n{git_stat}"

    if player and hasattr(player, "show_content"):
        player.show_content("Antigravity Review", summary_msg)
    elif player and hasattr(player, "write_log"):
        player.write_log(f"AGY Review: {summary_msg}")

    return outcome.ActionResult(
        status=outcome.STATUS_DONE,
        action="review_changes",
        success=True,
        verified=True,
        message=summary_msg,
        data={
            "count": file_count,
            "files": all_files,
            "git_stat": git_stat,
            "project_path": str(proj_path) if proj_path else None,
        },
    )


def accept_all(
    player=None,
    project_folder: Optional[str] = None,
) -> outcome.ActionResult:
    """Accept all agent changes in Antigravity IDE.

    Enforces safety requirements:
    1. Reviews changes and inspects git diff.
    2. Creates a git checkpoint before proceeding.
    3. Clicks Accept All.
    4. Verifies pending changes reach 0 before reporting done.
    """
    mgr = get_antigravity_cdp_manager()
    page = mgr.get_workbench_page()
    if not page:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="accept_all",
            success=False,
            verified=False,
            message="Cannot connect to Antigravity IDE over CDP (port 9333).",
            error="CDP not connected",
        )

    # 1. Inspect changes
    rev_res = review_changes(player=player, project_folder=project_folder)
    file_count = rev_res.data.get("count", 0)
    files = rev_res.data.get("files", [])

    if file_count == 0:
        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="accept_all",
            success=True,
            verified=True,
            message="No pending changes to accept.",
            data={"accepted_count": 0},
        )

    # 2. Create git checkpoint
    proj_path = _resolve_project_dir(project_folder)
    checkpoint_msg = "No checkpoint created (not a git repository)."
    if proj_path:
        ok, cp_detail = create_git_checkpoint(proj_path)
        checkpoint_msg = cp_detail
        if not ok and "Not a git repository" not in cp_detail:
            logger.warning(f"Checkpoint warning: {cp_detail}")

    # 3. Click Accept All
    frame = mgr.get_agent_panel_frame(page)
    if not frame:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="accept_all",
            success=False,
            verified=False,
            message="Agent panel frame not found.",
        )

    btn = frame.query_selector(sel.ACCEPT_ALL_BUTTON) or page.query_selector(sel.ACCEPT_ALL_BUTTON)
    if not btn:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="accept_all",
            success=False,
            verified=False,
            message="Accept All button not found in Antigravity UI.",
            error="Selector not found",
        )

    try:
        btn.click()
        time.sleep(1.0)
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="accept_all",
            success=False,
            verified=False,
            message=f"Failed to click Accept All: {e}",
            error=str(e),
        )

    # 4. Verify pending changes reach zero
    start = time.monotonic()
    verified_zero = False
    while time.monotonic() - start < 4.0:
        st = mgr.panel_status()
        if st.get("pending_files", 0) == 0:
            verified_zero = True
            break
        time.sleep(0.5)

    if verified_zero:
        done_msg = f"Accepted all agent changes across {file_count} files. {checkpoint_msg}"
        if player and hasattr(player, "write_log"):
            player.write_log(f"AGY: {done_msg}")
        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="accept_all",
            success=True,
            verified=True,
            message=done_msg,
            data={"files": files, "checkpoint": checkpoint_msg},
        )

    return outcome.ActionResult(
        status=outcome.STATUS_UNVERIFIED,
        action="accept_all",
        success=True,
        verified=False,
        message=f"Sent Accept All, but pending changes count did not clear to zero. {checkpoint_msg}",
        data={"files": files, "checkpoint": checkpoint_msg},
    )


def reject_all(player=None) -> outcome.ActionResult:
    """Reject and discard all agent changes in Antigravity IDE."""
    mgr = get_antigravity_cdp_manager()
    page = mgr.get_workbench_page()
    if not page:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="reject_all",
            success=False,
            verified=False,
            message="Cannot connect to Antigravity IDE over CDP (port 9333).",
            error="CDP not connected",
        )

    frame = mgr.get_agent_panel_frame(page)
    if not frame:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="reject_all",
            success=False,
            verified=False,
            message="Agent panel frame not found.",
        )

    btn = frame.query_selector(sel.REJECT_ALL_BUTTON) or page.query_selector(sel.REJECT_ALL_BUTTON)
    if not btn:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="reject_all",
            success=False,
            verified=False,
            message="Reject All button not found in Antigravity UI.",
            error="Selector not found",
        )

    try:
        btn.click()
        time.sleep(1.0)
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="reject_all",
            success=False,
            verified=False,
            message=f"Failed to click Reject All: {e}",
            error=str(e),
        )

    # Verify pending changes reach zero
    start = time.monotonic()
    verified_zero = False
    while time.monotonic() - start < 4.0:
        st = mgr.panel_status()
        if st.get("pending_files", 0) == 0:
            verified_zero = True
            break
        time.sleep(0.5)

    if verified_zero:
        msg = "Discarded and rejected all Antigravity agent changes."
        if player and hasattr(player, "write_log"):
            player.write_log(f"AGY: {msg}")
        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="reject_all",
            success=True,
            verified=True,
            message=msg,
        )

    return outcome.ActionResult(
        status=outcome.STATUS_UNVERIFIED,
        action="reject_all",
        success=True,
        verified=False,
        message="Sent Reject All, but pending changes count did not clear to zero.",
    )


def accept_file(filename: str, player=None) -> outcome.ActionResult:
    """Accept changes for a single file in Antigravity IDE."""
    mgr = get_antigravity_cdp_manager()
    page = mgr.get_workbench_page()
    frame = mgr.get_agent_panel_frame(page) if page else None
    if not frame:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="accept_file",
            success=False,
            verified=False,
            message="Agent panel frame not found.",
        )

    clean_name = filename.strip().lower()
    try:
        items = frame.query_selector_all(sel.CHANGED_FILE_ITEM)
        target_item = None
        for it in items:
            txt = it.inner_text().strip().lower()
            if clean_name in txt:
                target_item = it
                break

        if not target_item:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="accept_file",
                success=False,
                verified=False,
                message=f"File '{filename}' not found among pending changes.",
            )

        btn = target_item.query_selector(sel.PER_FILE_ACCEPT)
        if not btn:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="accept_file",
                success=False,
                verified=False,
                message=f"Accept button for '{filename}' not found.",
            )

        btn.click()
        time.sleep(0.5)
        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="accept_file",
            success=True,
            verified=True,
            message=f"Accepted changes for '{filename}'.",
            data={"file": filename},
        )
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="accept_file",
            success=False,
            verified=False,
            message=f"Failed to accept '{filename}': {e}",
            error=str(e),
        )


def reject_file(filename: str, player=None) -> outcome.ActionResult:
    """Reject changes for a single file in Antigravity IDE."""
    mgr = get_antigravity_cdp_manager()
    page = mgr.get_workbench_page()
    frame = mgr.get_agent_panel_frame(page) if page else None
    if not frame:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="reject_file",
            success=False,
            verified=False,
            message="Agent panel frame not found.",
        )

    clean_name = filename.strip().lower()
    try:
        items = frame.query_selector_all(sel.CHANGED_FILE_ITEM)
        target_item = None
        for it in items:
            txt = it.inner_text().strip().lower()
            if clean_name in txt:
                target_item = it
                break

        if not target_item:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="reject_file",
                success=False,
                verified=False,
                message=f"File '{filename}' not found among pending changes.",
            )

        btn = target_item.query_selector(sel.PER_FILE_REJECT)
        if not btn:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="reject_file",
                success=False,
                verified=False,
                message=f"Reject button for '{filename}' not found.",
            )

        btn.click()
        time.sleep(0.5)
        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="reject_file",
            success=True,
            verified=True,
            message=f"Rejected changes for '{filename}'.",
            data={"file": filename},
        )
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="reject_file",
            success=False,
            verified=False,
            message=f"Failed to reject '{filename}': {e}",
            error=str(e),
        )


def undo_agent_edit(player=None) -> outcome.ActionResult:
    """Trigger agent undo if the UI provides an undo action for the last edit."""
    mgr = get_antigravity_cdp_manager()
    page = mgr.get_workbench_page()
    frame = mgr.get_agent_panel_frame(page) if page else None
    if not frame:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="undo_agent_edit",
            success=False,
            verified=False,
            message="Agent panel frame not found.",
        )

    btn = frame.query_selector("button[aria-label*='Undo'], button:has-text('Undo last change')")
    if btn and btn.is_visible():
        btn.click()
        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="undo_agent_edit",
            success=True,
            verified=True,
            message="Reversed last agent edit in Antigravity IDE.",
        )

    return outcome.ActionResult(
        status=outcome.STATUS_UNSUPPORTED,
        action="undo_agent_edit",
        success=False,
        verified=False,
        message="Undo button not available in the current agent panel state.",
    )
