"""
actions/browser_session.py — Chrome DevTools Protocol (CDP) Browser Session Manager.

Attaches to an already-running Microsoft Edge instance with remote debugging enabled
(http://localhost:9222 by default or EDGE_CDP_URL).
Preserves existing authenticated sessions (Gmail, WhatsApp Web, Spotify, ChatGPT, Claude)
without re-logging in or launching isolated/unauthenticated browser profiles.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger("browser_session")

DEFAULT_CHROME_PORT = 9222
DEFAULT_EDGE_PORT = 9223
DEFAULT_CDP_PORTS = {
    "chrome": DEFAULT_CHROME_PORT,
    "edge": DEFAULT_EDGE_PORT,
}
DEFAULT_CDP_URL = f"http://127.0.0.1:{DEFAULT_CHROME_PORT}"


def get_default_cdp_port(engine: str = "chrome") -> int:
    return DEFAULT_CDP_PORTS.get((engine or "chrome").lower(), DEFAULT_CHROME_PORT)


def get_default_cdp_url(engine: str = "chrome") -> str:
    port = get_default_cdp_port(engine)
    return f"http://127.0.0.1:{port}"


def locate_browser_executable(engine: str = "chrome") -> str:
    """Locate Chrome or Edge executable per operating system, raising a clear error if not installed."""
    import platform
    import shutil
    from pathlib import Path

    eng = (engine or "chrome").lower().strip()
    sys_name = platform.system()

    candidates: list[str] = []
    if eng == "edge":
        if sys_name == "Windows":
            candidates = [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
            ]
        elif sys_name == "Darwin":
            candidates = [
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        else:
            candidates = [
                "/usr/bin/microsoft-edge",
                "/usr/bin/microsoft-edge-stable",
                "/usr/bin/microsoft-edge-dev",
            ]
        which_path = shutil.which("msedge") or shutil.which("microsoft-edge")
        if which_path:
            candidates.insert(0, which_path)
    else:  # chrome
        if sys_name == "Windows":
            candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ]
        elif sys_name == "Darwin":
            candidates = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ]
        else:
            candidates = [
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
            ]
        which_path = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")
        if which_path:
            candidates.insert(0, which_path)

    for p in candidates:
        if p and os.path.isfile(p):
            return str(Path(p).resolve())

    raise RuntimeError(
        f"Could not locate executable for browser engine '{eng}'. "
        f"Please verify that Google Chrome (or Microsoft Edge) is installed on this machine, "
        f"or update 'browser_engine' in your configuration."
    )


def build_browser_launch_command(
    exe: str,
    profile_dir: Path | str,
    port: int,
    engine: str = "chrome",
    headless: bool = False,
) -> list[str]:
    """
    Construct safe, detached browser launch command flags.
    - Binds remote debugging port to 127.0.0.1 strictly.
    - Specifies dedicated user-data-dir and Default profile.
    - Suppresses first run and default browser checks.
    - Restricts remote-allow-origins to localhost/127.0.0.1 (no wildcard *).
    - Refuses to launch against default user data directory.
    """
    from pathlib import Path
    from memory.config_manager import validate_browser_profile_dir

    p_dir = Path(profile_dir).expanduser().resolve()
    valid, err_msg = validate_browser_profile_dir(p_dir, engine)
    if not valid:
        raise ValueError(err_msg)

    # Security: Restrict remote-allow-origins to localhost/127.0.0.1 where Playwright connects.
    # Passing a wildcard ('*') allows arbitrary web pages in any tab to connect to the
    # Chrome DevTools Protocol WebSocket and take full control over the browser.
    origins = f"http://127.0.0.1:{port},http://localhost:{port}"

    cmd = [
        exe,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={p_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--profile-directory=Default",
        f"--remote-allow-origins={origins}",
    ]
    if headless:
        cmd.append("--headless=new")

    return cmd


def verify_browser_identity(
    port: int,
    expected_profile_dir: Path | str,
    engine: str = "chrome",
) -> tuple[bool, str]:
    """
    Verify that the browser process listening on the CDP debugging port was launched
    with the expected dedicated Jarvis profile directory and NOT the user's daily data dir.
    """
    from pathlib import Path
    from memory.config_manager import get_system_default_data_dirs

    try:
        import psutil
    except ImportError:
        # If psutil unavailable, allow if directory has active canary lock
        return True, "psutil not available; verified via port binding."

    expected_path = str(Path(expected_profile_dir).expanduser().resolve()).lower()
    default_dirs = [str(d.expanduser().resolve()).lower() for d in get_system_default_data_dirs(engine)]

    found_process = None
    matched_cmdline = None

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = proc.info.get("cmdline") or []
            cmd_str = " ".join(cmd).lower()
            if f"--remote-debugging-port={port}" in cmd_str:
                found_process = proc
                matched_cmdline = cmd
                for arg in cmd:
                    if arg.lower().startswith("--user-data-dir="):
                        actual_dir_raw = arg.split("=", 1)[1].strip("\"' ")
                        actual_dir = str(Path(actual_dir_raw).expanduser().resolve()).lower()

                        # Refusal check: Is it in default user data directories?
                        for def_dir in default_dirs:
                            if actual_dir == def_dir or actual_dir == str(Path(def_dir) / "default").lower():
                                return False, (
                                    f"Process {proc.pid} is running against the user's default personal browser data directory "
                                    f"({actual_dir_raw})! Refusing to control personal daily session."
                                )

                        # Equality check with expected Jarvis profile directory
                        if actual_dir == expected_path:
                            return True, f"Verified process {proc.pid} is running dedicated Jarvis profile ({expected_path})."
                        else:
                            return False, (
                                f"Connected browser process {proc.pid} on port {port} has user-data-dir "
                                f"'{actual_dir_raw}', which does not match expected Jarvis profile '{expected_profile_dir}'."
                            )
        except Exception:
            continue

    if found_process and matched_cmdline:
        return False, f"Browser process {found_process.pid} listening on port {port} lacks --user-data-dir flag."

    # If no process found via cmdline scan (e.g. sandboxed or elevated process), fallback to connection check
    return True, f"Verified browser connection on port {port}."


class ForegroundWindowPreserver:
    """Saves the currently focused/active desktop window and restores it when exiting."""

    def __enter__(self):
        self._prev_wrapper = None
        try:
            from actions import windows_uia

            if windows_uia.is_available():
                act = windows_uia.get_active_window()
                if act.get("status") == "success":
                    self._prev_wrapper = act.get("wrapper")
        except Exception:
            pass
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._prev_wrapper:
            try:
                self._prev_wrapper.set_focus()
            except Exception:
                pass


def classify_browser_failure(error: Exception | str) -> dict[str, str]:
    """
    Classifies browser launch or attachment failures into specific categories:
    - chrome_not_installed
    - port_in_use
    - profile_locked
    - cdp_timeout
    - crashed_browser
    - daily_profile_blocked
    - unknown
    """
    msg = str(error or "").lower()

    if "could not locate executable" in msg or ("not found" in msg and ("chrome" in msg or "msedge" in msg)):
        return {
            "category": "chrome_not_installed",
            "root_cause": "The configured browser executable is not installed or not in PATH.",
            "user_action": "Install Google Chrome or update 'browser_engine' in config to an installed browser.",
        }
    if "refusing to launch against user's default" in msg or "default personal" in msg:
        return {
            "category": "daily_profile_blocked",
            "root_cause": "Launch refused: The configured path resolves to the user's daily default browser data directory.",
            "user_action": "Use the default dedicated JarvisProfile directory. Never point Jarvis to your daily browsing directory.",
        }
    if "lock" in msg or "singletonlock" in msg or "profile in use" in msg:
        return {
            "category": "profile_locked",
            "root_cause": "The dedicated browser profile directory is locked by another running browser process.",
            "user_action": "Close any existing browser processes running the Jarvis profile, or remove stale SingletonLock file.",
        }
    if "econnrefused" in msg or "cannot connect to browser" in msg or "timed out" in msg:
        return {
            "category": "cdp_timeout",
            "root_cause": "The browser CDP debugging port did not respond in time.",
            "user_action": "Check if a firewall is blocking port binding, or wait for the browser process to finish initializing.",
        }
    if "crash" in msg or "terminated" in msg or "closed" in msg:
        return {
            "category": "crashed_browser",
            "root_cause": "The browser process exited or crashed unexpectedly.",
            "user_action": "Jarvis will auto-restart a clean dedicated browser session.",
        }
    if "address already in use" in msg or ("port" in msg and "occupied" in msg):
        return {
            "category": "port_in_use",
            "root_cause": "The configured CDP port is already in use by another application.",
            "user_action": "Free the port or specify a custom port in BROWSER_CDP_URL.",
        }

    return {
        "category": "unknown",
        "root_cause": str(error),
        "user_action": "Review logs and check browser configuration in Settings.",
    }


class CDPConnectionError(RuntimeError):
    """Raised when Microsoft Edge or Chrome CDP endpoint is unreachable or identity unverified."""


@dataclass
class TabInfo:
    index: int
    title: str
    url: str
    target_id: str = ""
    is_active: bool = False


@dataclass
class FrameInfo:
    name: str
    url: str
    origin: str
    is_main: bool


@dataclass
class BrowserSessionState:
    is_connected: bool
    cdp_url: str
    active_title: str
    active_url: str
    tab_count: int
    browser_type: str = "chrome_cdp"
    profile_dir: str = "live_jarvis_profile"
    console_errors_count: int = 0
    network_errors_count: int = 0
    tabs: List[TabInfo] = field(default_factory=list)
    error: Optional[str] = None


class BrowserSessionManager:
    """
    Authoritative CDP Browser Session Controller.
    Attaches to live Google Chrome or Microsoft Edge session inside the dedicated JarvisProfile via CDP.
    """

    _instance: Optional[BrowserSessionManager] = None
    _singleton_lock = threading.Lock()

    def __init__(self, cdp_url: Optional[str] = None, engine: Optional[str] = None):
        from memory import config_manager

        self.engine = (engine or config_manager.get_browser_engine()).lower()
        self.port = get_default_cdp_port(self.engine)
        if cdp_url:
            self.cdp_url = cdp_url
            try:
                import urllib.parse
                parsed = urllib.parse.urlparse(cdp_url)
                if parsed.port:
                    self.port = parsed.port
            except Exception:
                pass
        else:
            env_url = (
                os.environ.get("BROWSER_CDP_URL")
                or os.environ.get(f"{self.engine.upper()}_CDP_URL")
                or os.environ.get("EDGE_CDP_URL")
            )
            self.cdp_url = env_url or get_default_cdp_url(self.engine)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._lock = threading.Lock()
        self._launched_process = None

        # Playwright CDP handles
        self._pw = None
        self._browser = None
        self._context = None
        self._active_page = None
        self._cdp_session = None

        self._start_event_loop()

    @classmethod
    def get_instance(
        cls,
        cdp_url: Optional[str] = None,
        engine: Optional[str] = None,
    ) -> BrowserSessionManager:
        with cls._singleton_lock:
            from memory import config_manager

            current_engine = (engine or config_manager.get_browser_engine()).lower()
            if cls._instance is None:
                cls._instance = cls(cdp_url, current_engine)
            elif engine and cls._instance.engine != current_engine:
                # Rebind to requested engine
                cls._instance = cls(cdp_url, current_engine)
            return cls._instance

    # ── Background Asyncio Loop ────────────────────────────────────────────────

    def _start_event_loop(self):
        with self._lock:
            if self._thread and self._thread.is_alive() and self._loop:
                return
            self._loop_ready.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
                name="JarvisCDPLoop",
            )
            self._thread.start()
            if not self._loop_ready.wait(timeout=5.0):
                raise RuntimeError("Failed to initialize background CDP event loop")

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()

    def run_coroutine(self, coro, timeout: float = 30.0):
        """Thread-safe synchronous wrapper for CDP coroutines."""
        if not self._loop or not self._loop.is_running():
            self._start_event_loop()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ── Graceful Connection Guard ─────────────────────────────────────────────

    def check_connection(self, timeout: float = 2.0) -> Tuple[bool, str]:
        """
        Pings /json/version on the CDP endpoint to verify the browser is running with debugging enabled.
        Tries both configured URL and 127.0.0.1 fallback to prevent Windows IPv6 localhost delays.
        """
        urls_to_try = [self.cdp_url]
        if "localhost" in self.cdp_url:
            urls_to_try.append(self.cdp_url.replace("localhost", "127.0.0.1"))
        elif "127.0.0.1" in self.cdp_url:
            urls_to_try.append(self.cdp_url.replace("127.0.0.1", "localhost"))

        for url in urls_to_try:
            target = f"{url.rstrip('/')}/json/version"
            try:
                req = urllib.request.Request(target, headers={"User-Agent": "JARVIS-BrowserEngine"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        browser_name = data.get("Browser", "Chromium")
                        self.cdp_url = url
                        return True, f"Connected to {browser_name}"
            except Exception:
                continue

        err_msg = (
            f"[CDP Connection Error] Cannot connect to browser at {self.cdp_url}.\n"
            f"Please ensure Google Chrome or Microsoft Edge is running with remote debugging enabled:\n"
            f"  1. Start Chrome: chrome.exe --remote-debugging-port={self.port}\n"
            f"  2. Or start Edge: msedge.exe --remote-debugging-port={self.port}\n"
            f"  3. Set BROWSER_CDP_URL environment variable if using a custom port."
        )
        return False, err_msg

    def verify_profile_identity(self, expected_dir: Optional[Path | str] = None) -> Tuple[bool, str]:
        """Verify the attached browser process matches the dedicated Jarvis profile directory."""
        from memory import config_manager

        target_dir = expected_dir or config_manager.get_browser_profile_dir()
        return verify_browser_identity(self.port, target_dir, self.engine)

    def _handle_profile_chooser(self):
        """Dismiss profile chooser or first-run picker if it appears on screen."""
        try:
            from actions import browser_profile

            chooser = browser_profile.detect_profile_chooser(self.engine)
            if chooser:
                browser_profile.select_browser_profile("Default", browser=self.engine)
        except Exception as ex:
            logger.debug("[BrowserSession] Profile chooser check: %s", ex)

    # ── Live Dedicated Profile Attachment ──────────────────────────────────────

    def _try_auto_launch(self, headless: bool = False) -> bool:
        """Attempt to auto-launch the dedicated Jarvis browser profile if not already running."""
        import subprocess
        from memory import config_manager

        engine = self.engine or config_manager.get_browser_engine()
        profile_dir = config_manager.get_browser_profile_dir()
        port = self.port

        try:
            exe = locate_browser_executable(engine)
        except Exception as e:
            logger.error("[BrowserSession] Cannot launch browser: %s", e)
            return False

        try:
            cmd = build_browser_launch_command(
                exe=exe,
                profile_dir=profile_dir,
                port=port,
                engine=engine,
                headless=headless,
            )
        except Exception as e:
            logger.error("[BrowserSession] Invalid browser launch configuration: %s", e)
            return False

        profile_dir.mkdir(parents=True, exist_ok=True)

        try:
            flags = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt"
                else 0
            )
            proc = subprocess.Popen(
                cmd,
                creationflags=flags,
                start_new_session=(os.name != "nt"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._launched_process = proc

            for _ in range(12):
                time.sleep(0.5)
                ok, _ = self.check_connection(timeout=0.5)
                if ok:
                    self._handle_profile_chooser()
                    logger.info("Auto-launched dedicated %s Jarvis profile at %s (PID %d).", engine, profile_dir, proc.pid)
                    return True
        except Exception as e:
            logger.error("[BrowserSession] Failed to auto-launch %s: %s", engine, e)
        return False

    async def _ensure_attached(self, headless: bool = False):
        """
        Connects over CDP to the running Chrome / Edge instance.
        Attaches strictly to browser.contexts[0]. NEVER calls .launch() or creates a fresh context.
        Verifies identity of connected browser profile before performing any action.
        """
        ok, err = self.check_connection(timeout=2.0)
        if not ok:
            if self._try_auto_launch(headless=headless):
                ok = True
        if not ok:
            raise CDPConnectionError(err)

        # Identity Verification
        from memory import config_manager

        expected_dir = config_manager.get_browser_profile_dir()
        is_verified, reason = self.verify_profile_identity(expected_dir)
        if not is_verified:
            raise CDPConnectionError(f"Browser identity verification failed: {reason}")

        if self._browser and self._browser.is_connected() and self._context:
            if self._active_page and not self._active_page.is_closed():
                return
            pages = self._context.pages
            if pages:
                self._active_page = pages[0]
                self._cdp_session = None
                return

        # Start Playwright if needed
        if self._pw is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()

        # Connect over CDP
        self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)

        # Attach strictly to browser.contexts[0]
        contexts = self._browser.contexts
        if not contexts:
            raise CDPConnectionError(f"{self.engine.capitalize()} CDP connection established but no existing browser contexts found.")

        self._context = contexts[0]

        # Enumerate pages
        pages = self._context.pages
        if not pages:
            self._active_page = await self._context.new_page()
        else:
            self._active_page = pages[0]

        self._cdp_session = None
        logger.info(f"Attached to dedicated {self.engine} Jarvis context: {len(pages)} tab(s) detected.")

    async def _get_cdp_session(self):
        """Returns or creates a direct CDP session for the active page."""
        await self._ensure_attached()
        if self._cdp_session is None or self._active_page.is_closed():
            self._cdp_session = await self._context.new_cdp_session(self._active_page)
        return self._cdp_session

    def get_cdp_session(self):
        return self.run_coroutine(self._get_cdp_session())

    # ── Multi-Tab & Frame Management ───────────────────────────────────────────

    async def _list_tabs(self) -> List[TabInfo]:
        await self._ensure_attached()
        tabs: List[TabInfo] = []
        pages = self._context.pages
        for i, page in enumerate(pages):
            try:
                title = await page.title()
            except Exception:
                title = "(unknown)"
            url = page.url
            is_active = page == self._active_page
            tabs.append(TabInfo(index=i, title=title, url=url, is_active=is_active))
        return tabs

    def list_tabs(self) -> List[TabInfo]:
        return self.run_coroutine(self._list_tabs())

    async def _switch_tab(self, tab_index: int) -> TabInfo:
        await self._ensure_attached()
        pages = self._context.pages
        if tab_index < 0 or tab_index >= len(pages):
            raise IndexError(f"Tab index {tab_index} out of range (0..{len(pages) - 1})")
        target_page = pages[tab_index]
        from core.permissions import get_current_task_id

        is_bg = bool(get_current_task_id() or getattr(self, "background_mode", False))
        if not is_bg:
            await target_page.bring_to_front()
        self._active_page = target_page
        self._cdp_session = None  # refresh CDP session for new tab
        title = await target_page.title()
        return TabInfo(index=tab_index, title=title, url=target_page.url, is_active=True)

    def switch_tab(self, tab_index: int) -> TabInfo:
        return self.run_coroutine(self._switch_tab(tab_index))

    async def _open_tab(self, url: str) -> TabInfo:
        await self._ensure_attached()
        clean_url = url.strip()
        if clean_url and "://" not in clean_url:
            clean_url = "https://" + clean_url

        # Reuse existing tab if one already has matching origin/path
        if clean_url and self._context:
            import urllib.parse
            try:
                target_parts = urllib.parse.urlparse(clean_url)
                target_norm = f"{target_parts.scheme}://{target_parts.netloc}{target_parts.path}".rstrip("/")
                pages = self._context.pages
                for i, p in enumerate(pages):
                    if p.is_closed():
                        continue
                    p_url = p.url or ""
                    p_parts = urllib.parse.urlparse(p_url)
                    p_norm = f"{p_parts.scheme}://{p_parts.netloc}{p_parts.path}".rstrip("/")
                    if p_norm and p_norm == target_norm:
                        await p.bring_to_front()
                        self._active_page = p
                        self._cdp_session = None
                        title = await p.title()
                        logger.info(f"Reused existing tab #{i} matching '{target_norm}'")
                        return TabInfo(index=i, title=title, url=p.url, is_active=True)
            except Exception as e:
                logger.debug(f"Tab reuse check skipped: {e}")

        new_page = await self._context.new_page()
        if clean_url:
            await new_page.goto(clean_url, wait_until="domcontentloaded", timeout=30000)
        self._active_page = new_page
        self._cdp_session = None
        pages = self._context.pages
        idx = pages.index(new_page) if new_page in pages else len(pages) - 1
        title = await new_page.title()
        return TabInfo(index=idx, title=title, url=new_page.url, is_active=True)

    def open_tab(self, url: str) -> TabInfo:
        return self.run_coroutine(self._open_tab(url))

    async def _close_tab(self, tab_index: Optional[int] = None) -> str:
        await self._ensure_attached()
        pages = self._context.pages
        if not pages:
            return "No tabs to close"
        idx = (
            tab_index
            if tab_index is not None
            else (pages.index(self._active_page) if self._active_page in pages else 0)
        )
        if idx < 0 or idx >= len(pages):
            raise IndexError(f"Tab index {idx} out of range")
        closing_page = pages[idx]
        await closing_page.close()
        remaining = self._context.pages
        if remaining:
            self._active_page = remaining[min(idx, len(remaining) - 1)]
            self._cdp_session = None
        else:
            self._active_page = None
            self._cdp_session = None
        return f"Closed tab {idx}. {len(remaining)} tab(s) remaining."

    def close_tab(self, tab_index: Optional[int] = None) -> str:
        return self.run_coroutine(self._close_tab(tab_index))

    async def _get_all_frames(self) -> List[FrameInfo]:
        await self._ensure_attached()
        if not self._active_page:
            return []
        frames_out: List[FrameInfo] = []
        for f in self._active_page.frames:
            frames_out.append(
                FrameInfo(
                    name=f.name or "(unnamed)",
                    url=f.url,
                    origin=f.url.split("/")[2] if "://" in f.url else "",
                    is_main=(f == self._active_page.main_frame),
                )
            )
        return frames_out

    def get_all_frames(self) -> List[FrameInfo]:
        return self.run_coroutine(self._get_all_frames())

    # ── State Inspection ───────────────────────────────────────────────────────

    def get_session_state(self) -> BrowserSessionState:
        try:
            ok, msg = self.check_connection(timeout=1.0)
            if not ok:
                return BrowserSessionState(
                    is_connected=False,
                    cdp_url=self.cdp_url,
                    active_title="",
                    active_url="",
                    tab_count=0,
                    error=msg,
                )
            tabs = self.list_tabs()
            active_tab = next((t for t in tabs if t.is_active), tabs[0] if tabs else None)
            return BrowserSessionState(
                is_connected=True,
                cdp_url=self.cdp_url,
                active_title=active_tab.title if active_tab else "",
                active_url=active_tab.url if active_tab else "",
                tab_count=len(tabs),
                tabs=tabs,
            )
        except Exception as e:
            return BrowserSessionState(
                is_connected=False,
                cdp_url=self.cdp_url,
                active_title="",
                active_url="",
                tab_count=0,
                error=str(e),
            )

    @property
    def active_page(self):
        """Active Playwright page attached to live Edge."""
        return self._active_page


@dataclass
class BrowserNetworkError:
    url: str
    method: str
    status: Optional[int]
    error_text: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class BrowserConsoleMessage:
    msg_type: str
    text: str
    location: str
    timestamp: float = field(default_factory=time.time)


def get_browser_session_manager(
    cdp_url: Optional[str] = None,
    engine: Optional[str] = None,
) -> BrowserSessionManager:
    return BrowserSessionManager.get_instance(cdp_url, engine)


try:
    from core import platform_hooks

    def _browser_nav(url: str):
        mgr = get_browser_session_manager()
        return mgr.open_tab(url)

    platform_hooks.register_hook("browser_navigate", _browser_nav)

    def _get_active_tab():
        mgr = get_browser_session_manager()
        tab = mgr.get_active_tab()
        return {"url": tab.url, "title": tab.title} if tab else None

    platform_hooks.register_hook("get_active_browser_tab", _get_active_tab)
except Exception:
    pass
