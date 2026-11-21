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

    async def _resolve_contact_in_page(self, page, contact_name: str) -> outcome.ActionResult:
        """Resolve and open a chat for contact_name in WhatsApp Web.

        1. If stored phone number found: direct navigate to web.whatsapp.com/send?phone=...
        2. Fallback: Search box typing, wait to filter, candidate disambiguation,
           and active chat header verification before typing.
        """
        clean_name = (contact_name or "").strip()
        if not clean_name:
            return outcome.result_failed("resolve_contact", "No contact name or phone number specified.")

        # 1. Prefer stored phone number for known contacts
        phone = find_contact_phone_number(clean_name)
        if phone:
            logger.info("[WhatsAppWeb] Resolved phone for '%s': %s (direct URL)", clean_name, phone)
            target_url = f"https://web.whatsapp.com/send?phone={phone}"
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

            # Wait for either composer, chat header, or invalid phone dialog
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                # Check for invalid phone number dialog/popup
                popup = await page.query_selector('div[data-testid="popup-contents"], div[role="dialog"]')
                if popup and await popup.is_visible():
                    text = (await popup.inner_text()) or ""
                    if any(term in text.lower() for term in ("invalid", "not exist", "phone number", "url")):
                        ok_btn = await popup.query_selector('button, [role="button"]')
                        if ok_btn:
                            try:
                                await ok_btn.click()
                            except Exception:
                                pass
                        return outcome.result_failed(
                            "resolve_contact",
                            f"Phone number '{phone}' for '{clean_name}' is not registered on WhatsApp.",
                            data={"contact": clean_name, "phone": phone},
                        )

                # Check if chat composer or active header is visible
                compose = await page.query_selector(SELECTORS.compose_box)
                if compose and await compose.is_visible():
                    return outcome.result_done(
                        "resolve_contact",
                        f"Opened chat directly for '{clean_name}' via phone {phone}.",
                        verified=True,
                        data={"contact": clean_name, "phone": phone, "method": "direct_phone"},
                    )
                await asyncio.sleep(0.4)

            # If compose box didn't appear in 15s, check if header is present
            compose = await page.query_selector(SELECTORS.compose_box)
            if compose and await compose.is_visible():
                return outcome.result_done(
                    "resolve_contact",
                    f"Opened chat for '{clean_name}'.",
                    verified=True,
                    data={"contact": clean_name, "phone": phone},
                )
            logger.info("[WhatsAppWeb] Direct phone URL timed out; falling back to search box.")

        # 2. Fall back to search box
        search_el = await page.wait_for_selector(SELECTORS.search_input, timeout=6000)
        if not search_el:
            return outcome.result_failed("resolve_contact", "WhatsApp Web layout changed: Search input box not found.")

        # Focus and clear search box via CDP
        await search_el.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.1)

        # Type contact query via CDP keyboard
        await page.keyboard.type(clean_name)
        await asyncio.sleep(1.2)  # Wait for list to filter

        # Inspect search results for ambiguity / candidates
        items = await page.query_selector_all(SELECTORS.search_result_item)
        matched_elements: List[Tuple[str, Any]] = []
        candidate_names: List[str] = []

        for it in items:
            try:
                if not await it.is_visible():
                    continue
                title_span = await it.query_selector('span[dir="auto"], span[title]')
                if title_span:
                    title = (await title_span.inner_text() or await title_span.get_attribute("title") or "").strip()
                    if title and clean_name.lower() in title.lower() and title not in candidate_names:
                        candidate_names.append(title)
                        matched_elements.append((title, it))
            except Exception:
                continue

        # Multiple matches -> Disambiguation STOP and return "ask"
        if len(candidate_names) > 1:
            return outcome.result_ask(
                "resolve_contact",
                f"Found multiple contacts matching '{clean_name}': {', '.join(candidate_names[:4])}. Which one would you like to message?",
                data={"candidates": candidate_names, "expected": clean_name},
            )

        if not matched_elements:
            # Check XPath / title exact match fallback
            chat_item = await page.query_selector(
                f'//span[@title="{clean_name}"] | //span[contains(translate(@title, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "{clean_name.lower()}")]'
            )
            if chat_item and await chat_item.is_visible():
                matched_elements.append((clean_name, chat_item))
            else:
                return outcome.result_failed(
                    "resolve_contact",
                    f"Contact '{clean_name}' not found on WhatsApp Web.",
                    data={"contact": clean_name},
                )

        # Click the single identified contact item
        chosen_title, chosen_el = matched_elements[0]
        await chosen_el.click()
        await asyncio.sleep(1.0)

        # Strict Header Verification: verify opened chat header title matches intended contact
        header_el = await page.wait_for_selector(SELECTORS.chat_header_title, timeout=5000)
        if not header_el:
            return outcome.result_failed(
                "resolve_contact",
                "WhatsApp Web layout changed: Active chat header not found after selecting contact.",
            )

        header_text = (await header_el.inner_text() or await header_el.get_attribute("title") or "").strip()
        if clean_name.lower() not in header_text.lower():
            # Header does not match -> STOP immediately!
            return outcome.result_failed(
                "resolve_contact",
                f"Opened chat header '{header_text}' does not match intended contact '{clean_name}'. Refusing to proceed.",
                data={"opened_header": header_text, "expected_contact": clean_name},
            )

        return outcome.result_done(
            "resolve_contact",
            f"Verified active chat for '{header_text}'.",
            verified=True,
            data={"contact": header_text, "expected": clean_name, "method": "search_verified"},
        )

    def resolve_contact(self, contact_name: str, background: bool = True) -> outcome.ActionResult:
        """Synchronously resolve contact and open chat on WhatsApp Web."""
        state, page, reason = self.ensure_page_ready(background=background)
        if state != "ready" or page is None:
            if state == "blocked":
                return outcome.result_blocked("resolve_contact", reason)
            return outcome.result_failed("resolve_contact", reason)

        bm = self._get_browser_manager()
        try:
            return bm.run_coroutine(self._resolve_contact_in_page(page, contact_name), timeout=35.0)
        except Exception as e:
            return outcome.result_failed("resolve_contact", f"Error resolving contact: {e}")


