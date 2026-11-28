"""
actions/sandboxed_shell.py — Safe Sandboxed Shell Command Execution for JARVIS.

Executes local shell commands under strict security constraints:
- Enforces execution timeout (default 30s, max 60s).
- Caps stdout/stderr output to 16KB to prevent context blowout.
- Confirmation gating (SENSITIVE) for destructive shell patterns (rm -rf, format, del /f /s /q).
- Untrusted output wrapping on raw process output.
"""

from __future__ import annotations

import logging
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from core import outcome

logger = logging.getLogger("sandboxed_shell")

MAX_OUTPUT_BYTES = 16 * 1024  # 16 KB
DEFAULT_TIMEOUT_SEC = 30.0
MAX_TIMEOUT_SEC = 60.0

DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+-[rf]{1,2}\b", re.IGNORECASE),
    re.compile(r"\bdel\s+/[fsq]{1,3}\b", re.IGNORECASE),
    re.compile(r"\brmdir\s+/[sq]{1,2}\b", re.IGNORECASE),
    re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),
    re.compile(r"\bmkfs\.", re.IGNORECASE),
    re.compile(r"\bdd\s+if=", re.IGNORECASE),
    re.compile(r"\bshutdown(\.exe)?\s+/[srf]", re.IGNORECASE),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.IGNORECASE),  # fork bomb
]


def _is_destructive(command: str) -> bool:
    for pattern in DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return True
    return False


def run_shell_command(
    command: str,
    cwd: Optional[str] = None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    confirmed: bool = False,
) -> str:
    if not command or not command.strip():
        return outcome.failed("sandboxed_shell", "Missing command.")

    cmd_clean = command.strip()
    timeout = min(max(1.0, float(timeout_sec)), MAX_TIMEOUT_SEC)

    # Check destructive patterns
    if _is_destructive(cmd_clean):
        if not confirmed:
            return outcome.ask(
                "sandboxed_shell",
                f"Command contains destructive patterns: '{cmd_clean}'. Are you sure you want to run it?",
                command=cmd_clean,
            )

    target_cwd = Path(cwd).resolve() if cwd else Path.cwd()
    if not target_cwd.is_dir():
        return outcome.failed("sandboxed_shell", f"Working directory '{target_cwd}' does not exist.")

    is_win = platform.system() == "Windows"
    shell_bin = "powershell.exe" if is_win else "/bin/bash"

    start_t = time.monotonic()
    try:
        proc = subprocess.run(
            [shell_bin, "-Command" if is_win else "-c", cmd_clean],
            cwd=str(target_cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        duration_ms = round((time.monotonic() - start_t) * 1000, 1)

        stdout = proc.stdout
        stderr = proc.stderr
        truncated = False

        if len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
            stdout = stdout[:MAX_OUTPUT_BYTES] + "\n...[OUTPUT TRUNCATED]"
            truncated = True

        if len(stderr.encode("utf-8")) > MAX_OUTPUT_BYTES:
            stderr = stderr[:MAX_OUTPUT_BYTES] + "\n...[STDERR TRUNCATED]"
            truncated = True

        if proc.returncode == 0:
            return outcome.done(
                "sandboxed_shell",
                command=cmd_clean,
                exit_code=proc.returncode,
                duration_ms=duration_ms,
                stdout=outcome.untrusted("shell", stdout.strip()) if stdout.strip() else "",
                stderr=outcome.untrusted("shell", stderr.strip()) if stderr.strip() else "",
                truncated=truncated,
            )
        return outcome.failed(
            "sandboxed_shell",
            f"Command exited with code {proc.returncode}: {stderr.strip() or stdout.strip() or 'non-zero exit'}",
            command=cmd_clean,
            exit_code=proc.returncode,
            duration_ms=duration_ms,
        )
    except subprocess.TimeoutExpired:
        duration_ms = round((time.monotonic() - start_t) * 1000, 1)
        return outcome.failed(
            "sandboxed_shell",
            f"Command timed out after {timeout} seconds.",
            command=cmd_clean,
            timeout=timeout,
            duration_ms=duration_ms,
        )
    except Exception as e:
        logger.error("sandboxed_shell execution error: %s", e)
        return outcome.failed("sandboxed_shell", f"Execution error: {e}", command=cmd_clean)


def sandboxed_shell(parameters: dict, **kwargs) -> str:
    params = parameters or {}
    cmd = str(params.get("command", ""))
    cwd = params.get("cwd")
    timeout = float(params.get("timeout_sec", DEFAULT_TIMEOUT_SEC))
    confirmed = bool(params.get("confirmed", False))
    return run_shell_command(cmd, cwd=cwd, timeout_sec=timeout, confirmed=confirmed)


TOOL = {
    "name": "sandboxed_shell",
    "permission": "sensitive",
    "description": (
        "Execute shell commands with timeout enforcement (max 60s), 16KB output capping, "
        "and strict confirmation gating on destructive commands (rm -rf, format, del /f)."
    ),
    "brief": "Sandboxed shell execution with timeout & output caps.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "command": {
                "type": "STRING",
                "description": "Shell command line to execute.",
            },
            "cwd": {
                "type": "STRING",
                "description": "Working directory for command execution (default: current workspace).",
            },
            "timeout_sec": {
                "type": "NUMBER",
                "description": "Maximum execution time in seconds (default: 30, max: 60).",
            },
            "confirmed": {
                "type": "BOOLEAN",
                "description": "Confirmation flag required for destructive commands.",
            },
        },
        "required": ["command"],
    },
    "handler": sandboxed_shell,
}
