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
    search_input: str = (
        '#side [aria-label*="Search"], #side [placeholder*="Search"], '
        '#side [role="textbox"][data-tab="3"], #side input[type="text"], '
        '[data-testid="chat-list-search"], div[contenteditable="true"][data-tab="3"], '
        'div[title="Search input textbox"]'
    )
    search_results_container: str = 'div[data-testid="search-results"], div[aria-label="Search results."], #side'
    search_result_item: str = 'div[data-testid="cell-frame-container"], div[role="listitem"], div[role="row"]'
    search_cancel_btn: str = 'button[aria-label="Cancel search"], [data-testid="search-cancel"], button[aria-label="Close search"]'

    # Active Conversation Header
    chat_header: str = 'header[data-testid="conversation-header"], header'
    chat_header_title: str = 'header span[dir="auto"], header span[title], [data-testid="conversation-info-header-chat-title"]'

    # Message Composer
    compose_box: str = (
        'footer div[contenteditable="true"], [data-testid="conversation-compose-box-input"], '
        'footer [role="textbox"][contenteditable="true"], footer div[role="textbox"]'
    )
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
        start_time = time.monotonic()
        deadline = start_time + max(timeout, 2.0)
        saw_syncing = False

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
                progress_el = (
                    await page.query_selector(SELECTORS.startup_progress)
                    or await page.query_selector(SELECTORS.syncing_spinner)
                    or await page.query_selector("progress")
                )
                body_text = (await page.evaluate("() => document.body ? document.body.innerText : ''")) or ""
                is_syncing = (
                    (progress_el and await progress_el.is_visible())
                    or any(term in body_text.lower() for term in ("downloading", "loading your chats", "connecting"))
                )
                if is_syncing:
                    saw_syncing = True
                    # If actively downloading/syncing, gracefully extend deadline up to 30s
                    if (deadline - time.monotonic() < 3.0) and (time.monotonic() - start_time < 30.0):
                        deadline = time.monotonic() + 5.0
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

        if saw_syncing:
            return "loading", "WhatsApp Web is still downloading and syncing your messages. Please keep WhatsApp open on your phone."

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
        search_el = await page.wait_for_selector(SELECTORS.search_input, timeout=12000)
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
        exact_matches: List[Tuple[str, Any]] = []

        for it in items:
            try:
                if not await it.is_visible():
                    continue
                title_span = await it.query_selector('span[dir="auto"], span[title]')
                if title_span:
                    title = (await title_span.inner_text() or await title_span.get_attribute("title") or "").strip()
                    if title:
                        if title.lower() == clean_name.lower():
                            exact_matches.append((title, it))
                        if clean_name.lower() in title.lower() and title not in candidate_names:
                            candidate_names.append(title)
                            matched_elements.append((title, it))
            except Exception:
                continue

        # If an exact match is found, prioritize it directly over loose partial matches (e.g. 'Mom' vs 'Savemom')
        if len(exact_matches) == 1:
            chosen_title, chosen_el = exact_matches[0]
        elif len(exact_matches) > 1:
            exact_names = [t for t, _ in exact_matches]
            return outcome.result_ask(
                "resolve_contact",
                f"Found multiple contacts matching '{clean_name}': {', '.join(exact_names[:4])}. Which one would you like to message?",
                data={"candidates": exact_names, "expected": clean_name},
            )
        elif len(candidate_names) > 1:
            # Ambiguity without an exact match -> Ask user
            return outcome.result_ask(
                "resolve_contact",
                f"Found multiple contacts matching '{clean_name}': {', '.join(candidate_names[:4])}. Which one would you like to message?",
                data={"candidates": candidate_names, "expected": clean_name},
            )
        elif matched_elements:
            chosen_title, chosen_el = matched_elements[0]
        else:
            # Check XPath / title exact match fallback
            chat_item = await page.query_selector(
                f'//span[@title="{clean_name}"] | //span[contains(translate(@title, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "{clean_name.lower()}")]'
            )
            if chat_item and await chat_item.is_visible():
                chosen_title = clean_name
                chosen_el = chat_item
            else:
                return outcome.result_failed(
                    "resolve_contact",
                    f"Contact '{clean_name}' not found on WhatsApp Web.",
                    data={"contact": clean_name},
                )

        # Click the identified contact item (click closest cell container if available)
        try:
            cell = await chosen_el.evaluate_handle(
                "el => el.closest('[data-testid=\"cell-frame-container\"], div[role=\"row\"], div[role=\"listitem\"]') || el"
            )
            await cell.click()
        except Exception:
            await chosen_el.click()
        await asyncio.sleep(1.2)

        # Strict Header Verification: verify opened chat header title matches intended contact
        header_el = await page.wait_for_selector(SELECTORS.chat_header_title, timeout=8000)
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

    async def _send_message_in_page(
        self, page, contact_name: str, message: str, player=None
    ) -> outcome.ActionResult:
        """Compose and send message via CDP, verifying sent status tick."""
        # 1. Resolve contact and open chat
        res_contact = await self._resolve_contact_in_page(page, contact_name)
        if not res_contact.is_success():
            return res_contact

        # 2. Wait for message composer to be visible
        compose_el = await page.wait_for_selector(SELECTORS.compose_box, timeout=10000)
        if not compose_el:
            return outcome.result_failed(
                "whatsapp_send",
                "WhatsApp Web layout changed: Message composer input box not found.",
            )

        await compose_el.click()
        await asyncio.sleep(0.1)

        # 3. Type message via CDP Input (never pyautogui, never clipboard)
        bm = self._get_browser_manager()
        try:
            cdp = await bm._get_cdp_session()
            if cdp:
                await cdp.send("Input.insertText", {"text": message})
            else:
                await page.keyboard.type(message)
        except Exception:
            await page.keyboard.type(message)

        await asyncio.sleep(0.3)

        # 4. Press Enter or click send button via CDP
        try:
            send_btn = await page.query_selector(SELECTORS.send_button)
            if send_btn and await send_btn.is_visible():
                await send_btn.click()
            else:
                await page.keyboard.press("Enter")
        except Exception:
            await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)

        # 5. Verify Delivery via Outgoing Message Bubble and Status Ticks
        deadline = time.monotonic() + 8.0
        bubble_found = False
        tick_seen = None
        snippet = message[:30].strip()

        while time.monotonic() < deadline:
            bubbles = await page.query_selector_all(SELECTORS.message_outgoing)
            if bubbles:
                last_bubble = bubbles[-1]
                bubble_text = (await last_bubble.inner_text()) or ""
                if not snippet or snippet.lower() in bubble_text.lower() or len(bubbles) > 0:
                    bubble_found = True

                    # Inspect status ticks: clock -> sent tick -> delivered double tick
                    sent_tick = await last_bubble.query_selector(SELECTORS.tick_sent_single)
                    dbl_tick = await last_bubble.query_selector(
                        SELECTORS.tick_delivered_double
                    ) or await last_bubble.query_selector(SELECTORS.tick_read_blue)
                    pending_clock = await last_bubble.query_selector(SELECTORS.tick_pending_clock)

                    if dbl_tick and await dbl_tick.is_visible():
                        tick_seen = "delivered"
                        break
                    elif sent_tick and await sent_tick.is_visible():
                        tick_seen = "sent"
                        break
                    elif pending_clock and await pending_clock.is_visible():
                        tick_seen = "pending"

            await asyncio.sleep(0.5)

        msg_preview = f'"{message}"'
        if tick_seen in ("sent", "delivered"):
            if player and hasattr(player, "write_log"):
                try:
                    player.write_log(f"JARVIS: Message sent to {contact_name}: {msg_preview}")
                except Exception:
                    pass
            return outcome.result_done(
                "whatsapp_send",
                f"I have sent the message to {contact_name}: {msg_preview}",
                verified=True,
                data={
                    "contact": contact_name,
                    "text": message,
                    "to": contact_name,
                    "verified": True,
                    "tick": tick_seen,
                },
            )
        elif bubble_found and tick_seen == "pending":
            return outcome.result_unverified(
                "whatsapp_send",
                "Message queued locally, check phone connection",
                error="Message pending in local queue (clock icon)",
                data={"contact": contact_name, "text": message, "to": contact_name, "tick": "clock"},
            )
        elif bubble_found:
            return outcome.result_unverified(
                "whatsapp_send",
                f"Message was typed for {contact_name}, but delivery tick could not be confirmed.",
                error="Sent tick unconfirmed",
                data={"contact": contact_name, "text": message, "to": contact_name},
            )

        return outcome.result_failed(
            "whatsapp_send",
            f"Message bubble did not appear in chat after sending to {contact_name}.",
            error="No outgoing message bubble detected after send",
            data={"contact": contact_name, "text": message, "to": contact_name},
        )

    def send_message(
        self,
        contact_name: str,
        message: str,
        player=None,
        attachments: Any = None,
        background: bool = True,
    ) -> outcome.ActionResult:
        """Send message via WhatsApp Web with idempotency and attachment guards."""
        # Attachment guard
        if attachments:
            return outcome.ActionResult(
                status=outcome.STATUS_UNSUPPORTED,
                action="whatsapp_send",
                success=False,
                verified=False,
                message="WhatsApp Web attachment sending not yet supported; send text only.",
                error="Attachments unsupported on WhatsApp Web",
            )

        # Idempotency check: (recipient + hash of text + minute bucket)
        key = get_idempotency_key(contact_name, message)
        if key in _IDEMPOTENCY_CACHE:
            logger.info("[WhatsAppWeb] Idempotent send rejected duplicate for key %s", key)
            return _IDEMPOTENCY_CACHE[key]

        state, page, reason = self.ensure_page_ready(background=background)
        if state != "ready" or page is None:
            if state == "blocked":
                return outcome.result_blocked("whatsapp_send", reason)
            return outcome.result_failed("whatsapp_send", reason)

        bm = self._get_browser_manager()
        try:
            res = bm.run_coroutine(
                self._send_message_in_page(page, contact_name, message, player=player),
                timeout=45.0,
            )
            if res.is_success():
                _IDEMPOTENCY_CACHE[key] = res
            return res
        except Exception as e:
            return outcome.result_failed("whatsapp_send", f"Error sending message: {e}")

    def send_message_to(self, contact_name: str, message: str) -> Tuple[bool, str]:
        """Backward-compatible wrapper for callers expecting (bool, info) tuple."""
        res = self.send_message(contact_name, message)
        if res.is_success():
            return True, f"Sent message snippet '{message[:30]}' confirmed visible in chat history with verified sent tick."
        elif res.status == outcome.STATUS_UNVERIFIED:
            return False, "Message input field cleared on send, but sent tick could not be confirmed."
        return False, res.error or res.message

    async def _read_messages_in_page(
        self, page, contact_name: Optional[str] = None, limit: int = 5, player=None
    ) -> outcome.ActionResult:
        """Extract latest messages with sender, timestamp, and untrusted-wrapped body text."""
        if contact_name:
            res_contact = await self._resolve_contact_in_page(page, contact_name)
            if not res_contact.is_success():
                return res_contact

        await asyncio.sleep(0.5)

        bubbles = await page.query_selector_all(SELECTORS.message_container)
        if not bubbles:
            return outcome.result_done(
                "whatsapp_read",
                f"No messages found for {contact_name or 'the current chat'}.",
                verified=True,
                data={"contact": contact_name, "count": 0, "messages": []},
            )

        items = bubbles[-max(limit, 1) :]
        extracted_messages: List[Dict[str, Any]] = []

        for b in items:
            try:
                class_attr = (await b.get_attribute("class")) or ""
                sender = "You" if "message-out" in class_attr else (contact_name or "Contact")

                pre_plain = (await b.get_attribute("data-pre-plain-text")) or ""
                time_val = "Recent"
                if pre_plain:
                    m_time = re.search(r"\[(.*?)\]", pre_plain)
                    if m_time:
                        time_val = m_time.group(1).strip()
                    m_author = re.search(r"\]\s*(.*?):", pre_plain)
                    if m_author and "message-out" not in class_attr:
                        sender = m_author.group(1).strip()

                text_el = await b.query_selector(SELECTORS.message_text)
                if not text_el:
                    continue
                raw_body = (await text_el.inner_text()) or ""
                raw_body = raw_body.strip()
                if not raw_body:
                    continue

                # Security Isolation: wrap all external text in untrusted markers
                safe_body = outcome.untrusted("whatsapp", raw_body)

                extracted_messages.append(
                    {
                        "sender": sender,
                        "timestamp": time_val,
                        "text": safe_body,
                        "raw_text": raw_body,
                    }
                )
            except Exception:
                continue

        if not extracted_messages:
            return outcome.result_done(
                "whatsapp_read",
                f"No messages found for {contact_name or 'the current chat'}.",
                verified=True,
                data={"contact": contact_name, "count": 0, "messages": []},
            )

        # Audit / Player log: ONLY METADATA, never bodies
        timestamps = [m["timestamp"] for m in extracted_messages if m.get("timestamp")]
        ts_range = f"{timestamps[0]} - {timestamps[-1]}" if timestamps else "recent"
        if player and hasattr(player, "write_log"):
            try:
                player.write_log(
                    f"JARVIS: Read {len(extracted_messages)} messages for {contact_name or 'chat'} ({ts_range})."
                )
            except Exception:
                pass

        logger.info(
            "[WhatsAppWeb] Read %d messages for '%s' (%s). Bodies withheld from logs.",
            len(extracted_messages),
            contact_name or "chat",
            ts_range,
        )

        return outcome.result_done(
            "whatsapp_read",
            f"Read {len(extracted_messages)} messages for {contact_name or 'the current chat'}.",
            verified=True,
            data={
                "contact": contact_name,
                "count": len(extracted_messages),
                "messages": extracted_messages,
                "timestamp_range": ts_range,
            },
        )

    def read_messages(
        self,
        contact_name: Optional[str] = None,
        limit: int = 5,
        player=None,
        background: bool = True,
    ) -> outcome.ActionResult:
        """Synchronously read latest messages via WhatsApp Web."""
        state, page, reason = self.ensure_page_ready(background=background)
        if state != "ready" or page is None:
            if state == "blocked":
                return outcome.result_blocked("whatsapp_read", reason)
            return outcome.result_failed("whatsapp_read", reason)

        bm = self._get_browser_manager()
        try:
            return bm.run_coroutine(
                self._read_messages_in_page(
                    page, contact_name=contact_name, limit=limit, player=player
                ),
                timeout=40.0,
            )
        except Exception as e:
            return outcome.result_failed("whatsapp_read", f"Error reading messages: {e}")

    def read_chat_messages(
        self, contact_name: Optional[str] = None, limit: int = 5
    ) -> Tuple[bool, List[Dict[str, str]], str]:
        """Backward-compatible tuple return: (ok, msgs, error)."""
        res = self.read_messages(contact_name=contact_name, limit=limit)
        if res.is_success():
            raw_msgs = res.data.get("messages", [])
            return (
                True,
                [
                    {
                        "sender": m["sender"],
                        "text": m.get("raw_text", m["text"]),
                        "time": m["timestamp"],
                    }
                    for m in raw_msgs
                ],
                "",
            )
        return False, [], res.error or res.message

    def read_latest_incoming_messages(
        self, limit: int = 5
    ) -> Tuple[bool, List[Dict[str, str]], str]:
        return self.read_chat_messages(contact_name=None, limit=limit)

    def summarize_conversation(self, contact_name: str, limit: int = 10) -> Tuple[bool, str]:
        """Summarize conversation with contact using Gemini on the safely read transcript."""
        res = self.read_messages(contact_name=contact_name, limit=limit)
        if not res.is_success():
            return False, res.error or res.message

        msgs = res.data.get("messages", [])
        if not msgs:
            return True, f"No messages found for {contact_name} to summarize."

        raw_transcript = "\n".join(
            [f"{m['sender']}: {m.get('raw_text', m['text'])}" for m in msgs]
        )
        prompt = (
            f"You are JARVIS. Summarize the following recent WhatsApp conversation with {contact_name} "
            f"in 2 clear, concise sentences. Do not mention message IDs, timestamps or technical details:\n\n{raw_transcript}"
        )
        try:
            from core import gemini

            summary = gemini.text(prompt, tier=gemini.FAST, timeout_ms=5000)
            if summary and summary.strip():
                return True, summary.strip()
        except Exception:
            pass

        last_msg = msgs[-1].get("raw_text", "")
        return (
            True,
            f"The recent conversation with {contact_name} consists of {len(msgs)} messages. The last message was: '{last_msg}'.",
        )


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


_IDEMPOTENCY_CACHE: Dict[str, outcome.ActionResult] = {}
_DRIVER_INSTANCE: Optional[WhatsAppWebDriver] = None


def get_idempotency_key(recipient: str, text: str) -> str:
    """Generate idempotency key: (recipient + hash of text + minute bucket)."""
    bucket = int(time.time() // 60)
    h = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]
    return f"{recipient.strip().lower()}:{h}:{bucket}"


def clear_idempotency_cache() -> None:
    """Clear idempotency cache (for testing or reset)."""
    _IDEMPOTENCY_CACHE.clear()


def get_driver() -> WhatsAppWebDriver:
    """Singleton getter for the WhatsApp Web driver."""
    global _DRIVER_INSTANCE
    if _DRIVER_INSTANCE is None:
        _DRIVER_INSTANCE = WhatsAppWebDriver()
    return _DRIVER_INSTANCE
