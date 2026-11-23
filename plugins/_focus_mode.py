"""
plugins/_focus_mode.py — Focus Mode Engine for MARK LVI / JARVIS.

Features:
- Distraction and adult-domain blocking via system hosts file with clear markers
- Optional secondary family-filter DNS switching (Cloudflare 1.1.1.3 / 1.0.0.3)
- Detection and warning for browser Secure DNS / DoH bypasses
- Background process monitor thread (poll every 2.5s, polite WM_CLOSE/SIGTERM, never kill)
- Always-allowed application protection (VS Code, Antigravity, GitHub Desktop, Notion, Obsidian, JARVIS)
- MVP browser tab closing for active distraction tabs with pre-warning and verified outcome
- Crash recovery via data/focus_mode_state.json (restoring hosts and DNS on startup or unexpected termination)
- Centralized permission integration and undo stack support
- Graceful handling of missing administrative privileges
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from core import appidentity, outcome
from core.lazyimport import LazyModule, installed
from core.permissions import (
    POLICY_ALLOW,
    POLICY_ASK,
    POLICY_DENY,
    resolve_permission_policy,
)
from core.undo import push_undo
from memory import config_manager

logger = logging.getLogger(__name__)

_PYAUTOGUI = installed("pyautogui")
pyautogui = LazyModule("pyautogui")

_SYSTEM = platform.system()  # "Windows" | "Darwin" | "Linux"

# Markers in hosts file
MARKER_BEGIN = "# === BEGIN JARVIS FOCUS MODE ==="
MARKER_END = "# === END JARVIS FOCUS MODE ==="

# Default hosts file paths
if _SYSTEM == "Windows":
    _DEFAULT_HOSTS_PATH = (
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
    )
else:
    _DEFAULT_HOSTS_PATH = Path("/etc/hosts")

_TEST_HOSTS_PATH: Optional[Path] = None

# State directory and files
_ROOT_DIR = Path(__file__).parent.parent
_DATA_DIR = _ROOT_DIR / "data"
_STATE_FILE = _DATA_DIR / "focus_mode_state.json"
_ADULT_BLOCKLIST_CACHE = _DATA_DIR / "hosts_porn.txt"

# Family DNS servers (Cloudflare 1.1.1.3 & 1.0.0.3)
CLOUDFLARE_FAMILY_DNS_PRIMARY = "1.1.1.3"
CLOUDFLARE_FAMILY_DNS_SECONDARY = "1.0.0.3"

# Lock for hosts and state operations
_mode_lock = threading.RLock()

# Active monitor instance
_active_monitor: Optional[FocusMonitor] = None


def set_hosts_path_for_testing(path: Optional[Path]) -> None:
    """Override hosts file path for unit testing. NEVER point to real hosts in tests."""
    global _TEST_HOSTS_PATH
    _TEST_HOSTS_PATH = path


def get_hosts_file_path() -> Path:
    """Return effective hosts file path (test override or OS path)."""
    if _TEST_HOSTS_PATH is not None:
        return _TEST_HOSTS_PATH
    return _DEFAULT_HOSTS_PATH


# ─────────────────────────────────────────────────────────────────────────────
#  State & Crash Recovery
# ─────────────────────────────────────────────────────────────────────────────


def _read_state_file() -> dict:
    with _mode_lock:
        if not _STATE_FILE.exists():
            return {}
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[FocusMode] Failed to read state file: {e}")
            return {}


def _write_state_file(state: dict) -> None:
    with _mode_lock:
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"[FocusMode] Failed to write state file: {e}")


def _clear_state_file() -> None:
    with _mode_lock:
        try:
            if _STATE_FILE.exists():
                _STATE_FILE.unlink()
        except Exception as e:
            logger.debug(f"[FocusMode] Failed to delete state file: {e}")


def is_focus_mode_active() -> bool:
    """Return True if Focus Mode is currently recorded as active."""
    with _mode_lock:
        if _active_monitor is not None and _active_monitor.is_alive():
            return True
        st = _read_state_file()
        return bool(st.get("active", False))


# ─────────────────────────────────────────────────────────────────────────────
#  Hosts File Manipulation
# ─────────────────────────────────────────────────────────────────────────────


def load_adult_domains() -> list[str]:
    """Load adult domains from local cache file."""
    domains: list[str] = []
    if _ADULT_BLOCKLIST_CACHE.exists():
        try:
            with open(_ADULT_BLOCKLIST_CACHE, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        domain = parts[1].strip().lower()
                    else:
                        domain = parts[0].strip().lower()
                    if domain and domain not in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
                        domains.append(domain)
        except Exception as e:
            logger.warning(f"[FocusMode] Failed reading adult blocklist: {e}")
    return domains


def apply_hosts_block(
    blocked_domains: list[str],
    block_adult: bool = True,
    hosts_path: Optional[Path] = None,
) -> tuple[bool, str]:
    """
    Append blocked domains inside JARVIS FOCUS MODE markers in the hosts file.
    Returns (success, message).
    """
    path = hosts_path or get_hosts_file_path()
    all_domains = set()
    for d in blocked_domains:
        d = str(d).strip().lower()
        if d:
            all_domains.add(d)

    if block_adult:
        adults = load_adult_domains()
        all_domains.update(adults)

    if not all_domains:
        return True, "No domains to block in hosts file."

    with _mode_lock:
        original_content = ""
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    original_content = f.read()
            except Exception as e:
                return False, f"Cannot read hosts file ({path}): {e}"

        # Strip existing markers if present
        clean_content = _remove_markers_from_text(original_content)

        # Build block lines
        block_lines = [
            "",
            MARKER_BEGIN,
            "# Auto-generated by JARVIS Focus Mode. Do not edit this section manually.",
        ]
        for dom in sorted(all_domains):
            block_lines.append(f"127.0.0.1 {dom}")
            block_lines.append(f"0.0.0.0 {dom}")
        block_lines.append(MARKER_END)
        block_lines.append("")

        new_content = clean_content.rstrip() + "\n" + "\n".join(block_lines)

        try:
            # Atomic write or direct write
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except PermissionError:
            msg = (
                f"Modifying system hosts file ({path}) requires Administrator/root privileges. "
                "Hosts-level blocking was skipped, but process and tab monitoring will stay active."
            )
            logger.warning(f"[FocusMode] {msg}")
            return False, msg
        except Exception as e:
            msg = f"Failed to write hosts file: {e}"
            logger.warning(f"[FocusMode] {msg}")
            return False, msg

        # Register undo handler
        push_undo("Focus Mode hosts block", lambda: remove_hosts_block(hosts_path=path)[1])
        return True, f"Successfully added {len(all_domains)} blocked domains to hosts file."


def remove_hosts_block(hosts_path: Optional[Path] = None) -> tuple[bool, str]:
    """
    Remove JARVIS FOCUS MODE markers and enclosed domains from the hosts file.
    Returns (success, message).
    """
    path = hosts_path or get_hosts_file_path()
    with _mode_lock:
        if not path.exists():
            return True, "Hosts file does not exist."

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            return False, f"Cannot read hosts file: {e}"

        if MARKER_BEGIN not in content:
            return True, "No Focus Mode entries found in hosts file."

        cleaned = _remove_markers_from_text(content)

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(cleaned)
        except PermissionError:
            msg = f"Permission denied removing Focus Mode block from hosts file ({path}). Administrator rights required."
            logger.warning(f"[FocusMode] {msg}")
            return False, msg
        except Exception as e:
            msg = f"Failed to restore hosts file: {e}"
            logger.warning(f"[FocusMode] {msg}")
            return False, msg

        return True, "Successfully removed Focus Mode block from hosts file."


def _remove_markers_from_text(text: str) -> str:
    """Helper to cut text between MARKER_BEGIN and MARKER_END."""
    lines = text.splitlines(keepends=True)
    out_lines = []
    inside_block = False
    for line in lines:
        if MARKER_BEGIN in line:
            inside_block = True
            continue
        if MARKER_END in line:
            inside_block = False
            continue
        if not inside_block:
            out_lines.append(line)
    return "".join(out_lines)


# ─────────────────────────────────────────────────────────────────────────────
#  DNS Resolver Switching (Optional Second Layer)
# ─────────────────────────────────────────────────────────────────────────────


def _get_active_windows_network_interfaces() -> list[str]:
    """Return names of active/connected network interfaces on Windows."""
    if _SYSTEM != "Windows":
        return []
    interfaces: list[str] = []
    try:
        proc = subprocess.run(
            ["netsh", "interface", "show", "interface"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if "Connected" in line:
                    # Columns: Admin State | State | Type | Interface Name
                    parts = line.split(maxsplit=3)
                    if len(parts) >= 4:
                        interfaces.append(parts[3].strip())
    except Exception as e:
        logger.debug(f"[FocusMode] Failed to get network interfaces: {e}")
    return interfaces or ["Wi-Fi", "Ethernet"]


def get_current_windows_dns(interface: str) -> dict:
    """Query current IPv4 DNS servers for an interface on Windows."""
    if _SYSTEM != "Windows":
        return {"type": "unsupported", "servers": []}
    try:
        proc = subprocess.run(
            ["netsh", "interface", "ipv4", "show", "dns", f"name={interface}"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        output = proc.stdout
        servers = []
        is_dhcp = "DHCP" in output or "dynamically" in output.lower()
        import re

        ip_regex = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        for line in output.splitlines():
            if "DNS servers configured through DHCP" in line or "Statically Configured DNS Servers" in line or "DNS servers" in line:
                ips = ip_regex.findall(line)
                servers.extend(ips)
            elif "Register with which suffix" not in line:
                ips = ip_regex.findall(line)
                servers.extend(ips)

        # De-duplicate while preserving order
        unique_servers = []
        for s in servers:
            if s not in unique_servers and s not in ("127.0.0.1", "0.0.0.0"):
                unique_servers.append(s)

        return {
            "type": "dhcp" if is_dhcp else "static",
            "servers": unique_servers,
        }
    except Exception as e:
        logger.debug(f"[FocusMode] Failed to query DNS for {interface}: {e}")
        return {"type": "unknown", "servers": []}


def apply_family_dns() -> tuple[bool, str, dict]:
    """
    Set primary and secondary DNS to Cloudflare Family Filter on active adapters.
    Returns (success, message, original_dns_snapshot).
    """
    if _SYSTEM != "Windows":
        return False, "DNS switching is currently only supported on Windows.", {}

    interfaces = _get_active_windows_network_interfaces()
    if not interfaces:
        return False, "No active network adapters found to configure DNS.", {}

    backup: dict = {}
    success_count = 0
    fail_msgs = []

    for iface in interfaces:
        cur_dns = get_current_windows_dns(iface)
        backup[iface] = cur_dns

        # Set primary DNS
        res1 = subprocess.run(
            [
                "netsh",
                "interface",
                "ipv4",
                "set",
                "dns",
                f"name={iface}",
                "static",
                CLOUDFLARE_FAMILY_DNS_PRIMARY,
                "primary",
            ],
            capture_output=True,
            text=True,
            timeout=8.0,
            check=False,
        )
        if res1.returncode != 0:
            fail_msgs.append(f"{iface}: {res1.stderr.strip() or res1.stdout.strip()}")
            continue

        # Add secondary DNS
        subprocess.run(
            [
                "netsh",
                "interface",
                "ipv4",
                "add",
                "dns",
                f"name={iface}",
                CLOUDFLARE_FAMILY_DNS_SECONDARY,
                "index=2",
            ],
            capture_output=True,
            text=True,
            timeout=8.0,
            check=False,
        )
        success_count += 1

    if success_count > 0:
        push_undo("Focus Mode family DNS filter", lambda: restore_family_dns(backup)[1])
        return True, f"Applied Family DNS filter ({CLOUDFLARE_FAMILY_DNS_PRIMARY}) to {success_count} adapter(s).", backup
    else:
        err = "; ".join(fail_msgs) or "Administrator permissions required."
        return False, f"Failed to switch DNS: {err}", backup


def restore_family_dns(backup_dns: Optional[dict] = None) -> tuple[bool, str]:
    """Restore original DNS configuration from backup or reset to DHCP."""
    if _SYSTEM != "Windows":
        return False, "DNS switching only supported on Windows."

    interfaces = _get_active_windows_network_interfaces()
    restored_count = 0
    fail_msgs = []

    backup = backup_dns or {}
    for iface in interfaces:
        saved = backup.get(iface, {})
        dns_type = saved.get("type", "dhcp")
        servers = saved.get("servers", [])

        if dns_type == "dhcp" or not servers:
            # Revert to DHCP
            res = subprocess.run(
                ["netsh", "interface", "ipv4", "set", "dns", f"name={iface}", "dhcp"],
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
            )
            if res.returncode == 0:
                restored_count += 1
            else:
                fail_msgs.append(f"{iface} (DHCP): {res.stderr.strip() or res.stdout.strip()}")
        else:
            # Revert to original static IPs
            primary = servers[0]
            res1 = subprocess.run(
                [
                    "netsh",
                    "interface",
                    "ipv4",
                    "set",
                    "dns",
                    f"name={iface}",
                    "static",
                    primary,
                    "primary",
                ],
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
            )
            if res1.returncode == 0:
                for idx, sec in enumerate(servers[1:], start=2):
                    subprocess.run(
                        [
                            "netsh",
                            "interface",
                            "ipv4",
                            "add",
                            "dns",
                            f"name={iface}",
                            sec,
                            f"index={idx}",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=8.0,
                        check=False,
                    )
                restored_count += 1
            else:
                fail_msgs.append(f"{iface} (Static): {res1.stderr.strip() or res1.stdout.strip()}")

    if restored_count > 0:
        return True, f"Restored DNS configuration on {restored_count} adapter(s)."
    else:
        return False, f"Failed to restore DNS: {'; '.join(fail_msgs)}"


# ─────────────────────────────────────────────────────────────────────────────
#  Secure DNS (DoH) Warning Check
# ─────────────────────────────────────────────────────────────────────────────


def check_doh_warning() -> Optional[str]:
    """
    Check whether browser Secure DNS (DNS-over-HTTPS) could bypass OS hosts / DNS blocks.
    Returns a warning message string if advice should be surfaced to user.
    """
    # Secure DNS in Chrome, Edge, and Firefox resolves directly through DoH HTTPS endpoints,
    # ignoring local /etc/hosts and local adapter DNS servers.
    return (
        "Note: Secure DNS (DNS-over-HTTPS) enabled in browsers (Chrome, Edge, Firefox, Brave) "
        "bypasses system hosts-file and DNS filtering. If blocked sites still load, temporarily disable "
        "'Use secure DNS' in your browser settings during Focus Mode."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Browser Tab Closer (MVP)
# ─────────────────────────────────────────────────────────────────────────────

_BROWSER_PROCESSES = {
    "chrome.exe",
    "chrome",
    "msedge.exe",
    "msedge",
    "firefox.exe",
    "firefox",
    "brave.exe",
    "brave",
    "opera.exe",
    "opera",
    "vivaldi.exe",
    "vivaldi",
}

_BROWSER_KEYWORDS = {
    "google chrome",
    "microsoft edge",
    "mozilla firefox",
    "brave",
    "opera",
    "vivaldi",
}


def _get_foreground_window_info() -> tuple[str, str, int]:
    """
    Return (window_title, process_name, hwnd).
    Returns ('', '', 0) if inaccessible or unsupported.
    """
    if _SYSTEM == "Windows":
        try:
            import win32gui
            import win32process

            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return "", "", 0
            title = win32gui.GetWindowText(hwnd) or ""
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc_name = ""
            if pid and appidentity._PSUTIL:
                try:
                    p = appidentity.psutil.Process(pid)
                    proc_name = p.name().lower()
                except Exception:
                    pass
            return title, proc_name, hwnd
        except Exception as e:
            logger.debug(f"[FocusMode] win32 GetForegroundWindow failed: {e}")
            return "", "", 0
    elif _SYSTEM == "Darwin":
        # macOS title extraction via AppleScript
        try:
            cmd = """osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true'"""
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=1.0)
            app = res.stdout.strip()
            return app, app.lower(), 1
        except Exception:
            return "", "", 0
    else:
        # Linux (X11 / Wayland check)
        if os.environ.get("WAYLAND_DISPLAY"):
            # Wayland intentionally restricts foreground window inspection for security
            return "", "", 0
        try:
            res = subprocess.run(
                ["xdotool", "getwindowfocus", "getwindowname"],
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            title = res.stdout.strip()
            return title, "", 1
        except Exception:
            return "", "", 0


def close_active_distraction_tab_mvp(
    blocked_keywords: list[str],
    warn_first: bool = True,
    warned_set: Optional[set] = None,
    player=None,
) -> tuple[bool, str]:
    """
    MVP browser tab closing:
    If foreground window is a browser and title matches blocked keywords,
    send Ctrl+W, verify after 0.5s, and return (verified, result_msg).
    """
    if _SYSTEM != "Windows" and os.environ.get("WAYLAND_DISPLAY"):
        return False, "Browser tab monitoring is not supported on Wayland without accessibility extensions."

    title, proc_name, hwnd = _get_foreground_window_info()
    if not title:
        return False, "No active foreground window title accessible."

    title_lower = title.lower()
    proc_lower = proc_name.lower()

    # Is the foreground window a browser?
    is_browser = proc_lower in _BROWSER_PROCESSES or any(kw in title_lower for kw in _BROWSER_KEYWORDS)
    if not is_browser:
        return False, f"Foreground window '{title[:30]}' is not a browser."

    # Does title contain any blocked domain or keyword?
    matched_keyword = None
    for kw in blocked_keywords:
        kw_clean = str(kw).strip().lower()
        if not kw_clean:
            continue
        # Check domain parts e.g. "instagram", "xvideos", etc.
        root_part = kw_clean.replace("www.", "").split(".")[0]
        if root_part and (root_part in title_lower or kw_clean in title_lower):
            matched_keyword = kw_clean
            break

    if not matched_keyword:
        return False, "No blocked keyword in active tab."

    # Check permission
    policy = resolve_permission_policy("process:close_app")
    if policy == POLICY_DENY:
        return False, f"Permission policy for process:close_app is DENY. Tab close refused."

    # Pre-close warning if first time
    if warn_first and warned_set is not None:
        if hwnd not in warned_set:
            warned_set.add(hwnd)
            msg = f"Focus Mode: Active browser tab '{title[:40]}' matches blocked keyword '{matched_keyword}'. Closing tab..."
            if player and hasattr(player, "write_log"):
                player.write_log(f"JARVIS: {msg}")
            logger.info(f"[FocusMode] {msg}")
            time.sleep(1.0)  # Brief grace period

    if not _PYAUTOGUI:
        return False, "PyAutoGUI is not available to send tab-close shortcut."

    # Send Ctrl+W (or Cmd+W on macOS)
    try:
        if _SYSTEM == "Darwin":
            pyautogui.hotkey("command", "w")
        else:
            pyautogui.hotkey("ctrl", "w")
    except Exception as e:
        return False, f"Failed to send close hotkey: {e}"

    # Verification: check if foreground window title changed after 0.5s
    time.sleep(0.5)
    new_title, _, _ = _get_foreground_window_info()
    new_title_lower = new_title.lower()

    # If title changed or no longer contains the keyword -> verified
    if matched_keyword not in new_title_lower and root_part not in new_title_lower:
        msg = f"Closed active tab containing '{matched_keyword}' (verified)."
        if player and hasattr(player, "write_log"):
            player.write_log(f"JARVIS: {msg}")
        return True, msg
    else:
        # Title didn't change (e.g. dialog blocked it or hotkey missed)
        msg = f"Sent close hotkey for tab '{matched_keyword}', but tab remains open (unverified)."
        logger.warning(f"[FocusMode] {msg}")
        return False, msg


# ─────────────────────────────────────────────────────────────────────────────
#  Background Monitor Thread
# ─────────────────────────────────────────────────────────────────────────────


class FocusMonitor(threading.Thread):
    """
    Background thread that runs while Focus Mode is active.
    Polls every 2.5 seconds to:
    - Close blocked desktop applications politely (WM_CLOSE / SIGTERM, never kill)
    - Protect always-allowed applications and assistant's own processes
    - Check active browser tab against blocked distraction keywords
    """

    def __init__(
        self,
        blocked_apps: list[str],
        always_allowed_apps: list[str],
        blocked_domains: list[str],
        poll_interval: float = 2.5,
        close_tabs: bool = True,
        player=None,
    ):
        super().__init__(name="jarvis-focus-monitor", daemon=True)
        self.blocked_apps = [a.lower().strip() for a in blocked_apps if a.strip()]
        self.always_allowed_apps = [a.lower().strip() for a in always_allowed_apps if a.strip()]
        self.blocked_domains = [d.lower().strip() for d in blocked_domains if d.strip()]
        self.poll_interval = max(1.0, float(poll_interval))
        self.close_tabs = close_tabs
        self.player = player

        self._stop_event = threading.Event()
        self._warned_tab_hwnds: set = set()
        self._closed_count = 0

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("[FocusMode] Monitor thread started.")
        while not self._stop_event.is_set():
            try:
                self._check_processes()
                if self.close_tabs:
                    self._check_browser_tabs()
            except Exception as e:
                logger.debug(f"[FocusMode] Monitor iteration error: {e}")

            self._stop_event.wait(self.poll_interval)
        logger.info("[FocusMode] Monitor thread stopped.")

    def _check_processes(self) -> None:
        """Scan process table and politely request closing of blocked applications."""
        if not appidentity._PSUTIL:
            return

        own_pids = appidentity.own_pids()

        for proc_target in self.blocked_apps:
            # Look up running instances
            matches = appidentity.running(proc_target)
            for match in matches:
                if match.is_self or match.pid in own_pids:
                    continue

                # Check if process matches any always-allowed app
                label_clean = match.label.lower()
                name_clean = (match.name or "").lower()
                is_allowed = any(
                    al in label_clean or al in name_clean
                    for al in self.always_allowed_apps
                )
                if is_allowed:
                    continue

                # Check permission policy for process:close_app
                policy = resolve_permission_policy("process:close_app")
                if policy == POLICY_DENY:
                    logger.debug(f"[FocusMode] process:close_app is DENY; skipping {match.label}")
                    continue

                # Politely close application (WM_CLOSE or SIGTERM)
                gone, detail = appidentity.request_close(match, timeout=2.0)
                if gone:
                    self._closed_count += 1
                    msg = f"Closed blocked distraction application: {match.label} ({match.pid})."
                    logger.info(f"[FocusMode] {msg}")
                    if self.player and hasattr(self.player, "write_log"):
                        self.player.write_log(f"JARVIS: Focus Mode politely closed {match.label}.")

    def _check_browser_tabs(self) -> None:
        """Inspect foreground browser window and close active distraction tab."""
        keywords = list(self.blocked_domains)
        keywords.extend(["instagram", "reels", "shorts"])
        close_active_distraction_tab_mvp(
            blocked_keywords=keywords,
            warn_first=True,
            warned_set=self._warned_tab_hwnds,
            player=self.player,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Public Focus Mode Control API
# ─────────────────────────────────────────────────────────────────────────────


def activate_focus_mode(player=None) -> tuple[bool, str]:
    """
    Turn on Focus Mode:
    - Reads configuration from config_manager
    - Applies hosts-file blocking for blocked domains + adult sites
    - Optionally applies Family DNS filter
    - Starts background polite process monitor thread
    - Persists state in data/focus_mode_state.json
    """
    global _active_monitor

    with _mode_lock:
        cfg = config_manager.get_mode_settings("focus")
        blocked_apps = cfg.get("blocked_apps", ["Instagram"])
        always_allowed = cfg.get(
            "always_allowed_apps",
            ["Code", "Antigravity IDE", "GitHubDesktop", "Notion", "Obsidian"],
        )
        blocked_domains = cfg.get(
            "blocked_domains",
            ["instagram.com", "www.instagram.com", "i.instagram.com", "cdninstagram.com"],
        )
        block_adult = cfg.get("block_adult_content", True)
        enable_dns = cfg.get("enable_family_dns", False)
        close_tabs = cfg.get("close_browser_tabs", True)
        poll_interval = cfg.get("poll_interval_seconds", 2.5)

        # 1. Hosts file modification
        hosts_success, hosts_msg = False, ""
        perm_hosts = resolve_permission_policy("network:hosts_edit")
        if perm_hosts == POLICY_DENY:
            hosts_msg = "Hosts-file edit denied by permission settings."
        else:
            hosts_success, hosts_msg = apply_hosts_block(
                blocked_domains, block_adult=block_adult
            )

        # 2. Family DNS (optional)
        dns_success, dns_msg, orig_dns = False, "", {}
        if enable_dns:
            perm_dns = resolve_permission_policy("network:dns_change")
            if perm_dns != POLICY_DENY:
                dns_success, dns_msg, orig_dns = apply_family_dns()
            else:
                dns_msg = "DNS change denied by permission settings."

        # 3. Start background monitor
        if _active_monitor is not None and _active_monitor.is_alive():
            _active_monitor.stop()

        _active_monitor = FocusMonitor(
            blocked_apps=blocked_apps,
            always_allowed_apps=always_allowed,
            blocked_domains=blocked_domains,
            poll_interval=poll_interval,
            close_tabs=close_tabs,
            player=player,
        )
        _active_monitor.start()

        # 4. Save state for crash recovery
        state = {
            "active": True,
            "start_time": time.time(),
            "hosts_modified": hosts_success,
            "original_dns": orig_dns,
            "blocked_apps": blocked_apps,
            "blocked_domains": blocked_domains,
        }
        _write_state_file(state)
        config_manager.set_active_mode("focus")

        # 5. Check DoH warning
        doh_note = check_doh_warning()

        parts = ["Focus Mode is now ACTIVE."]
        if hosts_success:
            parts.append("Distraction and adult domains blocked in hosts.")
        else:
            parts.append(f"Hosts block note: {hosts_msg}")

        if enable_dns:
            if dns_success:
                parts.append("Family DNS filter applied.")
            else:
                parts.append(f"DNS note: {dns_msg}")

        parts.append(f"App monitor active for: {', '.join(blocked_apps)}.")
        if doh_note:
            parts.append(doh_note)

        summary = " ".join(parts)
        if player and hasattr(player, "write_log"):
            player.write_log(f"JARVIS: {summary}")

        return True, summary


def deactivate_focus_mode(player=None) -> tuple[bool, str]:
    """
    Turn off Focus Mode:
    - Stops background monitor thread
    - Removes hosts file block markers
    - Restores DNS configuration if changed
    - Cleans up state file and updates config_manager
    """
    global _active_monitor

    with _mode_lock:
        state = _read_state_file()
        orig_dns = state.get("original_dns", {})

        # 1. Stop monitor
        if _active_monitor is not None:
            _active_monitor.stop()
            _active_monitor = None

        # 2. Restore hosts file
        hosts_ok, hosts_msg = remove_hosts_block()

        # 3. Restore DNS
        dns_ok, dns_msg = True, ""
        if orig_dns:
            dns_ok, dns_msg = restore_family_dns(orig_dns)

        # 4. Clear state & config
        _clear_state_file()
        if config_manager.get_active_mode() == "focus":
            config_manager.set_active_mode(None)

        msg = "Focus Mode has been DEACTIVATED. Distraction blocks and monitors removed."
        if not hosts_ok:
            msg += f" Note: {hosts_msg}"
        if orig_dns and not dns_ok:
            msg += f" Note: {dns_msg}"

        if player and hasattr(player, "write_log"):
            player.write_log(f"JARVIS: {msg}")

        return True, msg


# ─────────────────────────────────────────────────────────────────────────────
#  Startup Crash Recovery & Process Exit Cleanup
# ─────────────────────────────────────────────────────────────────────────────


def restore_crash_state(hosts_path: Optional[Path] = None) -> tuple[bool, str]:
    """
    Crash recovery: checks if a previous session terminated while Focus Mode was active,
    and restores hosts file and DNS settings. Called during startup.
    """
    with _mode_lock:
        state = _read_state_file()
        if not state.get("active") and not state.get("hosts_modified"):
            # Check hosts file directly for orphan markers
            hpath = hosts_path or get_hosts_file_path()
            if hpath.exists():
                try:
                    with open(hpath, "r", encoding="utf-8", errors="ignore") as f:
                        if MARKER_BEGIN in f.read():
                            remove_hosts_block(hosts_path=hpath)
                            return True, "Recovered hosts file from previous session markers."
                except Exception:
                    pass
            return False, "No stale Focus Mode state to recover."

        logger.info("[FocusMode] Recovering from previous crash or unclean shutdown...")
        # 1. Restore hosts file
        remove_hosts_block(hosts_path=hosts_path)

        # 2. Restore DNS if recorded
        orig_dns = state.get("original_dns")
        if orig_dns:
            restore_family_dns(orig_dns)

        # 3. Reset state
        _clear_state_file()
        if config_manager.get_active_mode() == "focus":
            config_manager.set_active_mode(None)

        return True, "Successfully recovered hosts and network configuration from previous Focus Mode session."


def _emergency_restore_atexit() -> None:
    """Exit handler to ensure hosts file is not left modified on exit."""
    try:
        if is_focus_mode_active():
            deactivate_focus_mode()
    except Exception:
        pass


atexit.register(_emergency_restore_atexit)
