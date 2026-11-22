"""
actions/version_control.py — Git and GitHub Version Control Operations for JARVIS.

Provides reliable, verified operations for:
- git_status: inspect current branch, staged, unstaged, and untracked changes.
- git_commit: stage files and commit with a clean descriptive message.
- git_push: push committed changes to remote repository (with verification).
- git_pull: synchronize and pull updates from remote repository.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from actions.antigravity_launcher import get_window_status, is_allowed_root, resolve_ide_path
from core import outcome

logger = logging.getLogger("version_control")

TOOL = {
    "name": "version_control",
    "description": (
        "Execute Git and GitHub version control shortcuts: inspect status, commit changes "
        "with a message, push to remote repository, and pull updates. All actions run "
        "in the active workspace repository with verified results."
    ),
    "brief": "Git & GitHub version control: status, commit, push, pull.",
    "tier": "core",
    "permission": "normal",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Version control action: 'status', 'commit', 'push', 'pull'.",
                "enum": ["status", "commit", "push", "pull"],
            },
            "message": {
                "type": "STRING",
                "description": "Commit message describing the changes (required for 'commit').",
            },
            "branch": {
                "type": "STRING",
                "description": "Target Git branch name (defaults to active branch).",
            },
            "remote": {
                "type": "STRING",
                "description": "Target Git remote name (default: 'origin').",
            },
            "files": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Specific list of file paths to stage and commit. Defaults to all modified files.",
            },
            "cwd": {
                "type": "STRING",
                "description": "Repository working directory (defaults to active Antigravity IDE project).",
            },
            "force": {
                "type": "BOOLEAN",
                "description": "Push with lease protection (--force-with-lease).",
            },
        },
        "required": ["action"],
    },
    "handler": "version_control",
}


def _resolve_repo_dir(cwd: Optional[str] = None) -> Tuple[Optional[Path], Optional[str]]:
    """Determine the active git directory and verify it is a git work tree."""
    target: Optional[Path] = None
    if cwd:
        resolved, _ = resolve_ide_path(cwd)
        if resolved and resolved.is_dir():
            target = resolved

    if not target:
        st = get_window_status()
        open_f = st.get("open_folder")
        if open_f:
            resolved_proj, _ = resolve_ide_path(open_f)
            if resolved_proj and resolved_proj.is_dir():
                target = resolved_proj

    if not target:
        target = Path.cwd()

    if not is_allowed_root(target):
        return None, f"Access denied: '{target}' is outside allowed directories."

    try:
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=4,
        )
        if check.returncode != 0 or check.stdout.strip() != "true":
            return None, f"Directory '{target.name}' is not a Git repository."
    except Exception as e:
        return None, f"Failed to verify Git repository: {e}"

    return target, None


def git_status(cwd: Optional[str] = None) -> outcome.ActionResult:
    """Check working tree status, branch name, and uncommitted modifications."""
    repo_dir, err = _resolve_repo_dir(cwd)
    if not repo_dir:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="git_status",
            success=False,
            verified=False,
            message=err or "Invalid Git repository.",
            error=err,
        )

    try:
        res = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="git_status",
                success=False,
                verified=False,
                message=f"Git status failed: {res.stderr.strip()}",
                error=res.stderr.strip(),
            )

        lines = res.stdout.strip().splitlines()
        branch_line = lines[0] if lines else "## unknown"
        change_lines = lines[1:] if len(lines) > 1 else []

        staged: list[str] = []
        modified: list[str] = []
        untracked: list[str] = []

        for l in change_lines:
            status_code = l[:2]
            fpath = l[3:].strip()
            if status_code.startswith("?"):
                untracked.append(fpath)
            elif status_code[0] in ("M", "A", "D", "R"):
                staged.append(fpath)
            elif status_code[1] in ("M", "D"):
                modified.append(fpath)

        clean = len(change_lines) == 0
        summary = f"Git Status ({branch_line.lstrip('#').strip()}): "
        if clean:
            summary += "Working tree clean."
        else:
            summary += f"{len(staged)} staged, {len(modified)} modified, {len(untracked)} untracked."

        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="git_status",
            success=True,
            verified=True,
            message=summary,
            data={
                "branch_info": branch_line,
                "clean": clean,
                "staged": staged,
                "modified": modified,
                "untracked": untracked,
                "cwd": str(repo_dir),
            },
        )
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="git_status",
            success=False,
            verified=False,
            message=f"Error checking Git status: {e}",
            error=str(e),
        )


def git_commit(
    message: str,
    files: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    player=None,
) -> outcome.ActionResult:
    """Stage files and commit changes to the local Git repository."""
    clean_msg = str(message or "").strip()
    if not clean_msg:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="git_commit",
            success=False,
            verified=False,
            message="Commit message cannot be empty.",
            error="Empty message",
        )

    repo_dir, err = _resolve_repo_dir(cwd)
    if not repo_dir:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="git_commit",
            success=False,
            verified=False,
            message=err or "Invalid Git repository.",
            error=err,
        )

    try:
        # 1. Stage changes
        if files:
            add_args = ["git", "add"] + [str(f) for f in files]
        else:
            add_args = ["git", "add", "-A"]

        add_res = subprocess.run(add_args, cwd=str(repo_dir), capture_output=True, text=True, timeout=10)
        if add_res.returncode != 0:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="git_commit",
                success=False,
                verified=False,
                message=f"Git add failed: {add_res.stderr.strip()}",
                error=add_res.stderr.strip(),
            )

        # 2. Check if anything is staged
        diff_cached = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )
        staged_files = [line.strip() for line in diff_cached.stdout.splitlines() if line.strip()]
        if not staged_files:
            return outcome.ActionResult(
                status=outcome.STATUS_DONE,
                action="git_commit",
                success=True,
                verified=True,
                message="No staged changes to commit (working tree clean).",
                data={"committed": False, "files": []},
            )

        # 3. Create commit
        commit_res = subprocess.run(
            ["git", "commit", "-m", clean_msg],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if commit_res.returncode != 0:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="git_commit",
                success=False,
                verified=False,
                message=f"Git commit failed: {commit_res.stderr.strip()}",
                error=commit_res.stderr.strip(),
            )

        # 4. Read commit hash
        sha_res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=3,
        )
        commit_sha = sha_res.stdout.strip() if sha_res.returncode == 0 else "HEAD"

        done_msg = f"Committed {len(staged_files)} files ({commit_sha}): \"{clean_msg}\"."
        if player and hasattr(player, "write_log"):
            player.write_log(f"Git: {done_msg}")

        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="git_commit",
            success=True,
            verified=True,
            message=done_msg,
            data={"commit_sha": commit_sha, "message": clean_msg, "files": staged_files},
        )
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="git_commit",
            success=False,
            verified=False,
            message=f"Git commit error: {e}",
            error=str(e),
        )


def git_push(
    remote: str = "origin",
    branch: Optional[str] = None,
    force: bool = False,
    cwd: Optional[str] = None,
    player=None,
) -> outcome.ActionResult:
    """Push local commits to the remote repository (GitHub/remote)."""
    repo_dir, err = _resolve_repo_dir(cwd)
    if not repo_dir:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="git_push",
            success=False,
            verified=False,
            message=err or "Invalid Git repository.",
            error=err,
        )

    try:
        # Determine target branch
        target_branch = branch
        if not target_branch:
            br_res = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=3,
            )
            target_branch = br_res.stdout.strip() if br_res.returncode == 0 else "main"

        push_args = ["git", "push", remote, target_branch]
        if force:
            push_args.append("--force-with-lease")

        if player and hasattr(player, "write_log"):
            player.write_log(f"Git: Pushing branch '{target_branch}' to remote '{remote}'...")

        push_res = subprocess.run(
            push_args,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=45,
        )

        if push_res.returncode == 0:
            done_msg = f"Successfully pushed branch '{target_branch}' to '{remote}'."
            if player and hasattr(player, "write_log"):
                player.write_log(f"Git: {done_msg}")
            return outcome.ActionResult(
                status=outcome.STATUS_DONE,
                action="git_push",
                success=True,
                verified=True,
                message=done_msg,
                data={"remote": remote, "branch": target_branch, "cwd": str(repo_dir)},
            )

        err_detail = push_res.stderr.strip() or push_res.stdout.strip()
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="git_push",
            success=False,
            verified=True,
            message=f"Git push failed: {err_detail}",
            error=err_detail,
        )
    except subprocess.TimeoutExpired:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="git_push",
            success=False,
            verified=False,
            message=f"Git push to '{remote}' timed out after 45 seconds.",
            error="Timeout",
        )
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="git_push",
            success=False,
            verified=False,
            message=f"Git push error: {e}",
            error=str(e),
        )


def git_pull(
    remote: str = "origin",
    branch: Optional[str] = None,
    cwd: Optional[str] = None,
    player=None,
) -> outcome.ActionResult:
    """Pull latest updates from remote repository."""
    repo_dir, err = _resolve_repo_dir(cwd)
    if not repo_dir:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="git_pull",
            success=False,
            verified=False,
            message=err or "Invalid Git repository.",
            error=err,
        )

    try:
        pull_args = ["git", "pull", remote]
        if branch:
            pull_args.append(branch)

        res = subprocess.run(
            pull_args,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )

        if res.returncode == 0:
            msg = f"Git pull completed successfully:\n{res.stdout.strip()}"
            return outcome.ActionResult(
                status=outcome.STATUS_DONE,
                action="git_pull",
                success=True,
                verified=True,
                message=msg,
                data={"output": res.stdout.strip()},
            )

        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="git_pull",
            success=False,
            verified=False,
            message=f"Git pull failed: {res.stderr.strip()}",
            error=res.stderr.strip(),
        )
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="git_pull",
            success=False,
            verified=False,
            message=f"Git pull error: {e}",
            error=str(e),
        )


def version_control(parameters: dict, player=None) -> outcome.ActionResult:
    """Authoritative handler for Git and GitHub version control actions."""
    action = str(parameters.get("action") or "status").lower().strip()
    cwd = parameters.get("cwd") or parameters.get("path") or parameters.get("folder")

    if action == "status":
        return git_status(cwd=cwd)

    if action == "commit":
        msg = str(parameters.get("message") or "")
        files = parameters.get("files")
        return git_commit(message=msg, files=files, cwd=cwd, player=player)

    if action == "push":
        rem = str(parameters.get("remote") or "origin")
        br = parameters.get("branch")
        force = bool(parameters.get("force", False))
        return git_push(remote=rem, branch=br, force=force, cwd=cwd, player=player)

    if action == "pull":
        rem = str(parameters.get("remote") or "origin")
        br = parameters.get("branch")
        return git_pull(remote=rem, branch=br, cwd=cwd, player=player)

    return outcome.ActionResult(
        status=outcome.STATUS_FAILED,
        action=action,
        success=False,
        verified=False,
        message=f"Unknown version control action: '{action}'. Choose from: status, commit, push, pull.",
        error="Unknown action",
    )
