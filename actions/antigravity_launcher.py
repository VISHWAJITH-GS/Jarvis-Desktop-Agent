"""
actions/antigravity_launcher.py — Launcher detection and argument list builder for Antigravity IDE and CLI.

Provides robust, cross-platform resolution of:
- Antigravity IDE launcher (antigravity-ide.cmd / antigravity-ide / Antigravity IDE.exe)
- Antigravity CLI (agy / agy.exe)
- Version querying and extension listing via subprocess argument lists (no shell)
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("antigravity_launcher")

_SYSTEM = platform.system()  # "Windows" | "Darwin" | "Linux"


def find_antigravity_launcher() -> Optional[str]:
    """Locate the Antigravity IDE executable or CLI runner script.

    Searches:
    1. PATH via shutil.which ('antigravity-ide', 'agy-ide', 'antigravity')
    2. Standard OS install paths (Windows LocalAppData, Program Files; macOS /Applications; Linux /usr)
    """
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
