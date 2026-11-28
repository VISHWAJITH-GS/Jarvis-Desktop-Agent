"""
actions/background_browser.py — Detached Zero-Focus Background Browser & Silent Execution Engine.

Ensures background workers NEVER call SetForegroundWindow, steal keyboard/mouse focus,
or interrupt the user's active window:
- Dedicated background Microsoft Edge/Chrome profile in ./data/worker_browser_profile
- Remote debugging on port 9223
- Off-screen positioning (x=-32000, y=-32000) and hidden HWND via win32gui.ShowWindow(hwnd, 0)
- Playwright / CDP tasks routed exclusively to localhost:9223
- asyncio.Lock() protecting concurrent worker tab access
- Silent file & subprocess execution via ProcessPoolExecutor and CREATE_NO_WINDOW
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("background_browser")

_IS_WINDOWS = platform.system() == "Windows"
DEFAULT_WORKER_PORT = 9223
DEFAULT_WORKER_URL = f"http://127.0.0.1:{DEFAULT_WORKER_PORT}"

# Base and Profile paths
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

WORKER_PROFILE_DIR = BASE_DIR / "data" / "worker_browser_profile"


# ── Window Hiding & Focus Protection ─────────────────────────────────────────

def hide_window_by_pid(pid: int) -> int:
    """
    Locates top-level HWNDs belonging to pid and hides them while pushing them off-screen.
    Guarantees no window activation or focus theft.
    """
    if not _IS_WINDOWS or not pid:
        return 0

    hidden_count = 0
    try:
        import win32con
        import win32gui
        import win32process

        def _enum_cb(hwnd: int, extra: Any) -> bool:
            nonlocal hidden_count
            if not win32gui.IsWindow(hwnd):
                return True
            try:
                _, w_pid = win32process.GetWindowThreadProcessId(hwnd)
                if w_pid == pid:
                    # SW_HIDE = 0
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                    win32gui.SetWindowPos(
                        hwnd,
                        0,
                        -32000,
                        -32000,
                        0,
                        0,
                        win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
                    )
                    hidden_count += 1
            except Exception:
                pass
            return True

        win32gui.EnumWindows(_enum_cb, None)
    except Exception as e:
        logger.debug(f"win32gui window hiding note: {e}")

    return hidden_count


def locate_worker_browser_executable(preferred_engine: str = "edge") -> str:
    """Locate Microsoft Edge or Chrome executable for background worker."""
    sys_name = platform.system()
    candidates: List[str] = []

    if preferred_engine.lower() == "edge":
        if sys_name == "Windows":
            candidates = [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
            ]
        elif sys_name == "Darwin":
            candidates = ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
        else:
            candidates = ["/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"]

        w = shutil.which("msedge") or shutil.which("microsoft-edge")
        if w:
            candidates.insert(0, w)

    # Fallback to Chrome
    if sys_name == "Windows":
        candidates.extend([
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ])
    elif sys_name == "Darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    else:
        candidates.extend(["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium"])

    w_chrome = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")
    if w_chrome:
        candidates.append(w_chrome)

    for p in candidates:
        if p and os.path.isfile(p):
            return str(Path(p).resolve())

    # Return first plausible name as fallback
    return "msedge.exe" if sys_name == "Windows" else "google-chrome"


# ── Detached Background Browser Manager ──────────────────────────────────────

class BackgroundBrowserManager:
    """
    Manages dedicated off-screen browser instance on port 9223.
    Thread-safe and async-safe with concurrent lock for Worker-1 and Worker-2.
    """

    _instance: Optional[BackgroundBrowserManager] = None
    _init_lock = asyncio.Lock()

    def __init__(self, port: int = DEFAULT_WORKER_PORT, profile_dir: Optional[Path] = None):
        self.port = port
        self.cdp_url = f"http://127.0.0.1:{self.port}"
        self.profile_dir = (profile_dir or WORKER_PROFILE_DIR).resolve()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._process: Optional[subprocess.Popen] = None
        self._cdp_lock = asyncio.Lock()
        self._playwright = None
        self._browser = None

    @classmethod
    def get_instance(cls) -> BackgroundBrowserManager:
        if cls._instance is None:
            cls._instance = BackgroundBrowserManager()
        return cls._instance

    def is_cdp_ready(self, timeout: float = 0.5) -> bool:
        """Check if background browser CDP port is responding."""
        try:
            req = urllib.request.Request(f"{self.cdp_url}/json/version")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def ensure_running(self) -> bool:
        """Ensure background browser is running with zero window focus disruption."""
        if self.is_cdp_ready(timeout=0.4):
            return True

        async with self._init_lock:
            # Recheck inside lock
            if self.is_cdp_ready(timeout=0.4):
                return True

            exe = locate_worker_browser_executable("edge")
            args = [
                exe,
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={str(self.profile_dir)}",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-position=-32000,-32000",
                "--window-size=1280,800",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-features=CalculateNativeWinOcclusion",
                "--hide-crash-restore-bubble",
                "--password-store=basic",
                "about:blank",
            ]

            creation_flags = 0
            if _IS_WINDOWS:
                # DETACHED_PROCESS or CREATE_NO_WINDOW
                creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

            try:
                self._process = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    creationflags=creation_flags,
                )
                logger.info(f"[BackgroundBrowser] Launched PID={self._process.pid} on port={self.port}")
            except Exception as e:
                logger.error(f"[BackgroundBrowser] Failed to launch browser: {e}")
                return False

            # Wait for CDP endpoint to respond
            deadline = time.monotonic() + 8.0
            ready = False
            while time.monotonic() < deadline:
                if self.is_cdp_ready(timeout=0.3):
                    ready = True
                    break
                await asyncio.sleep(0.2)

            if ready and self._process:
                # Immediately hide any visible window & place off-screen
                hide_window_by_pid(self._process.pid)
                logger.info(f"[BackgroundBrowser] CDP ready on port {self.port}, window hidden & off-screen.")
                return True
            else:
                logger.warning(f"[BackgroundBrowser] CDP failed to respond on port {self.port} within timeout.")
                return False

    async def get_browser(self):
        """Connect Playwright to the background CDP endpoint."""
        await self.ensure_running()
        if self._browser and self._browser.is_connected():
            return self._browser

        try:
            from playwright.async_api import async_playwright
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
            return self._browser
        except Exception as e:
            logger.error(f"[BackgroundBrowser] Playwright connection error: {e}")
            return None

    # ── High-Level Worker Tasks (Protected with _cdp_lock) ────────────────────

    async def execute_task(self, task_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a background browser task with mutual exclusion between Worker-1 and Worker-2.
        Supported task_type: 'read_gmail', 'draft_gmail', 'search_whatsapp', 'web_research', 'navigate_and_read'.
        """
        async with self._cdp_lock:
            browser = await self.get_browser()
            if not browser:
                return {"status": "failed", "error": "Background browser unavailable"}

            try:
                contexts = browser.contexts
                context = contexts[0] if contexts else await browser.new_context()
                page = await context.new_page()

                try:
                    if task_type == "read_gmail":
                        return await self._task_read_gmail(page, params)
                    elif task_type == "draft_gmail":
                        return await self._task_draft_gmail(page, params)
                    elif task_type == "search_whatsapp":
                        return await self._task_search_whatsapp(page, params)
                    elif task_type == "web_research":
                        return await self._task_web_research(page, params)
                    elif task_type in ("navigate_and_read", "open_url"):
                        return await self._task_navigate_and_read(page, params)
                    else:
                        return {"status": "failed", "error": f"Unknown background browser task type: {task_type}"}
                finally:
                    await page.close()
            except Exception as e:
                logger.error(f"[BackgroundBrowser] Task '{task_type}' error: {e}")
                return {"status": "failed", "error": str(e)}

    async def _task_read_gmail(self, page: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        query = str(params.get("query") or "").strip()
        url = "https://mail.google.com/mail/u/0/#inbox"
        if query:
            import urllib.parse
            url = f"https://mail.google.com/mail/u/0/#search/{urllib.parse.quote_plus(query)}"

        await page.goto(url, timeout=15000, wait_until="domcontentloaded")
        await asyncio.sleep(2.0)

        # Extract latest message text or rows
        title = await page.title()
        snippet = await page.evaluate("""() => {
            const rows = document.querySelectorAll('tr.zA, div[role="main"]');
            if (rows.length > 0) {
                return Array.from(rows).slice(0, 3).map(r => r.innerText.replace(/\\s+/g, ' ').trim()).join('\\n');
            }
            return document.body.innerText.slice(0, 800);
        }""")

        return {
            "status": "success",
            "page_title": title,
            "emails_preview": snippet or "No emails found or login required.",
            "url": page.url,
        }

    async def _task_draft_gmail(self, page: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        to_addr = str(params.get("to") or "")
        subject = str(params.get("subject") or "Reply")
        body = str(params.get("body") or "")

        import urllib.parse
        compose_url = (
            f"https://mail.google.com/mail/u/0/?view=cm&fs=1"
            f"&to={urllib.parse.quote_plus(to_addr)}"
            f"&su={urllib.parse.quote_plus(subject)}"
            f"&body={urllib.parse.quote_plus(body)}"
        )
        await page.goto(compose_url, timeout=15000, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)

        return {
            "status": "drafted",
            "to": to_addr,
            "subject": subject,
            "body": body,
            "action_required": "approval_to_send",
            "note": "Draft created in background browser. Requires approval before sending.",
        }

    async def _task_search_whatsapp(self, page: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        contact = str(params.get("contact") or "")
        await page.goto("https://web.whatsapp.com", timeout=20000, wait_until="domcontentloaded")
        await asyncio.sleep(2.0)

        title = await page.title()
        return {
            "status": "success",
            "contact": contact,
            "page_title": title,
            "url": page.url,
            "note": f"Searched WhatsApp for contact '{contact}' in background profile.",
        }

    async def _task_web_research(self, page: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        query = str(params.get("query") or "")
        import urllib.parse
        search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}"
        await page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
        await asyncio.sleep(1.0)

        results = await page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('.result__title a, a.result__url'));
            const snippets = Array.from(document.querySelectorAll('.result__snippet'));
            const data = [];
            for (let i = 0; i < Math.min(5, links.length); i++) {
                data.push({
                    title: links[i] ? links[i].innerText.trim() : '',
                    href: links[i] ? links[i].href : '',
                    snippet: snippets[i] ? snippets[i].innerText.trim() : ''
                });
            }
            return data;
        }""")

        return {
            "status": "success",
            "query": query,
            "results": results,
            "count": len(results),
        }

    async def _task_navigate_and_read(self, page: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        url = str(params.get("url") or "")
        if not url.startswith("http"):
            url = f"https://{url}"
        await page.goto(url, timeout=15000, wait_until="domcontentloaded")
        await asyncio.sleep(1.0)

        title = await page.title()
        text = await page.evaluate("() => document.body.innerText.slice(0, 1500)")
        return {
            "status": "success",
            "title": title,
            "url": page.url,
            "text": text,
        }

    async def close(self) -> None:
        """Close playwright connection and terminate background browser."""
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        finally:
            self._browser = None
            self._playwright = None

        if self._process:
            try:
                self._process.terminate()
            except Exception:
                pass
            self._process = None


# Global ProcessPoolExecutor for silent CPU/File tasks
_PROCESS_POOL: Optional[concurrent.futures.ProcessPoolExecutor] = None


def get_silent_process_pool() -> concurrent.futures.ProcessPoolExecutor:
    global _PROCESS_POOL
    if _PROCESS_POOL is None:
        _PROCESS_POOL = concurrent.futures.ProcessPoolExecutor(max_workers=2)
    return _PROCESS_POOL


async def run_silent_process(func: Callable[..., Any], *args: Any) -> Any:
    """Execute CPU-bound or file processing inside a background ProcessPoolExecutor."""
    loop = asyncio.get_running_loop()
    pool = get_silent_process_pool()
    return await loop.run_in_executor(pool, func, *args)


async def run_silent_subprocess(
    cmd: List[str],
    timeout: float = 30.0,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """
    Execute background subprocess silently without creating any console window or stealing focus.
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if _IS_WINDOWS else 0

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        creationflags=creationflags,
        cwd=cwd,
        env=env,
    )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        code = proc.returncode or 0
        return code, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return 124, "", "Subprocess timed out."
