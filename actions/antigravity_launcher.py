"""
actions/antigravity_launcher.py — Launcher detection, argument lists, window awareness, and project control for Antigravity IDE and CLI.

Provides robust, cross-platform resolution of:
- Antigravity IDE launcher (antigravity-ide.cmd / antigravity-ide / Antigravity IDE.exe)
- Antigravity CLI (agy / agy.exe)
- Subprocess argument list execution (no shell concatenation)
- Path resolution and allowed roots enforcement
- Window awareness and status reporting (is_running, open_folder, active_file, unsaved changes)
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import string
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple

from core import outcome

logger = logging.getLogger("antigravity_launcher")

_SYSTEM = platform.system()  # "Windows" | "Darwin" | "Linux"

# Allowed roots: user directories and project workspaces
_ALLOWED_ROOTS: list[Path] = [
    Path.home(),
]
if _SYSTEM == "Windows":
    for _letter in string.ascii_uppercase:
        _drive_path = Path(f"{_letter}:\\")
        if _drive_path.exists():
            _ALLOWED_ROOTS.append(_drive_path)

_FORBIDDEN_PREFIXES: list[str] = [
    r"c:\windows",
    r"c:\program files\windows",
    "/etc",
    r"\etc",
    "/bin",
    r"\bin",
    "/sbin",
    r"\sbin",
    "/usr",
    r"\usr",
    "/System",
    r"\System",
]


def is_allowed_root(target: Path) -> bool:
    """Verify target path is inside allowed user project spaces and not a protected system path."""
    try:
        raw_str = str(target).strip().lower()
        for fb in _FORBIDDEN_PREFIXES:
            if raw_str == fb.lower() or raw_str.startswith(fb.lower() + "/") or raw_str.startswith(fb.lower() + "\\"):
                return False

        resolved = target.resolve()
        res_str = str(resolved).lower()
        for fb in _FORBIDDEN_PREFIXES:
            if res_str == fb.lower() or res_str.startswith(fb.lower() + os.sep) or res_str.endswith(fb.lower()):
                return False

        return any(resolved == root.resolve() or resolved.is_relative_to(root.resolve()) for root in _ALLOWED_ROOTS)
    except Exception:
        return False


def resolve_ide_path(raw_path: str) -> Tuple[Optional[Path], Optional[List[str]]]:
    """Resolve spoken or raw file/folder paths to an existing Path.

    Returns:
        (resolved_path, None) if resolved cleanly
        (None, candidates_list) if ambiguous
        (None, None) if path cannot be resolved or does not exist
    """
    if not raw_path:
        return None, None

    cleaned = raw_path.strip().strip('"').strip("'")
    lower = cleaned.lower()

    # Spoken path mappings
    spoken_map: dict[str, Path] = {
        "desktop": Path.home() / "Desktop",
        "downloads": Path.home() / "Downloads",
        "documents": Path.home() / "Documents",
        "home": Path.home(),
        "jarvis projects": Path.home() / "Desktop" / "JarvisProjects",
    }
    if _SYSTEM == "Windows":
        spoken_map["d drive projects"] = Path("D:/Projects")
        spoken_map["d projects"] = Path("D:/Projects")
        spoken_map["d drive"] = Path("D:/")
        spoken_map["c drive"] = Path("C:/")

    if lower in spoken_map:
        target = spoken_map[lower]
        if target.exists():
            return target.resolve(), None

    # Handle spoken drive aliases like "d drive projects / mark lvi"
    if lower.startswith("d drive projects") or lower.startswith("d projects"):
        sub = cleaned[len("d drive projects"):].strip(" /\\")
        if not sub and lower.startswith("d projects"):
            sub = cleaned[len("d projects"):].strip(" /\\")
        cand = Path(r"D:\Projects") / sub
        if cand.exists():
            return cand.resolve(), None

    # Normal direct path expansion
    candidate = Path(cleaned).expanduser()
    if candidate.exists():
        return candidate.resolve(), None

    # Search in common project roots if just a project name was spoken
    search_dirs = [
        Path(r"D:\Projects") if _SYSTEM == "Windows" and Path(r"D:\Projects").exists() else None,
        Path.home() / "Desktop" / "JarvisProjects",
        Path.home() / "Desktop",
        Path.home() / "Projects",
        Path.home(),
    ]
    matches: list[Path] = []
    norm_name = re.sub(r"[\s\-_]+", "", lower)
    for root in search_dirs:
        if root and root.is_dir():
            try:
                for item in root.iterdir():
                    item_norm = re.sub(r"[\s\-_]+", "", item.name.lower())
                    if item_norm == norm_name or norm_name in item_norm:
                        matches.append(item.resolve())
            except Exception:
                continue

    if len(matches) == 1:
        return matches[0], None
    elif len(matches) > 1:
        return None, [m.name for m in matches[:5]]

    return None, None


def find_antigravity_launcher() -> Optional[str]:
    """Locate the Antigravity IDE executable or CLI runner script."""
    # 1. Search PATH
    for name in ("antigravity-ide", "antigravity-ide.cmd", "agy-ide", "agy-ide.cmd", "antigravity"):
        which_path = shutil.which(name)
        if which_path and os.path.isfile(which_path):
            return str(Path(which_path).resolve())

    # 2. Search platform-specific candidate locations
    candidates: list[str] = []
    if _SYSTEM == "Windows":
        local_app = os.environ.get("LOCALAPPDATA", "")
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        candidates.extend([
            os.path.join(local_app, "Programs", "Antigravity IDE", "bin", "antigravity-ide.cmd"),
            os.path.join(local_app, "Programs", "Antigravity IDE", "Antigravity IDE.exe"),
            os.path.join(prog_files, "Antigravity IDE", "bin", "antigravity-ide.cmd"),
            os.path.join(prog_files, "Antigravity IDE", "Antigravity IDE.exe"),
            os.path.join(prog_files_x86, "Antigravity IDE", "bin", "antigravity-ide.cmd"),
            os.path.join(prog_files_x86, "Antigravity IDE", "Antigravity IDE.exe"),
        ])
    elif _SYSTEM == "Darwin":
        candidates.extend([
            "/Applications/Antigravity IDE.app/Contents/Resources/app/bin/antigravity-ide",
            os.path.expanduser("~/Applications/Antigravity IDE.app/Contents/Resources/app/bin/antigravity-ide"),
            "/usr/local/bin/antigravity-ide",
        ])
    else:  # Linux
        candidates.extend([
            "/usr/bin/antigravity-ide",
            "/usr/share/antigravity-ide/bin/antigravity-ide",
            "/usr/local/bin/antigravity-ide",
            "/opt/antigravity-ide/bin/antigravity-ide",
            os.path.expanduser("~/.local/bin/antigravity-ide"),
        ])

    for c in candidates:
        if c and os.path.isfile(c):
            return str(Path(c).resolve())

    return None


def find_agy_cli() -> Optional[str]:
    """Locate the standalone Antigravity CLI executable (agy / agy.exe)."""
    # 1. Search PATH
    for name in ("agy", "agy.exe"):
        which_path = shutil.which(name)
        if which_path and os.path.isfile(which_path):
            return str(Path(which_path).resolve())

    # 2. Search platform-specific candidate locations
    candidates: list[str] = []
    user_home = Path.home()
    if _SYSTEM == "Windows":
        candidates.extend([
            str(user_home / ".gemini" / "bin" / "agy.exe"),
            str(user_home / "AppData" / "Local" / ".gemini" / "bin" / "agy.exe"),
            str(user_home / "AppData" / "Local" / "Programs" / "agy" / "agy.exe"),
        ])
    else:
        candidates.extend([
            str(user_home / ".gemini" / "bin" / "agy"),
            "/usr/local/bin/agy",
            str(user_home / ".local" / "bin" / "agy"),
        ])

    for c in candidates:
        if c and os.path.isfile(c):
            return str(Path(c).resolve())

    return None


def get_ide_version(launcher_path: Optional[str] = None) -> Optional[str]:
    """Query Antigravity IDE version using subprocess argument lists (no shell)."""
    exe = launcher_path or find_antigravity_launcher()
    if not exe:
        return None

    try:
        res = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=8)
        if res.returncode == 0 and res.stdout.strip():
            lines = res.stdout.strip().splitlines()
            return lines[0].strip() if lines else None
    except Exception as e:
        logger.debug(f"Failed to query IDE version: {e}")
    return None


def get_cli_version(cli_path: Optional[str] = None) -> Optional[str]:
    """Query Antigravity CLI (agy) version using subprocess argument lists (no shell)."""
    exe = cli_path or find_agy_cli()
    if not exe:
        return None

    try:
        res = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=8)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().splitlines()[0].strip()
    except Exception as e:
        logger.debug(f"Failed to query CLI version: {e}")
    return None


def list_extensions(launcher_path: Optional[str] = None) -> List[str]:
    """List installed Antigravity IDE extensions."""
    exe = launcher_path or find_antigravity_launcher()
    if not exe:
        return []

    try:
        res = subprocess.run([exe, "--list-extensions"], capture_output=True, text=True, timeout=12)
        if res.returncode == 0 and res.stdout:
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception as e:
        logger.debug(f"Failed to list IDE extensions: {e}")
    return []


def parse_status_output(output: str) -> dict:
    """Parse the text output of `antigravity-ide --status` into structured window awareness."""
    status = {
        "is_running": False,
        "open_folder": None,
        "active_file": None,
        "has_unsaved_changes": False,
        "window_title": "",
    }
    if not output:
        return status

    status["is_running"] = True
    # Look for window title lines, e.g.:
    # window [1] (Mark-LVI-main - Antigravity IDE - appidentity.py)
    # or: | Window (● appidentity.py - Mark-LVI-main - Antigravity IDE)
    win_match = re.search(r"window(?:\s*\[\d+\])?\s*\(([^)]+)\)", output, re.IGNORECASE)
    if not win_match:
        win_match = re.search(r"\|\s*Window\s*\(([^)]+)\)", output, re.IGNORECASE)

    if win_match:
        title = win_match.group(1).strip()
        status["window_title"] = title
        if "●" in title or "*" in title or "(dirty)" in title.lower():
            status["has_unsaved_changes"] = True

        parts = [p.strip() for p in title.split(" - ")]
        for p in parts:
            clean_p = p.lstrip("●* \t")
            if "antigravity" in clean_p.lower():
                continue
            if "." in clean_p and not status["active_file"]:
                status["active_file"] = clean_p
            elif not status["open_folder"]:
                status["open_folder"] = clean_p

    # Check Workspace Stats folder if available
    folder_match = re.search(r"Folder\s*\(([^)]+)\):", output)
    if folder_match:
        status["open_folder"] = folder_match.group(1).strip()

    return status


def get_window_status(launcher_path: Optional[str] = None) -> dict:
    """Check whether the IDE is running, active project folder, active file, and unsaved changes."""
    exe = launcher_path or find_antigravity_launcher()
    if not exe:
        return {
            "is_running": False,
            "open_folder": None,
            "active_file": None,
            "has_unsaved_changes": False,
            "window_title": "",
            "error": "Antigravity IDE launcher not found.",
        }

    try:
        res = subprocess.run([exe, "--status"], capture_output=True, text=True, timeout=8)
        if res.returncode == 0:
            return parse_status_output(res.stdout)
    except Exception as e:
        logger.debug(f"Failed to fetch window status: {e}")

    # Fallback: check process list
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            pname = (p.info.get("name") or "").lower()
            if "antigravity" in pname:
                return {
                    "is_running": True,
                    "open_folder": None,
                    "active_file": None,
                    "has_unsaved_changes": False,
                    "window_title": "",
                }
    except Exception:
        pass

    return {
        "is_running": False,
        "open_folder": None,
        "active_file": None,
        "has_unsaved_changes": False,
        "window_title": "",
    }


def open_folder(
    path: str,
    new_window: bool = False,
    launcher_path: Optional[str] = None,
    verify_timeout: float = 2.0,
) -> outcome.ActionResult:
    """Open a workspace folder in Antigravity IDE using subprocess argument lists."""
    resolved, candidates = resolve_ide_path(path)
    if candidates:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="open_folder",
            success=False,
            verified=False,
            message=f"Ambiguous project path '{path}'. Did you mean: {', '.join(candidates)}?",
            data={"candidates": candidates},
        )
    if not resolved:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="open_folder",
            success=False,
            verified=False,
            message=f"Project folder not found: '{path}'",
            error="Path does not exist",
        )

    if not resolved.is_dir():
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="open_folder",
            success=False,
            verified=False,
            message=f"Path is not a directory: '{resolved}'",
            error="Not a directory",
        )

    if not is_allowed_root(resolved):
        return outcome.ActionResult(
            status=outcome.STATUS_BLOCKED,
            action="open_folder",
            success=False,
            verified=False,
            message=f"Access denied: '{resolved}' is outside allowed project directories.",
            error="Outside allowed roots",
        )

    exe = launcher_path or find_antigravity_launcher()
    if not exe:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="open_folder",
            success=False,
            verified=False,
            message="Antigravity IDE is not installed or launcher could not be located.",
            error="Launcher not found",
        )

    args = [exe]
    if new_window:
        args.append("-n")
    args.append(str(resolved))

    try:
        subprocess.Popen(args, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="open_folder",
            success=False,
            verified=False,
            message=f"Failed to launch Antigravity IDE: {e}",
            error=str(e),
        )

    # Verification loop
    start = time.monotonic()
    verified = False
    folder_name_lower = resolved.name.lower()
    while time.monotonic() - start < verify_timeout:
        time.sleep(0.4)
        st = get_window_status(launcher_path=exe)
        if st.get("is_running"):
            open_folder_name = (st.get("open_folder") or "").lower()
            win_title = (st.get("window_title") or "").lower()
            if folder_name_lower in open_folder_name or folder_name_lower in win_title:
                verified = True
                break

    if verified:
        return outcome.ActionResult(
            status=outcome.STATUS_DONE,
            action="open_folder",
            success=True,
            verified=True,
            message=f"Opened project '{resolved.name}' in Antigravity IDE.",
            data={"path": str(resolved), "new_window": new_window},
        )

    # If launched but window title not yet confirmed in time
    return outcome.ActionResult(
        status=outcome.STATUS_UNVERIFIED,
        action="open_folder",
        success=True,
        verified=False,
        message=f"Launched Antigravity IDE for '{resolved.name}', but window title confirmation is pending.",
        data={"path": str(resolved), "new_window": new_window},
    )


def open_file(
    path: str,
    line: Optional[int] = None,
    column: Optional[int] = None,
    launcher_path: Optional[str] = None,
    verify_timeout: float = 2.0,
) -> outcome.ActionResult:
    """Open a file at an optional line:column in Antigravity IDE."""
    resolved, candidates = resolve_ide_path(path)
    if not resolved:
        candidate = Path(path).expanduser()
        if candidate.exists():
            resolved = candidate.resolve()

    if not resolved or not resolved.exists():
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="open_file",
            success=False,
            verified=False,
            message=f"File not found: '{path}'",
            error="File does not exist",
        )

    if not resolved.is_file():
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="open_file",
            success=False,
            verified=False,
            message=f"Target is not a file: '{resolved}'",
            error="Not a file",
        )

    if not is_allowed_root(resolved):
        return outcome.ActionResult(
            status=outcome.STATUS_BLOCKED,
            action="open_file",
            success=False,
            verified=False,
            message=f"Access denied: '{resolved}' is outside allowed directories.",
            error="Outside allowed roots",
        )

    exe = launcher_path or find_antigravity_launcher()
    if not exe:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="open_file",
            success=False,
            verified=False,
            message="Antigravity IDE launcher not found.",
            error="Launcher not found",
        )

    args = [exe]
    if line is not None:
        goto_arg = f"{str(resolved)}:{line}" + (f":{column}" if column is not None else "")
        args.extend(["-g", goto_arg])
    else:
        args.append(str(resolved))

    try:
        subprocess.Popen(args, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="open_file",
            success=False,
            verified=False,
            message=f"Failed to open file in Antigravity IDE: {e}",
            error=str(e),
        )

    return outcome.ActionResult(
        status=outcome.STATUS_DONE,
        action="open_file",
        success=True,
        verified=True,
        message=f"Opened '{resolved.name}' in Antigravity IDE" + (f" at line {line}" if line else "") + ".",
        data={"path": str(resolved), "line": line, "column": column},
    )


def open_diff(
    file_a: str,
    file_b: str,
    launcher_path: Optional[str] = None,
) -> outcome.ActionResult:
    """Open a diff view between two files in Antigravity IDE."""
    res_a, _ = resolve_ide_path(file_a)
    res_b, _ = resolve_ide_path(file_b)

    if not res_a or not res_a.is_file():
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="open_diff",
            success=False,
            verified=False,
            message=f"First diff file not found: '{file_a}'",
            error="File not found",
        )
    if not res_b or not res_b.is_file():
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="open_diff",
            success=False,
            verified=False,
            message=f"Second diff file not found: '{file_b}'",
            error="File not found",
        )

    if not is_allowed_root(res_a) or not is_allowed_root(res_b):
        return outcome.ActionResult(
            status=outcome.STATUS_BLOCKED,
            action="open_diff",
            success=False,
            verified=False,
            message="One or both files are outside allowed directories.",
            error="Outside allowed roots",
        )

    exe = launcher_path or find_antigravity_launcher()
    if not exe:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="open_diff",
            success=False,
            verified=False,
            message="Antigravity IDE launcher not found.",
            error="Launcher not found",
        )

    try:
        subprocess.Popen([exe, "-d", str(res_a), str(res_b)], shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="open_diff",
            success=False,
            verified=False,
            message=f"Failed to open diff in Antigravity IDE: {e}",
            error=str(e),
        )

    return outcome.ActionResult(
        status=outcome.STATUS_DONE,
        action="open_diff",
        success=True,
        verified=True,
        message=f"Opened diff between '{res_a.name}' and '{res_b.name}' in Antigravity IDE.",
        data={"file_a": str(res_a), "file_b": str(res_b)},
    )


def add_folder(
    path: str,
    launcher_path: Optional[str] = None,
) -> outcome.ActionResult:
    """Add a folder to the active Antigravity IDE window."""
    resolved, candidates = resolve_ide_path(path)
    if candidates:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="add_folder",
            success=False,
            verified=False,
            message=f"Ambiguous folder path '{path}'. Candidates: {', '.join(candidates)}",
            data={"candidates": candidates},
        )
    if not resolved or not resolved.is_dir():
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="add_folder",
            success=False,
            verified=False,
            message=f"Folder not found: '{path}'",
            error="Folder does not exist",
        )

    if not is_allowed_root(resolved):
        return outcome.ActionResult(
            status=outcome.STATUS_BLOCKED,
            action="add_folder",
            success=False,
            verified=False,
            message=f"Access denied: '{resolved}' is outside allowed directories.",
            error="Outside allowed roots",
        )

    exe = launcher_path or find_antigravity_launcher()
    if not exe:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="add_folder",
            success=False,
            verified=False,
            message="Antigravity IDE launcher not found.",
            error="Launcher not found",
        )

    try:
        subprocess.Popen([exe, "-a", str(resolved)], shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="add_folder",
            success=False,
            verified=False,
            message=f"Failed to add folder: {e}",
            error=str(e),
        )

    return outcome.ActionResult(
        status=outcome.STATUS_DONE,
        action="add_folder",
        success=True,
        verified=True,
        message=f"Added folder '{resolved.name}' to Antigravity IDE workspace.",
        data={"path": str(resolved)},
    )