def find_contact_phone_number(contact_name: str) -> Optional[str]:
    """Resolve contact to an E.164-compatible clean digit string (e.g. 919876543210).

    1. If contact_name is already a phone number, clean and return.
    2. Search long-term memory categories (relationships, identity, preferences, notes)
       for contact matches containing phone numbers.
    """
    clean_target = (contact_name or "").strip()
    if not clean_target:
        return None

    # Check if contact_name itself is already a phone number
    digits_only = re.sub(r"[^\d]", "", clean_target)
    if len(digits_only) >= 10:
        if len(digits_only) == 10 and digits_only[0] in "6789":
            return f"91{digits_only}"
        return digits_only

    # Search long-term memory
    try:
        data = memory_manager.load_memory()
        target_lower = clean_target.lower()
        candidates: List[str] = []

        for category, items in data.items():
            if not isinstance(items, dict):
                continue
            for key, val_entry in items.items():
                k_lower = key.lower()
                val_text = ""
                if isinstance(val_entry, dict):
                    val_text = str(val_entry.get("value", ""))
                elif isinstance(val_entry, str):
                    val_text = val_entry

                if target_lower in k_lower or target_lower in val_text.lower():
                    # Scan for phone patterns
                    matches = re.findall(
                        r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}|\b\d{10,14}\b",
                        f"{key} {val_text}",
                    )
                    for m in matches:
                        d = re.sub(r"[^\d]", "", m)
                        if len(d) >= 10:
                            if len(d) == 10 and d[0] in "6789":
                                d = f"91{d}"
                            candidates.append(d)

        if candidates:
            return candidates[0]
    except Exception as e:
        logger.debug("[WhatsAppWeb] Memory phone search error: %s", e)

    return None


_DRIVER_INSTANCE: Optional[WhatsAppWebDriver] = None


def get_driver() -> WhatsAppWebDriver:
    """Singleton getter for the WhatsApp Web driver."""
    global _DRIVER_INSTANCE
    if _DRIVER_INSTANCE is None:
        _DRIVER_INSTANCE = WhatsAppWebDriver()
    return _DRIVER_INSTANCE
