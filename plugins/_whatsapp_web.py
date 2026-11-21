"""
plugins/_whatsapp_web.py — Unified WhatsApp Web Driver over Chrome DevTools Protocol (CDP).

Exclusively attaches to the dedicated Jarvis Chrome profile via BrowserSessionManager.
Never launches a second browser, never touches user's personal default profile,
never uses pyautogui/clipboard/desktop UIA/window focus stealing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core import outcome
from memory import config_manager, memory_manager

logger = logging.getLogger("whatsapp_web")


@dataclass(frozen=True)
class WhatsAppSelectors:
    """Isolated, centralized DOM selectors for web.whatsapp.com.

    Prefer semantic attributes (data-testid, aria-label, role, contenteditable).
    If WhatsApp Web alters its DOM structure, updates are contained strictly here.
    """

    # Authentication / QR Code
    qr_canvas: str = 'canvas[aria-label*="Scan"], canvas[aria-label*="código QR"], canvas[role="img"]'
    qr_container: str = 'div[data-ref], [data-testid="qrcode"], div[data-testid="intro-title"]'
    intro_screen: str = 'div[data-testid="intro-title"], span:has-text("To use WhatsApp on your computer")'

    # Syncing / Startup Progress
    startup_progress: str = 'progress, [data-testid="startup-progress"], div[role="progressbar"]'
    syncing_spinner: str = 'span[data-testid="syncing-spinner"], div:has-text("Loading your chats"), div:has-text("Connecting")'

    # Main Panels & Chat List
    chat_list_panel: str = '#side, [data-testid="chat-list"], div[id="pane-side"]'
    main_chat_panel: str = '#main, [data-testid="conversation-panel-wrapper"]'

    # Search Box
    search_input: str = 'div[contenteditable="true"][data-tab="3"], div[title="Search input textbox"], [data-testid="chat-list-search"]'
    search_results_container: str = 'div[data-testid="search-results"], div[aria-label="Search results."]'
    search_result_item: str = 'div[data-testid="cell-frame-container"], div[role="listitem"]'
    search_cancel_btn: str = 'button[aria-label="Cancel search"], [data-testid="search-cancel"], button[aria-label="Close search"]'

    # Active Conversation Header
    chat_header: str = 'header[data-testid="conversation-header"], header'
    chat_header_title: str = 'header span[dir="auto"], header span[title], [data-testid="conversation-info-header-chat-title"]'

    # Message Composer
    compose_box: str = 'footer div[contenteditable="true"], div[contenteditable="true"][data-tab="10"], [data-testid="conversation-compose-box-input"]'
    send_button: str = 'button[aria-label="Send"], span[data-icon="send"], [data-testid="send"]'

    # Outgoing Message Delivery Ticks
    message_outgoing: str = 'div.message-out, [data-testid="msg-container"]:has(span[data-icon*="check"]), [data-testid="msg-container"]:has(span[data-icon="status-time"])'
    tick_pending_clock: str = 'span[data-icon="status-time"], [data-testid="status-time"]'
    tick_sent_single: str = 'span[data-icon="check"], [data-testid="msg-check"], span[data-icon="msg-check"]'
    tick_delivered_double: str = 'span[data-icon="msg-dblcheck"], [data-testid="msg-dblcheck"]'
    tick_read_blue: str = 'span[data-icon="msg-dblcheck-ack"], [data-testid="msg-dblcheck-ack"]'

    # Incoming / Outgoing Messages Inspection
    message_container: str = '[data-testid="msg-container"], div.message-in, div.message-out'
    message_text: str = 'span.selectable-text, div.copyable-text'
    message_author: str = 'span[data-testid="author"], div[data-pre-plain-text]'
    message_time: str = 'div[data-pre-plain-text], span[dir="auto"]'


SELECTORS = WhatsAppSelectors()


class WhatsAppWebDriver:
    """CDP-powered automation controller for WhatsApp Web running in Jarvis Chrome Profile."""

    def __init__(self):
        self._cached_page = None
        self._last_state: str = "unknown"

    def _get_browser_manager(self):
        from actions.browser_session import get_browser_session_manager

        bm = get_browser_session_manager()
        return bm

    def verify_profile_safe(self) -> Tuple[bool, str]:
        """Strict check: ensure connected browser instance is the dedicated Jarvis profile."""
        bm = self._get_browser_manager()
        expected_dir = config_manager.get_browser_profile_dir()
        is_verified, reason = bm.verify_profile_identity(expected_dir)
        if not is_verified:
            return False, f"Browser identity check failed: {reason}"
        return True, "Verified Jarvis dedicated profile."

    async def _find_or_open_whatsapp_page(self, bm, background: bool = True):
        """Locate existing WhatsApp Web tab or open a new tab without stealing window focus."""
        await bm._ensure_attached()
        if not bm._context:
            raise RuntimeError("Browser attached but no context found.")

        # Check existing cached page
        if self._cached_page and not self._cached_page.is_closed():
            if "web.whatsapp.com" in (self._cached_page.url or "").lower():
                return self._cached_page

        # Scan open pages
        pages = bm._context.pages
        for p in pages:
            try:
                url = p.url or ""
                if "web.whatsapp.com" in url.lower():
                    self._cached_page = p
                    return p
            except Exception:
                continue

        # None found: open dedicated WhatsApp tab
        new_page = await bm._context.new_page()
        # Navigate without bringing to front
        await new_page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=45000)
        self._cached_page = new_page
        return new_page

    async def _detect_state(self, page, timeout: float = 12.0) -> Tuple[str, str]:
        """Detect current WhatsApp Web state:

        Returns:
          - ("blocked", reason): QR code visible, user needs to scan
          - ("loading", reason): chats syncing or connecting
          - ("ready", reason): chat list loaded and ready for interaction
          - ("failed", reason): layout changed or unexpected failure
        """
        deadline = time.monotonic() + max(timeout, 2.0)

        while time.monotonic() < deadline:
            # 1. Check for QR Code (Not signed in)
            try:
                qr_el = await page.query_selector(SELECTORS.qr_canvas) or await page.query_selector(SELECTORS.qr_container)
                if qr_el and await qr_el.is_visible():
                    return "blocked", "Scan the QR code in the Jarvis Chrome window to log into WhatsApp Web."
            except Exception:
                pass

            # 2. Check for Startup Syncing / Progress
            try:
                progress_el = await page.query_selector(SELECTORS.startup_progress) or await page.query_selector(SELECTORS.syncing_spinner)
                if progress_el and await progress_el.is_visible():
                    # Still syncing; wait a moment for next check
                    await asyncio.sleep(0.5)
                    continue
            except Exception:
                pass

            # 3. Check for Ready State (Chat List or Side Pane visible)
            try:
                side_el = await page.query_selector(SELECTORS.chat_list_panel) or await page.query_selector(SELECTORS.search_input)
                if side_el and await side_el.is_visible():
                    return "ready", "WhatsApp Web is signed in and chat list is ready."
            except Exception:
                pass

            await asyncio.sleep(0.4)

        # Timed out without matching known states
        # Final inspection: did QR code appear late?
        try:
            qr_el = await page.query_selector(SELECTORS.qr_canvas) or await page.query_selector(SELECTORS.qr_container)
            if qr_el and await qr_el.is_visible():
                return "blocked", "Scan the QR code in the Jarvis Chrome window to log into WhatsApp Web."
        except Exception:
            pass

        return "failed", "WhatsApp Web layout changed: failed to detect chat list or QR code within timeout."

    def ensure_page_ready(self, background: bool = True, timeout: float = 12.0) -> Tuple[str, Any, str]:
        """Synchronously verify profile, obtain page, and verify ready state.

        Returns (status, page, message). Status is "ready", "blocked", or "failed".
        """
        safe, why = self.verify_profile_safe()
        if not safe:
            return "blocked", None, why

        bm = self._get_browser_manager()

        async def _ensure():
            page = await self._find_or_open_whatsapp_page(bm, background=background)
            state, reason = await self._detect_state(page, timeout=timeout)
            return state, page, reason

        try:
            state, page, reason = bm.run_coroutine(_ensure(), timeout=max(timeout + 5.0, 15.0))
            self._last_state = state
            return state, page, reason
        except Exception as e:
            return "failed", None, f"Could not connect to WhatsApp Web: {e}"

    def check_state(self) -> outcome.ActionResult:
        """High-level state probe for UI / diagnostic verification."""
        state, page, reason = self.ensure_page_ready(background=True, timeout=8.0)
        if state == "blocked":
            return outcome.result_blocked("whatsapp_state", reason, data={"state": state})
        elif state == "ready":
            return outcome.result_done("whatsapp_state", reason, verified=True, data={"state": state})
        return outcome.result_failed("whatsapp_state", reason, data={"state": state})


_DRIVER_INSTANCE: Optional[WhatsAppWebDriver] = None


def get_driver() -> WhatsAppWebDriver:
    """Singleton getter for the WhatsApp Web driver."""
    global _DRIVER_INSTANCE
    if _DRIVER_INSTANCE is None:
        _DRIVER_INSTANCE = WhatsAppWebDriver()
    return _DRIVER_INSTANCE
