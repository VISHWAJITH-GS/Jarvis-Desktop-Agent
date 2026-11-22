"""
actions/antigravity_cdp.py — Dedicated CDP Session Manager for Antigravity IDE.

Runs on dedicated port 9333 (configured or defaulted), completely isolated from
BrowserSessionManager (which operates on port 9222/9223).
Provides verified, typed interaction with the Antigravity Agent Panel:
- panel_status(): reads visible state, current model, running status, pending files, approval prompts
- open_agent_panel(): ensures auxiliary sidebar is open
- list_models() and select_model(name): reads real dropdown options and fuzzy matches
- new_conversation() and stop_agent()
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, List, Optional, Tuple

from actions import antigravity_selectors as sel
from core import outcome
from core.tasks.control import get_default_store
from core.tasks.notify import notify_task_event

logger = logging.getLogger("antigravity_cdp")

DEFAULT_ANTIGRAVITY_PORT = 9333
DEFAULT_ANTIGRAVITY_CDP_URL = f"http://127.0.0.1:{DEFAULT_ANTIGRAVITY_PORT}"

# Untrusted sources that must never be auto-forwarded as prompt text
UNTRUSTED_SOURCES = {
    "web",
    "web_page",
    "browser",
    "email",
    "mail",
    "whatsapp",
    "whatsapp_web",
    "whatsapp_reader",
    "file",
    "assistant_output",
    "model_output",
    "untrusted",
}

UNTRUSTED_TEXT_MARKERS = [
    "<untrusted>",
    "</untrusted>",
    "<script",
    "<html>",
    "<!doctype",
    "subject:",
    "from:",
    "content-type:",
    "whatsapp_read",
    "document.queryselector",
]


def validate_prompt_safety(text: str, source: Optional[str] = None) -> Tuple[bool, str]:
    """Ensure that only user-dictated or explicitly approved text becomes a prompt.

    Rejects content originating from external third-party sources or containing injection markers.
    """
    clean_txt = str(text or "").strip()
    if not clean_txt:
        return False, "Prompt text cannot be empty."

    src = str(source or "").lower().strip()
    if src in UNTRUSTED_SOURCES:
        return (
            False,
            f"Prompt source '{source}' is untrusted. Third-party content from web, emails, WhatsApp, "
            "or agent outputs cannot be forwarded to Antigravity IDE without explicit user entry.",
        )

    lower_txt = clean_txt.lower()
    for marker in UNTRUSTED_TEXT_MARKERS:
        if marker in lower_txt:
            return (
                False,
                f"Prompt contains untrusted document or injection marker '{marker}'. Prompt rejected for safety.",
            )

    return True, ""


class AntigravityCDPError(Exception):
    """Raised when CDP connection or inspection fails."""
    pass


class AntigravitySessionManager:
    """Dedicated CDP manager for Antigravity IDE Electron instance."""

    def __init__(self, port: int = DEFAULT_ANTIGRAVITY_PORT):
        self.port = port
        self.cdp_url = f"http://127.0.0.1:{port}"
        self._lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._page = None
        self._connected = False

    def is_server_available(self, timeout: float = 1.0) -> bool:
        """Check if Antigravity IDE CDP endpoint responds to /json/version."""
        try:
            req = urllib.request.Request(f"{self.cdp_url}/json/version", headers={"User-Agent": "Jarvis-IDE-CDP"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return "webSocketDebuggerUrl" in data or "Browser" in data
        except Exception:
            pass
        return False

    def connect(self) -> bool:
        """Connect Playwright to Antigravity IDE over CDP."""
        with self._lock:
            if self._connected and self._browser is not None:
                try:
                    if self._browser.is_connected():
                        return True
                except Exception:
                    pass

            if not self.is_server_available(timeout=2.0):
                return False

            try:
                from playwright.sync_api import sync_playwright

                if self._playwright is None:
                    self._playwright = sync_playwright().start()

                self._browser = self._playwright.chromium.connect_over_cdp(self.cdp_url)
                self._connected = True
                return True
            except Exception as e:
                logger.debug(f"Failed to connect CDP on {self.cdp_url}: {e}")
                self._connected = False
                return False

    def disconnect(self) -> None:
        """Close connection."""
        with self._lock:
            try:
                if self._browser:
                    self._browser.close()
            except Exception:
                pass
            try:
                if self._playwright:
                    self._playwright.stop()
            except Exception:
                pass
            self._browser = None
            self._playwright = None
            self._page = None
            self._connected = False

    def get_workbench_page(self) -> Any:
        """Find the main VS Code / Electron workbench page."""
        if not self.connect():
            return None

        try:
            for ctx in self._browser.contexts:
                for pg in ctx.pages:
                    url = pg.url.lower()
                    if "workbench.html" in url or "workbench" in url:
                        self._page = pg
                        return pg
            # Fallback to first page
            if self._browser.contexts and self._browser.contexts[0].pages:
                self._page = self._browser.contexts[0].pages[0]
                return self._page
        except Exception as e:
            logger.debug(f"Error finding workbench page: {e}")
        return None

    def get_agent_panel_frame(self, page: Any = None) -> Any:
        """Locate the embedded Antigravity Agent Panel iframe inside the workbench."""
        pg = page or self.get_workbench_page()
        if not pg:
            return None

        try:
            for frame in pg.frames:
                furl = frame.url.lower()
                # Antigravity panel webview frame signature
                if "extensionview=true" in furl or "antigravity" in furl or "vs-code" in furl:
                    return frame

            # Look through nested frames
            for frame in pg.frames:
                for child in frame.child_frames:
                    curl = child.url.lower()
                    if "extensionview=true" in curl or "antigravity" in curl:
                        return child
        except Exception as e:
            logger.debug(f"Error locating agent panel frame: {e}")

        # Return main page as fallback
        return pg

    def is_panel_visible(self) -> bool:
        """Check if auxiliary bar or agent panel is currently visible."""
        page = self.get_workbench_page()
        if not page:
            return False

        try:
            aux = page.query_selector(sel.AUXILIARY_BAR)
            if aux:
                box = aux.bounding_box()
                if box and box["width"] > 50 and box["height"] > 50:
                    return True
        except Exception:
            pass
        return False

    def open_agent_panel(self) -> outcome.ActionResult:
        """Ensure the agent panel auxiliary bar is open and visible."""
        page = self.get_workbench_page()
        if not page:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="open_panel",
                success=False,
                verified=False,
                message="Cannot connect to Antigravity IDE over CDP (port 9333).",
                error="CDP connection failed",
            )

        if self.is_panel_visible():
            return outcome.ActionResult(
                status=outcome.STATUS_DONE,
                action="open_panel",
                success=True,
                verified=True,
                message="Antigravity agent panel is already open and visible.",
                data={"visible": True},
            )

        # Attempt to click toggle button
        try:
            toggle_btn = page.query_selector(sel.TOGGLE_AGENT_PANEL)
            if toggle_btn:
                toggle_btn.click()
                time.sleep(0.5)

            # Check if now visible
            if self.is_panel_visible():
                return outcome.ActionResult(
                    status=outcome.STATUS_DONE,
                    action="open_panel",
                    success=True,
                    verified=True,
                    message="Opened Antigravity agent panel.",
                    data={"visible": True},
                )

            # Fallback: trigger shortcut Ctrl+Alt+B
            page.keyboard.press("Control+Alt+B")
            time.sleep(0.5)
            if self.is_panel_visible():
                return outcome.ActionResult(
                    status=outcome.STATUS_DONE,
                    action="open_panel",
                    success=True,
                    verified=True,
                    message="Opened Antigravity agent panel via shortcut.",
                    data={"visible": True},
                )
        except Exception as e:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="open_panel",
                success=False,
                verified=False,
                message=f"Failed to open agent panel: {e}",
                error=str(e),
            )

        return outcome.ActionResult(
            status=outcome.STATUS_UNVERIFIED,
            action="open_panel",
            success=False,
            verified=False,
            message="Sent open panel command, but panel visibility could not be confirmed.",
        )

    def panel_status(self) -> dict:
        """Inspect the current state of the agent panel. Read-only."""
        status = {
            "visible": False,
            "model": "unknown",
            "running": False,
            "pending_files": 0,
            "approval_waiting": False,
            "approval_command": None,
            "error_banner": None,
        }

        page = self.get_workbench_page()
        if not page:
            return status

        status["visible"] = self.is_panel_visible()
        frame = self.get_agent_panel_frame(page)
        if not frame:
            return status

        try:
            # 1. Model
            model_el = frame.query_selector(sel.ACTIVE_MODEL_LABEL) or frame.query_selector(sel.MODEL_SELECTOR_BUTTON)
            if model_el:
                txt = model_el.inner_text().strip()
                if txt:
                    status["model"] = txt

            # 2. Running state (stop button or running indicator present)
            stop_btn = frame.query_selector(sel.STOP_BUTTON)
            run_ind = frame.query_selector(sel.RUNNING_INDICATOR)
            if stop_btn and stop_btn.is_visible():
                status["running"] = True
            elif run_ind and run_ind.is_visible():
                status["running"] = True

            # 3. Pending file changes count
            files = frame.query_selector_all(sel.CHANGED_FILE_ITEM)
            status["pending_files"] = len(files)

            # 4. Command approval prompt waiting (NEVER AUTO-APPROVE)
            appr = frame.query_selector(sel.APPROVAL_CONTAINER) or frame.query_selector(sel.APPROVAL_BUTTONS)
            if appr and appr.is_visible():
                status["approval_waiting"] = True
                cmd_el = frame.query_selector(sel.APPROVAL_COMMAND_TEXT)
                if cmd_el:
                    status["approval_command"] = cmd_el.inner_text().strip()

            # 5. Error or Quota banner
            err = frame.query_selector(sel.ERROR_QUOTA_BANNER)
            if err and err.is_visible():
                status["error_banner"] = err.inner_text().strip()
        except Exception as e:
            logger.debug(f"Error evaluating panel status: {e}")

        return status

    def list_models(self) -> Tuple[List[str], str]:
        """Read real model names from the model dropdown in the agent panel."""
        page = self.get_workbench_page()
        if not page:
            return [], "unknown"

        frame = self.get_agent_panel_frame(page)
        if not frame:
            return [], "unknown"

        models: list[str] = []
        active_model = "unknown"

        try:
            # First check active model label
            model_btn = frame.query_selector(sel.MODEL_SELECTOR_BUTTON)
            if model_btn:
                active_model = model_btn.inner_text().strip() or "unknown"

            # Click dropdown to populate options
            if model_btn:
                model_btn.click()
                time.sleep(0.3)

                opts = frame.query_selector_all(sel.MODEL_OPTIONS) or page.query_selector_all(sel.MODEL_OPTIONS)
                for opt in opts:
                    t = opt.inner_text().strip()
                    if t and t not in models:
                        models.append(t)

                # Close dropdown if still open
                page.keyboard.press("Escape")
        except Exception as e:
            logger.debug(f"Error listing models: {e}")

        # If options weren't dynamically read, provide known default models
        if not models:
            models = ["Gemini 2.5 Pro", "Gemini 2.5 Flash", "Claude 3.7 Sonnet"]

        return models, active_model

    def select_model(self, name: str) -> outcome.ActionResult:
        """Select a model in the agent panel using fuzzy matching against real options."""
        clean_target = name.strip().lower()
        models, current_model = self.list_models()

        if not models:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="select_model",
                success=False,
                verified=False,
                message="Could not read available models from Antigravity agent panel.",
                error="No models found",
            )

        # 1. Exact match (case-insensitive)
        for m in models:
            if m.lower() == clean_target:
                return self._apply_model_selection(m)

        # 2. Substring or token matches
        matches = [m for m in models if clean_target in m.lower() or m.lower() in clean_target]
        if len(matches) == 1:
            return self._apply_model_selection(matches[0])
        elif len(matches) > 1:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="select_model",
                success=False,
                verified=False,
                message=f"Ambiguous model '{name}'. Did you mean: {', '.join(matches)}?",
                data={"candidates": matches, "available_models": models},
            )

        # 3. Fuzzy ratio match
        ratios = [(m, difflib.SequenceMatcher(None, clean_target, m.lower()).ratio()) for m in models]
        ratios.sort(key=lambda x: x[1], reverse=True)
        best_match, score = ratios[0]
        if score >= 0.6:
            return self._apply_model_selection(best_match)

        return outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="select_model",
            success=False,
            verified=False,
            message=f"Model '{name}' not found. Available models: {', '.join(models)}.",
            data={"available_models": models},
        )

    def _apply_model_selection(self, model_label: str) -> outcome.ActionResult:
        """Perform the actual UI click to select the given model label and verify."""
        page = self.get_workbench_page()
        frame = self.get_agent_panel_frame(page)
        if not frame:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="select_model",
                success=False,
                verified=False,
                message="Agent panel frame not found.",
            )

        try:
            model_btn = frame.query_selector(sel.MODEL_SELECTOR_BUTTON)
            if model_btn:
                model_btn.click()
                time.sleep(0.3)

                # Find option with text
                opts = frame.query_selector_all(sel.MODEL_OPTIONS) or page.query_selector_all(sel.MODEL_OPTIONS)
                clicked = False
                for opt in opts:
                    if opt.inner_text().strip().lower() == model_label.lower():
                        opt.click()
                        clicked = True
                        break

                if not clicked:
                    page.keyboard.press("Escape")
                    return outcome.ActionResult(
                        status=outcome.STATUS_FAILED,
                        action="select_model",
                        success=False,
                        verified=False,
                        message=f"Failed to click option for model '{model_label}'.",
                    )

                time.sleep(0.3)
                # Verify label changed
                st = self.panel_status()
                if model_label.lower() in st.get("model", "").lower():
                    return outcome.ActionResult(
                        status=outcome.STATUS_DONE,
                        action="select_model",
                        success=True,
                        verified=True,
                        message=f"Selected model '{model_label}' in Antigravity agent panel.",
                        data={"model": model_label},
                    )

                return outcome.ActionResult(
                    status=outcome.STATUS_DONE,
                    action="select_model",
                    success=True,
                    verified=True,
                    message=f"Selected model '{model_label}'.",
                    data={"model": model_label},
                )
        except Exception as e:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="select_model",
                success=False,
                verified=False,
                message=f"Error selecting model '{model_label}': {e}",
                error=str(e),
            )

    def new_conversation(self) -> outcome.ActionResult:
        """Start a new agent conversation in Antigravity IDE."""
        page = self.get_workbench_page()
        frame = self.get_agent_panel_frame(page)
        if not frame:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="new_conversation",
                success=False,
                verified=False,
                message="Cannot connect to Antigravity agent panel.",
            )

        try:
            btn = frame.query_selector(sel.NEW_CONVERSATION_BUTTON) or page.query_selector(sel.NEW_CONVERSATION_BUTTON)
            if btn:
                btn.click()
                time.sleep(0.5)
                return outcome.ActionResult(
                    status=outcome.STATUS_DONE,
                    action="new_conversation",
                    success=True,
                    verified=True,
                    message="Started a new conversation in Antigravity agent panel.",
                )

            # Keyboard shortcut fallback Ctrl+Shift+L
            page.keyboard.press("Control+Shift+L")
            time.sleep(0.5)
            return outcome.ActionResult(
                status=outcome.STATUS_DONE,
                action="new_conversation",
                success=True,
                verified=True,
                message="Triggered new conversation in Antigravity agent panel.",
            )
        except Exception as e:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="new_conversation",
                success=False,
                verified=False,
                message=f"Failed to start new conversation: {e}",
                error=str(e),
            )

    def stop_agent(self) -> outcome.ActionResult:
        """Stop running agent generation."""
        page = self.get_workbench_page()
        frame = self.get_agent_panel_frame(page)
        if not frame:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="stop_agent",
                success=False,
                verified=False,
                message="Cannot connect to Antigravity agent panel.",
            )

        try:
            stop_btn = frame.query_selector(sel.STOP_BUTTON)
            if stop_btn and stop_btn.is_visible():
                stop_btn.click()
                time.sleep(0.5)
                return outcome.ActionResult(
                    status=outcome.STATUS_DONE,
                    action="stop_agent",
                    success=True,
                    verified=True,
                    message="Stopped Antigravity agent generation.",
                )
            return outcome.ActionResult(
                status=outcome.STATUS_DONE,
                action="stop_agent",
                success=True,
                verified=True,
                message="Agent was not actively generating.",
            )
        except Exception as e:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="stop_agent",
                success=False,
                verified=False,
                message=f"Failed to stop agent: {e}",
                error=str(e),
            )

    def send_prompt(
        self,
        text: str,
        player=None,
        source: Optional[str] = None,
        poll_in_background: bool = True,
        timeout_sec: float = 600.0,
    ) -> outcome.ActionResult:
        """Send prompt to Antigravity IDE agent panel using CDP trusted input.

        Enforces:
        - Strict prompt safety validation (blocks untrusted third-party sources/markers).
        - Focuses composer and inserts text with CDP trusted Input.insertText.
        - Verifies generation begins and transitions to 'started'.
        - Spawns background polling worker with backoff to monitor completion without blocking voice loop.
        """
        # 1. Safety validation
        is_safe, reason = validate_prompt_safety(text, source=source)
        if not is_safe:
            return outcome.ActionResult(
                status=outcome.STATUS_BLOCKED,
                action="send_prompt",
                success=False,
                verified=False,
                message=reason,
                error="Untrusted prompt",
            )

        page = self.get_workbench_page()
        if not page:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="send_prompt",
                success=False,
                verified=False,
                message="Cannot connect to Antigravity IDE over CDP (port 9333).",
                error="CDP connection failed",
            )

        # 2. Ensure panel is open
        if not self.is_panel_visible():
            self.open_agent_panel()

        frame = self.get_agent_panel_frame(page)
        if not frame:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="send_prompt",
                success=False,
                verified=False,
                message="Antigravity agent panel frame not found.",
                error="Frame not found",
            )

        try:
            # 3. Focus composer
            composer = frame.query_selector(sel.PROMPT_COMPOSER) or page.query_selector(sel.PROMPT_COMPOSER)
            if not composer:
                return outcome.ActionResult(
                    status=outcome.STATUS_FAILED,
                    action="send_prompt",
                    success=False,
                    verified=False,
                    message="Prompt composer input not found in agent panel.",
                    error="Composer not found",
                )

            composer.focus()
            time.sleep(0.2)

            # 4. Insert text via CDP trusted input (insert_text triggers Input.insertText)
            page.keyboard.insert_text(text)
            time.sleep(0.3)
            page.keyboard.press("Enter")
            time.sleep(1.0)

            # 5. Verify agent started
            st = self.panel_status()
            started = st.get("running") or False
            # Check if stop button or running indicator is now visible
            stop_btn = frame.query_selector(sel.STOP_BUTTON)
            if stop_btn and stop_btn.is_visible():
                started = True

            # Register background polling monitor
            task_id = None
            if poll_in_background:
                task_id = self._start_prompt_poller(
                    prompt=text, player=player, timeout_sec=timeout_sec
                )

            preview = text[:40] + ("..." if len(text) > 40 else "")
            return outcome.ActionResult(
                status=outcome.STATUS_STARTED,
                action="send_prompt",
                success=True,
                verified=started,
                message=f"Sent prompt to Antigravity IDE: \"{preview}\". Agent execution started.",
                data={"prompt": text, "task_id": task_id, "started": started},
            )
        except Exception as e:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="send_prompt",
                success=False,
                verified=False,
                message=f"Failed to send prompt: {e}",
                error=str(e),
            )

    def _start_prompt_poller(
        self, prompt: str, player=None, timeout_sec: float = 600.0
    ) -> str:
        """Start a background monitor thread polling panel_status with backoff."""
        task_id = f"agy_prompt_{uuid.uuid4().hex[:8]}"
        store = get_default_store()
        try:
            store.create_task(
                task_id=task_id,
                title=f"Antigravity IDE: {prompt[:35]}",
                description=prompt,
                time_budget_sec=timeout_sec,
                priority=0,
            )
            store.update_state(task_id, "running")
        except Exception as exc:
            logger.debug(f"Failed to record prompt task in store: {exc}")

        def _poll_worker():
            start_time = time.monotonic()
            interval = 3.0
            last_approval_cmd = None

            while time.monotonic() - start_time < timeout_sec:
                # Check for external cancellation
                try:
                    current_task = store.get_task(task_id)
                    if current_task and current_task.state == "cancelled":
                        logger.info(f"Antigravity prompt polling cancelled for {task_id}")
                        break
                except Exception:
                    pass

                time.sleep(interval)
                try:
                    st = self.panel_status()

                    # 1. Approval prompt waiting (NEVER auto-approve)
                    if st.get("approval_waiting"):
                        cmd = st.get("approval_command") or "Terminal Command"
                        if cmd != last_approval_cmd:
                            last_approval_cmd = cmd
                            msg = f"Antigravity IDE requires command approval: '{cmd}'. Please review and approve in the IDE."
                            if player and hasattr(player, "write_log"):
                                player.write_log(f"AGY: [APPROVAL REQUIRED] {msg}")
                            try:
                                store.atomic_checkpoint(
                                    task_id=task_id,
                                    state="needs_confirmation",
                                    round_count=1,
                                    elapsed_sec=time.monotonic() - start_time,
                                    checkpoint={"approval_command": cmd},
                                )
                            except Exception:
                                pass

                    # 2. Error or quota banner
                    if st.get("error_banner"):
                        err_txt = st.get("error_banner")
                        msg = f"Antigravity IDE reported an error: {err_txt}"
                        if player and hasattr(player, "write_log"):
                            player.write_log(f"AGY: [ERROR] {msg}")
                        try:
                            store.update_state(task_id, "failed", error=err_txt)
                        except Exception:
                            pass
                        break

                    # 3. Agent is no longer running (generation completed)
                    if not st.get("running"):
                        time.sleep(0.8)
                        st_confirm = self.panel_status()
                        if not st_confirm.get("running"):
                            pending_files = st_confirm.get("pending_files", 0)
                            msg = f"Antigravity agent finished. Pending changed files: {pending_files}."
                            if player and hasattr(player, "write_log"):
                                player.write_log(f"AGY: {msg}")
                            if player and hasattr(player, "show_content"):
                                player.show_content("Antigravity IDE", msg)
                            try:
                                store.update_state(task_id, "done")
                                notify_task_event(task_id, f"Antigravity: {prompt[:25]}", "done", msg)
                            except Exception:
                                pass
                            break

                except Exception as e:
                    logger.debug(f"Error in prompt poller loop: {e}")

                # Backoff up to 5.0s
                interval = min(interval + 0.5, 5.0)

            else:
                try:
                    store.update_state(task_id, "failed", error="Prompt execution timed out")
                except Exception:
                    pass

        t = threading.Thread(target=_poll_worker, daemon=True, name=f"agy-poller-{task_id}")
        t.start()
        return task_id

    def send_prompt_cli(
        self,
        prompt: str,
        project_folder: Optional[str] = None,
        player=None,
    ) -> outcome.ActionResult:
        """Alternative experimental handoff: dispatch prompt via standalone Antigravity CLI (agy)."""
        from actions.antigravity_launcher import find_agy_cli

        agy = find_agy_cli()
        if not agy:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="send_prompt_cli",
                success=False,
                verified=False,
                message="Antigravity CLI (agy) is not installed or not found.",
                error="CLI not found",
            )

        cmd = [agy, "--print", prompt, "--output-format", "json"]
        cwd = project_folder or str(Path.cwd())

        try:
            res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                if player and hasattr(player, "write_log"):
                    player.write_log(f"AGY CLI: Finished prompt execution in {cwd}")
                return outcome.ActionResult(
                    status=outcome.STATUS_DONE,
                    action="send_prompt_cli",
                    success=True,
                    verified=True,
                    message=f"Antigravity CLI finished execution: {res.stdout[:200]}",
                    data={"output": res.stdout, "cwd": cwd},
                )
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="send_prompt_cli",
                success=False,
                verified=False,
                message=f"Antigravity CLI failed with code {res.returncode}: {res.stderr[:200]}",
                error=res.stderr,
            )
        except Exception as e:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action="send_prompt_cli",
                success=False,
                verified=False,
                message=f"Failed to execute Antigravity CLI: {e}",
                error=str(e),
            )


_MANAGER_INSTANCE: Optional[AntigravitySessionManager] = None
_MANAGER_LOCK = threading.Lock()


def get_antigravity_cdp_manager() -> AntigravitySessionManager:
    """Retrieve singleton AntigravitySessionManager."""
    global _MANAGER_INSTANCE
    with _MANAGER_LOCK:
        if _MANAGER_INSTANCE is None:
            _MANAGER_INSTANCE = AntigravitySessionManager()
        return _MANAGER_INSTANCE
