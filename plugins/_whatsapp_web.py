"""
plugins/_whatsapp_web.py — Unified WhatsApp Web Driver over Chrome DevTools Protocol (CDP).

Exclusively attaches to the dedicated Jarvis Chrome profile via BrowserSessionManager.
Never launches a second browser, never touches user's personal default profile,
never uses pyautogui/clipboard/desktop UIA/window focus stealing.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core import outcome
from memory import config_manager, memory_manager

logger = logging.getLogger("whatsapp_web")


from plugins._whatsapp_selectors import WhatsAppSelectors, SELECTORS


async def _safe_call(obj, method_name: str, *args, **kwargs) -> Any:
    """Safely call a method on obj whether it returns an awaitable, a normal value, or is a mock."""
    if obj is None:
        return None
    fn = getattr(obj, method_name, None)
    if fn is None:
        return None
    try:
        res = fn(*args, **kwargs) if callable(fn) else fn
        if inspect.isawaitable(res):
            return await res
        return res
    except Exception as e:
        logger.debug("[WhatsAppWeb] _safe_call(%s.%s) failed: %s", type(obj).__name__, method_name, e)
        return None


def save_contact_alias(alias: str, identity: dict) -> None:
    """Remember user's contact/group disambiguation choice so ambiguity is not asked again."""
    try:
        key = (alias or "").strip().lower()
        if not key:
            return
        if isinstance(identity, dict):
            clean_identity = {
                "display_name": identity.get("display_name", ""),
                "kind": identity.get("kind", "chat"),
                "section": identity.get("section", "Chats"),
                "subtitle": identity.get("subtitle", ""),
            }
            val_str = json.dumps(clean_identity)
        else:
            val_str = str(identity)
        memory_manager.update_memory({"whatsapp_aliases": {key: {"value": val_str}}})
        logger.info("[WhatsAppWeb] Saved contact alias '%s' -> %s", key, val_str)
    except Exception as e:
        logger.warning("[WhatsAppWeb] Failed to save contact alias: %s", e)


def get_contact_alias(alias: str) -> Optional[dict]:
    """Retrieve previously remembered contact or group choice."""
    try:
        key = (alias or "").strip().lower()
        if not key:
            return None
        data = memory_manager.load_memory()
        aliases = data.get("whatsapp_aliases", {})
        if not isinstance(aliases, dict):
            return None
        entry = aliases.get(key)
        if isinstance(entry, dict):
            raw = entry.get("value", "")
            try:
                return json.loads(raw)
            except Exception:
                return {"display_name": raw}
        elif isinstance(entry, str):
            try:
                return json.loads(entry)
            except Exception:
                return {"display_name": entry}
        return None
    except Exception as e:
        logger.warning("[WhatsAppWeb] Failed to load contact alias: %s", e)
        return None


def rank_and_disambiguate_entries(
    entries: List[Dict[str, Any]],
    clean_name: str,
    target_name: str,
    is_group_requested: bool = False,
    is_person_requested: bool = False,
) -> Tuple[str, Any, Optional[str]]:
    """Rank search entries and disambiguate between chats, contacts, and groups.

    Returns:
        (verdict, value, message):
        - ("chosen", chosen_entry_dict, None)
        - ("ask", list_of_candidate_entries, spoken_prompt)
        - ("failed", None, error_message)
    """
    if not entries:
        return ("failed", None, f"Contact '{clean_name}' not found on WhatsApp Web.")

    # Exclude message snippet rows (never merge sections or pick message body preview)
    valid_entries = [e for e in entries if e.get("kind") != "message"]
    if not valid_entries:
        valid_entries = entries

    t_low = target_name.strip().lower()
    c_low = clean_name.strip().lower()

    # Exact matches vs partial matches
    exact_matches = [
        e
        for e in valid_entries
        if e.get("display_name", "").strip().lower() == t_low
        or e.get("display_name", "").strip().lower() == c_low
    ]
    partial_matches = [
        e
        for e in valid_entries
        if t_low in e.get("display_name", "").strip().lower()
        or e.get("display_name", "").strip().lower() in t_low
    ]

    pool = exact_matches if exact_matches else partial_matches
    if not pool:
        # Fuzzy spelling variant matching (e.g. "Yashwanth" vs "Yaswanth")
        import difflib

        def _fuzzy_score(name_a: str, name_b: str) -> float:
            a = name_a.strip().lower()
            b = name_b.strip().lower()
            if not a or not b:
                return 0.0
            direct = difflib.SequenceMatcher(None, a, b).ratio()
            tokens_a = a.split()
            tokens_b = b.split()
            max_token = max(
                (difflib.SequenceMatcher(None, ta, tb).ratio() for ta in tokens_a for tb in tokens_b),
                default=0.0,
            )
            return max(direct, max_token)

        scored_entries = []
        for e in valid_entries:
            disp = e.get("display_name", "")
            s = max(_fuzzy_score(disp, t_low), _fuzzy_score(disp, c_low))
            if s >= 0.70:
                scored_entries.append((s, e))

        if scored_entries:
            scored_entries.sort(key=lambda x: x[0], reverse=True)
            top_score = scored_entries[0][0]
            # If top score is clear winner (score >= 0.75 and >= 0.10 higher than second or only one)
            if len(scored_entries) == 1 or (top_score >= 0.75 and (top_score - scored_entries[1][0] >= 0.10)):
                pool = [scored_entries[0][1]]
                logger.info(
                    "[WhatsAppWeb] Fuzzy matched spelling variant '%s' -> '%s' (score=%.2f)",
                    clean_name,
                    scored_entries[0][1].get("display_name"),
                    top_score,
                )
            else:
                # Ambiguous: multiple candidates with close scores -> ask user
                pool = [e for s, e in scored_entries if (top_score - s) < 0.15]
        else:
            return ("failed", None, f"Contact '{clean_name}' not found on WhatsApp Web.")

    # Filter by explicit kind requirement (Part 3.2)
    if is_group_requested:
        pool = [e for e in pool if e.get("kind") == "group"]
        if not pool:
            return ("failed", None, f"No matching group found for '{clean_name}' on WhatsApp Web.")
    elif is_person_requested:
        pool = [e for e in pool if e.get("kind") in ("chat", "contact")]
        if not pool:
            return ("failed", None, f"No matching contact or chat found for '{clean_name}' on WhatsApp Web.")

    # Check remembered alias in memory
    saved_alias = get_contact_alias(target_name) or get_contact_alias(clean_name)
    if saved_alias and isinstance(saved_alias, dict):
        saved_disp = str(saved_alias.get("display_name") or "").strip().lower()
        saved_kind = str(saved_alias.get("kind") or "").strip().lower()
        for e in pool:
            if e.get("display_name", "").strip().lower() == saved_disp:
                if not saved_kind or e.get("kind") == saved_kind:
                    logger.info(
                        "[WhatsAppWeb] Resolved '%s' via remembered alias: %s (%s)",
                        clean_name,
                        saved_disp,
                        saved_kind,
                    )
                    return ("chosen", e, None)

    # Kind ranking: exact-name 1:1 chat > contact with that name > group
    kind_priority = {"chat": 0, "contact": 1, "group": 2}

    def _sort_key(e):
        disp = e.get("display_name", "").strip().lower()
        is_exact = 0 if (disp == t_low or disp == c_low) else 1
        k_prio = kind_priority.get(e.get("kind", "chat"), 3)
        row_idx = e.get("row_index", 0)
        return (is_exact, k_prio, row_idx)

    pool.sort(key=_sort_key)

    if len(pool) == 1:
        return ("chosen", pool[0], None)

    # Disambiguation:
    # If more than one plausible entry remains (for example a chat and a group with the same name):
    all_contacts = all(c.get("kind") in ("contact", "chat") for c in pool)
    kind_word = "contacts" if all_contacts else "matches"
    numbered_lines = []
    for idx, c in enumerate(pool[:4], 1):
        sub = f" ({c['subtitle']})" if c.get("subtitle") else ""
        numbered_lines.append(f"{idx}) {c.get('kind', 'chat')} '{c.get('display_name', '')}'{sub}")
    spoken_list = ", ".join(numbered_lines)
    prompt_msg = f"Found multiple {kind_word} matching '{clean_name}': {spoken_list}. Which one would you like to open?"

    return ("ask", pool[:4], prompt_msg)


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

    async def _inspect_chat_header(self, page) -> Optional[dict]:
        """Inspect the active conversation header in #main.

        Returns dict with title, subtitle, kind ('chat' or 'group'), or None if not open.
        """
        try:
            header_el = await page.query_selector(SELECTORS.chat_header)
            if not header_el:
                return None
            vis = await _safe_call(header_el, "is_visible")
            if vis is False:
                return None

            title_el = await _safe_call(header_el, "query_selector", SELECTORS.chat_header_title)
            if not title_el:
                title_el = await _safe_call(page, "query_selector", SELECTORS.chat_header_title)
            if not title_el:
                return None

            title_text = (await _safe_call(title_el, "inner_text")) or (await _safe_call(title_el, "get_attribute", "title")) or ""
            if isinstance(title_text, str):
                title_text = title_text.splitlines()[0].strip()
            else:
                title_text = ""
            if not title_text:
                return None

            subtitle_text = ""
            try:
                subtitle_el = await _safe_call(header_el, "query_selector", SELECTORS.chat_header_subtitle)
                if subtitle_el:
                    sub_raw = (await _safe_call(subtitle_el, "inner_text")) or (await _safe_call(subtitle_el, "get_attribute", "title")) or ""
                    if isinstance(sub_raw, str):
                        subtitle_text = sub_raw.strip()
            except Exception:
                pass

            raw_header_text = ""
            try:
                hdr_raw = await _safe_call(header_el, "inner_text")
                if isinstance(hdr_raw, str):
                    raw_header_text = hdr_raw
            except Exception:
                pass

            combined = (raw_header_text + " " + subtitle_text).lower()
            is_group = (
                "group info" in combined
                or "members" in combined
                or ("," in subtitle_text and len(subtitle_text.split(",")) > 1)
            )
            kind = "group" if is_group else "chat"

            return {
                "title": title_text,
                "subtitle": subtitle_text.strip(),
                "kind": kind,
                "header_el": header_el,
            }
        except Exception as e:
            logger.debug("[WhatsAppWeb] _inspect_chat_header check failed: %s", e)
            return None

    async def _parse_search_results_by_section(self, page) -> List[Dict[str, Any]]:
        """Parse search results into typed entries grouped by section:
        {section, display_name, kind: 'chat'|'contact'|'group'|'message', last_message_preview, subtitle, row_index, data_testid}
        """
        js_code = """
        () => {
            const pane = document.querySelector('#pane-side') || document.querySelector('#side');
            if (!pane) return [];
            const grid = pane.querySelector('div[role="grid"]') || pane;
            const entries = [];
            let currentSection = 'Chats';
            let rowIndex = 0;

            for (const child of grid.children) {
                const h2 = child.querySelector('h2') || (child.tagName === 'H2' ? child : null);
                if (h2) {
                    currentSection = (h2.innerText || '').trim();
                    continue;
                }

                const titleEl = child.querySelector('[data-testid="cell-frame-title"], span[title], span[dir="auto"]');
                if (!titleEl) continue;
                const title = (titleEl.getAttribute('title') || titleEl.innerText || '').trim();
                if (!title) continue;

                const previewEl = child.querySelector('span.matched-text, div[role="gridcell"] span[dir="ltr"], div[role="gridcell"] span[dir="auto"]:last-child');
                const preview = previewEl ? (previewEl.innerText || '').trim() : '';
                const fullText = (child.innerText || '');

                const secLower = currentSection.toLowerCase();
                let kind = 'chat';
                if (secLower.includes('contact')) {
                    kind = 'contact';
                } else if (secLower.includes('group') || fullText.includes('members') || fullText.includes('also in this group') || !!child.querySelector('[data-icon="default-group"]')) {
                    kind = 'group';
                } else if (secLower.includes('message')) {
                    kind = 'message';
                } else {
                    if (fullText.includes('members') || fullText.includes('also in this group') || !!child.querySelector('[data-icon="default-group"]')) {
                        kind = 'group';
                    } else {
                        kind = 'chat';
                    }
                }

                let subtitle = '';
                if (kind === 'group') {
                    const mMatch = fullText.match(/(\\d+\\s*members)/i);
                    if (mMatch) {
                        subtitle = mMatch[1];
                    } else if (fullText.includes('also in this group')) {
                        subtitle = 'group in common';
                    }
                } else if (kind === 'contact') {
                    const phoneMatch = fullText.match(/(\\+?\\d[\\d\\s-]{8,}\\d)/);
                    if (phoneMatch) {
                        subtitle = phoneMatch[1].trim();
                    }
                }

                entries.push({
                    section: currentSection,
                    display_name: title,
                    kind: kind,
                    last_message_preview: preview,
                    subtitle: subtitle,
                    row_index: rowIndex,
                    data_testid: child.getAttribute('data-testid') || ''
                });
                rowIndex++;
            }
            return entries;
        }
        """
        try:
            res = await page.evaluate(js_code)
            if isinstance(res, list):
                return res
        except Exception as e:
            logger.debug("[WhatsAppWeb] DOM section parsing fallback: %s", e)
        return []

    async def _resolve_contact_in_page(
        self, page, contact_name: str, context_hint: str = ""
    ) -> outcome.ActionResult:
        """Resolve and open a chat for contact_name in WhatsApp Web.

        1. If already open in active header: verify title and kind, return done without searching.
        2. If stored phone number found: direct navigate to web.whatsapp.com/send?phone=...
        3. Search box typing: parse typed results into distinct sections (Chats, Contacts, Groups).
        4. Disambiguation: rank exact chat > contact > group; return status 'ask' if ambiguous.
        5. Verify opened chat header matches chosen name and kind.
        """
        clean_name = (contact_name or "").strip()
        if not clean_name:
            return outcome.result_failed("resolve_contact", "No contact name or phone number specified.")

        # Determine target name and requested kind (group vs person/contact)
        target_name = clean_name
        is_group_requested = (
            any(w in clean_name.lower().split() for w in ("group", "grp"))
            or "group" in (context_hint or "").lower()
        )
        is_person_requested = (
            any(w in clean_name.lower().split() for w in ("contact", "person", "chat"))
            or any(k in (context_hint or "").lower() for k in ("contact", "person", "chat"))
        )

        # Strip keyword prefixes/suffixes for clean search
        for prefix in ("group ", "contact ", "chat with ", "chat "):
            if target_name.lower().startswith(prefix):
                target_name = target_name[len(prefix) :].strip()
        for suffix in (" group", " contact"):
            if target_name.lower().endswith(suffix):
                target_name = target_name[: -len(suffix)].strip()

        # 1. Strict Check: Is the intended chat already open in the active header?
        # Decided strictly from reading the active conversation header, never guessed!
        open_header = await self._inspect_chat_header(page)
        if open_header:
            open_title = open_header.get("title", "")
            open_kind = open_header.get("kind", "chat")
            t_low = target_name.lower()
            ot_low = open_title.lower()
            if t_low == ot_low or (len(t_low) >= 3 and (t_low in ot_low or ot_low in t_low)):
                kind_ok = True
                if is_group_requested and open_kind != "group":
                    kind_ok = False
                elif is_person_requested and open_kind == "group":
                    kind_ok = False

                if kind_ok:
                    logger.info("[WhatsAppWeb] Chat '%s' (%s) is already open in active header.", open_title, open_kind)
                    return outcome.result_done(
                        "resolve_contact",
                        f"Chat for '{open_title}' is already open.",
                        verified=True,
                        data={
                            "contact": open_title,
                            "expected": clean_name,
                            "kind": open_kind,
                            "already_open": True,
                            "method": "header_already_open",
                        },
                    )

        # 2. Prefer stored phone number for known contacts (direct phone URL)
        phone = find_contact_phone_number(clean_name)
        if phone:
            logger.info("[WhatsAppWeb] Resolved phone for '%s': %s (direct URL)", clean_name, phone)
            target_url = f"https://web.whatsapp.com/send?phone={phone}"
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

            # Wait for either composer, chat header, or invalid phone dialog
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
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

                compose = await page.query_selector(SELECTORS.compose_box)
                if compose and await compose.is_visible():
                    return outcome.result_done(
                        "resolve_contact",
                        f"Opened chat directly for '{clean_name}' via phone {phone}.",
                        verified=True,
                        data={"contact": clean_name, "phone": phone, "method": "direct_phone"},
                    )
                await asyncio.sleep(0.4)

            compose = await page.query_selector(SELECTORS.compose_box)
            if compose and await compose.is_visible():
                return outcome.result_done(
                    "resolve_contact",
                    f"Opened chat for '{clean_name}'.",
                    verified=True,
                    data={"contact": clean_name, "phone": phone},
                )
            logger.info("[WhatsAppWeb] Direct phone URL timed out; falling back to search box.")

        # 3. Fall back to search box
        search_el = await _safe_call(page, "wait_for_selector", SELECTORS.search_input, timeout=12000)
        if not search_el:
            return outcome.result_failed("resolve_contact", "WhatsApp Web layout changed: Search input box not found.")

        # Focus and clear search box via CDP
        await _safe_call(search_el, "click")
        if hasattr(page, "keyboard") and page.keyboard:
            await _safe_call(page.keyboard, "press", "Control+A")
            await _safe_call(page.keyboard, "press", "Backspace")
            await asyncio.sleep(0.1)
            await _safe_call(page.keyboard, "type", target_name)
        await asyncio.sleep(1.2)  # Wait for list to filter

        # 4. Parse search results by section
        section_entries = await self._parse_search_results_by_section(page)

        # 5. If DOM evaluate didn't return entries (e.g. offline mock or virtual list fallback),
        # inspect DOM elements directly
        if not section_entries:
            items = await _safe_call(page, "query_selector_all", SELECTORS.search_result_item) or []
            for idx, it in enumerate(items):
                try:
                    vis = await _safe_call(it, "is_visible")
                    if vis is False:
                        continue
                    title_span = await _safe_call(it, "query_selector", 'span[dir="auto"], span[title]')
                    title = ""
                    if title_span:
                        title = (await _safe_call(title_span, "inner_text")) or (await _safe_call(title_span, "get_attribute", "title")) or ""
                    if isinstance(title, str):
                        title = title.strip()
                    else:
                        title = ""
                    if not title:
                        continue

                    full_text = await _safe_call(it, "inner_text")
                    full_text = full_text if isinstance(full_text, str) else ""

                    group_icon = await _safe_call(it, "query_selector", '[data-icon="default-group"]')
                    has_group_icon = group_icon is not None and group_icon is not title_span and group_icon is not it
                    is_grp = (
                        has_group_icon
                        or "members" in full_text.lower()
                        or "also in this group" in full_text.lower()
                    )
                    kind = "group" if is_grp else "chat"
                    subtitle = ""
                    if is_grp:
                        m = re.search(r"(\d+\s*members)", full_text, re.IGNORECASE)
                        subtitle = m.group(1) if m else ""

                    section_entries.append({
                        "section": "Chats",
                        "display_name": title,
                        "kind": kind,
                        "subtitle": subtitle,
                        "row_index": idx,
                        "row_ref": it,
                        "data_testid": "",
                    })
                except Exception:
                    continue

        # Disambiguate and rank entries
        verdict, val, msg = rank_and_disambiguate_entries(
            section_entries,
            clean_name=clean_name,
            target_name=target_name,
            is_group_requested=is_group_requested,
            is_person_requested=is_person_requested,
        )

        if verdict == "ask":
            candidates = [c["display_name"] for c in val]
            return outcome.result_ask(
                "resolve_contact",
                msg,
                data={
                    "candidates": candidates,
                    "candidate_entries": val,
                    "expected": clean_name,
                },
            )
        elif verdict == "failed":
            return outcome.result_failed(
                "resolve_contact",
                msg or f"Contact '{clean_name}' not found on WhatsApp Web.",
                data={"contact": clean_name},
            )

        chosen_entry = val
        # Remember user's choice so the same ambiguity is not asked again (Part 3.2)
        save_contact_alias(clean_name, chosen_entry)
        save_contact_alias(target_name, chosen_entry)

        # Click the identified contact / group row
        clicked = False
        if chosen_entry.get("row_ref"):
            try:
                await chosen_entry["row_ref"].click()
                clicked = True
            except Exception:
                pass

        if not clicked and chosen_entry.get("data_testid"):
            chosen_el = await page.query_selector(f'[data-testid="{chosen_entry["data_testid"]}"]')
            if chosen_el:
                try:
                    cell = await chosen_el.evaluate_handle(
                        "el => el.closest('[data-testid=\"cell-frame-container\"], div[role=\"row\"], div[role=\"listitem\"]') || el"
                    )
                    await cell.click()
                    clicked = True
                except Exception:
                    await chosen_el.click()
                    clicked = True

        if not clicked:
            rows = await page.query_selector_all(SELECTORS.search_result_item)
            idx = chosen_entry.get("row_index", 0)
            if 0 <= idx < len(rows):
                try:
                    await rows[idx].click()
                    clicked = True
                except Exception:
                    pass

        if not clicked:
            chat_item = await page.query_selector(f'//span[@title="{chosen_entry["display_name"]}"]')
            if chat_item:
                await chat_item.click()
                clicked = True

        await asyncio.sleep(1.2)

        # Strict Header Verification: verify opened chat header title and kind match chosen entry
        header_info = await self._inspect_chat_header(page)
        if not header_info:
            header_el = await page.wait_for_selector(SELECTORS.chat_header_title, timeout=8000)
            if not header_el:
                return outcome.result_failed(
                    "resolve_contact",
                    "WhatsApp Web layout changed: Active chat header not found after selecting contact.",
                )
            header_text = ""
            if hasattr(header_el, "inner_text"):
                header_text = (await header_el.inner_text()) or ""
            if not header_text and hasattr(header_el, "get_attribute"):
                header_text = (await header_el.get_attribute("title")) or ""
            header_text = header_text.strip()
            header_kind = "chat"
        else:
            header_text = header_info.get("title", "")
            header_kind = header_info.get("kind", "chat")

        # Check matching with normalization (ignoring spaces/punctuation) and fuzzy spelling variants
        t_norm = re.sub(r"[^a-z0-9]", "", target_name.lower())
        h_norm = re.sub(r"[^a-z0-9]", "", header_text.lower())
        from difflib import SequenceMatcher
        sim = SequenceMatcher(None, t_norm, h_norm).ratio()
        name_matches = (
            target_name.lower() in header_text.lower()
            or header_text.lower() in target_name.lower()
            or (t_norm and t_norm in h_norm)
            or (h_norm and h_norm in t_norm)
            or sim >= 0.70
        )
        if not name_matches:
            return outcome.result_failed(
                "resolve_contact",
                f"Opened chat header '{header_text}' does not match intended contact '{clean_name}'. Refusing to proceed.",
                data={"opened_header": header_text, "expected_contact": clean_name},
            )

        if chosen_entry.get("kind") == "group" and header_kind != "group":
            return outcome.result_failed(
                "resolve_contact",
                f"Opened chat '{header_text}' is a 1:1 chat, but requested target was a group.",
                data={"opened_header": header_text, "expected_kind": "group"},
            )

        if chosen_entry.get("kind") in ("chat", "contact") and header_kind == "group" and not is_group_requested:
            return outcome.result_failed(
                "resolve_contact",
                f"Opened chat '{header_text}' is a group, but requested target was a 1:1 contact.",
                data={"opened_header": header_text, "expected_kind": "chat"},
            )

        return outcome.result_done(
            "resolve_contact",
            f"Verified active chat for '{header_text}'.",
            verified=True,
            data={
                "contact": header_text,
                "kind": header_kind,
                "expected": clean_name,
                "method": "search_section_verified",
            },
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
            if res_contact.data.get("verdict") == "wrong_target" or "does not match" in res_contact.message.lower():
                return outcome.result_failed(
                    "whatsapp_send",
                    f"Wrong chat open: {res_contact.message}",
                    data={"verdict": "wrong_target", "contact": contact_name},
                )
            return res_contact

        # Verify active chat header matches target contact
        header_info = await self._inspect_chat_header(page)
        if header_info:
            open_title = header_info.get("title", "").strip().lower()
            c_low = contact_name.strip().lower()
            t_norm = re.sub(r"[^a-z0-9]", "", c_low)
            h_norm = re.sub(r"[^a-z0-9]", "", open_title)
            from difflib import SequenceMatcher
            sim = SequenceMatcher(None, t_norm, h_norm).ratio()
            name_matches = (
                c_low in open_title
                or open_title in c_low
                or (t_norm and t_norm in h_norm)
                or (h_norm and h_norm in t_norm)
                or sim >= 0.70
            )
            if not name_matches:
                return outcome.result_failed(
                    "whatsapp_send",
                    f"Wrong chat open: expected '{contact_name}', but active chat is '{header_info.get('title')}'.",
                    data={"verdict": "wrong_target", "expected": contact_name, "found": header_info.get("title")},
                )

        # 2. Wait for message composer to be visible
        compose_el = await page.wait_for_selector(SELECTORS.compose_box, timeout=10000)
        if not compose_el:
            return outcome.result_failed(
                "whatsapp_send",
                "WhatsApp Web layout changed: Message composer input box not found.",
            )

        # Record pre-action baseline (outgoing bubbles present BEFORE sending)
        pre_bubbles = await _safe_call(page, "query_selector_all", SELECTORS.message_outgoing) or []
        pre_count = len(pre_bubbles)
        pre_ids = set()
        for b in pre_bubbles:
            mid = await _safe_call(b, "get_attribute", "data-id")
            if mid:
                pre_ids.add(mid)

        await _safe_call(compose_el, "click")
        await asyncio.sleep(0.1)

        # 3. Type message via CDP Input (if not already drafted in composer)
        existing_text = ""
        try:
            existing_text = await page.evaluate(
                "() => { const el = document.querySelector('[data-testid=\"conversation-compose-box-input\"], footer [contenteditable=\"true\"], footer [role=\"textbox\"]'); return el ? (el.innerText || el.textContent || '') : ''; }"
            )
        except Exception:
            pass

        if not existing_text or message.strip() not in existing_text:
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

        # 5. Post-Action Verification: ONLY a NEW outgoing bubble counts (older bubbles never count!)
        deadline = time.monotonic() + 10.0
        new_bubble = None
        tick_seen = None

        while time.monotonic() < deadline:
            post_bubbles = await _safe_call(page, "query_selector_all", SELECTORS.message_outgoing) or []
            post_count = len(post_bubbles)

            cand = None
            if post_count > pre_count and post_bubbles:
                cand = post_bubbles[-1]
            elif post_bubbles:
                cand_id = await _safe_call(post_bubbles[-1], "get_attribute", "data-id")
                if cand_id and cand_id not in pre_ids:
                    cand = post_bubbles[-1]

            if cand is not None:
                cand_text_el = (await _safe_call(cand, "query_selector", SELECTORS.message_text)) or cand
                cand_text = (await _safe_call(cand_text_el, "inner_text")) or ""
                clean_msg = message.strip().lower()
                clean_cand = cand_text.strip().lower()
                if not clean_msg or clean_msg in clean_cand or clean_cand in clean_msg:
                    new_bubble = cand

                    sent_tick = await _safe_call(cand, "query_selector", SELECTORS.tick_sent_single)
                    dbl_tick = (await _safe_call(cand, "query_selector", SELECTORS.tick_delivered_double)) or (
                        await _safe_call(cand, "query_selector", SELECTORS.tick_read_blue)
                    )
                    pending_clock = await _safe_call(cand, "query_selector", SELECTORS.tick_pending_clock)

                    if dbl_tick and (await _safe_call(dbl_tick, "is_visible")) is not False:
                        tick_seen = "delivered"
                        break
                    elif sent_tick and (await _safe_call(sent_tick, "is_visible")) is not False:
                        tick_seen = "sent"
                        break
                    elif pending_clock and (await _safe_call(pending_clock, "is_visible")) is not False:
                        tick_seen = "pending"
                        break

            await asyncio.sleep(0.4)

        msg_preview = f'"{message}"'
        if new_bubble and tick_seen in ("sent", "delivered"):
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
                    "verdict": "verified",
                    "tick": tick_seen,
                },
            )
        elif new_bubble and tick_seen == "pending":
            return outcome.result_unverified(
                "whatsapp_send",
                "Message queued locally in WhatsApp Web (clock icon), waiting for phone connection.",
                error="Message pending in local queue (clock icon)",
                data={"contact": contact_name, "text": message, "to": contact_name, "verdict": "unverifiable", "tick": "clock"},
            )
        elif new_bubble:
            return outcome.result_unverified(
                "whatsapp_send",
                f"Message was typed for {contact_name}, but delivery tick could not be confirmed.",
                error="Sent tick unconfirmed",
                data={"contact": contact_name, "text": message, "to": contact_name, "verdict": "unverifiable"},
            )

        return outcome.result_failed(
            "whatsapp_send",
            f"Message was not sent to {contact_name}: no new outgoing bubble appeared in chat.",
            error="No new outgoing message bubble detected after send",
            data={"contact": contact_name, "text": message, "to": contact_name, "verdict": "not_done"},
        )

    async def _prepare_draft_in_page(
        self, page, contact_name: str, message: str
    ) -> outcome.ActionResult:
        """Search contact, open chat, and type message into composer without sending."""
        res_contact = await self._resolve_contact_in_page(page, contact_name)
        if not res_contact.is_success():
            return res_contact

        header_info = await self._inspect_chat_header(page)
        open_title = header_info.get("title", "").strip() if header_info else contact_name

        compose_el = await _safe_call(page, "wait_for_selector", SELECTORS.compose_box, timeout=10000)
        if not compose_el:
            return outcome.result_failed(
                "whatsapp_send",
                "WhatsApp Web layout changed: Message composer input box not found.",
            )

        await _safe_call(compose_el, "click")
        await asyncio.sleep(0.1)

        bm = self._get_browser_manager()
        try:
            cdp = await bm._get_cdp_session()
            if cdp:
                await cdp.send("Input.insertText", {"text": message})
            else:
                if hasattr(page, "keyboard") and page.keyboard:
                    await _safe_call(page.keyboard, "type", message)
        except Exception:
            if hasattr(page, "keyboard") and page.keyboard:
                await _safe_call(page.keyboard, "type", message)

        await asyncio.sleep(0.2)
        return outcome.result_done(
            "prepare_draft",
            f"Drafted message for '{open_title}'.",
            verified=True,
            data={
                "open_contact": open_title,
                "composer_text": message,
                "contact": contact_name,
            },
        )

    def prepare_draft(
        self,
        contact_name: str,
        message: str,
        background: bool = True,
    ) -> outcome.ActionResult:
        """Synchronously resolve contact, open chat, and type draft into composer without sending."""
        state, page, reason = self.ensure_page_ready(background=background)
        if state != "ready" or page is None:
            if state == "blocked":
                return outcome.result_blocked("whatsapp_send", reason)
            return outcome.result_failed("whatsapp_send", reason)

        bm = self._get_browser_manager()
        try:
            return bm.run_coroutine(
                self._prepare_draft_in_page(page, contact_name, message),
                timeout=40.0,
            )
        except Exception as e:
            return outcome.result_failed("whatsapp_send", f"Error drafting WhatsApp message: {e}")

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
        """Extract latest messages with DOM direction, grouped bubble author propagation, links, and untrusted wrapping."""
        if contact_name:
            res_contact = await self._resolve_contact_in_page(page, contact_name)
            if not res_contact.is_success():
                return res_contact

        await asyncio.sleep(0.4)

        eff_limit = max(1, min(int(limit or 5), 50))

        # Check scroll container if more messages requested than visible
        try:
            bubbles = await _safe_call(page, "query_selector_all", SELECTORS.message_container) or []
            if len(bubbles) < eff_limit:
                scroll_container = await _safe_call(
                    page,
                    "query_selector",
                    '#main div[data-testid="conversation-panel-messages"], #main div[role="application"]',
                )
                if scroll_container:
                    for _ in range(min(4, (eff_limit - len(bubbles)) // 5 + 1)):
                        await _safe_call(page, "evaluate", "(el) => { if (el) el.scrollTop = 0; }", scroll_container)
                        await asyncio.sleep(0.3)
                        bubbles = await _safe_call(page, "query_selector_all", SELECTORS.message_container) or []
                        if len(bubbles) >= eff_limit:
                            break
        except Exception:
            bubbles = await _safe_call(page, "query_selector_all", SELECTORS.message_container) or []

        if not bubbles:
            return outcome.result_done(
                "whatsapp_read",
                f"No messages found for {contact_name or 'the current chat'}.",
                verified=True,
                data={"contact": contact_name, "count": 0, "messages": [], "links": []},
            )

        items = bubbles[-eff_limit:]
        extracted_messages: List[Dict[str, Any]] = []
        last_incoming_author = contact_name or "Contact"

        for b in items:
            try:
                class_attr = (await _safe_call(b, "get_attribute", "class")) or ""
                # Skip date separators or system bubbles
                if "system-message" in class_attr:
                    continue

                # DOM Direction Detection (never guessed from pixels)
                tail_out = await _safe_call(b, "query_selector", '[data-icon="tail-out"], [data-testid="tail-out"]')
                tail_in = await _safe_call(b, "query_selector", '[data-icon="tail-in"], [data-testid="tail-in"]')
                out_tick = await _safe_call(
                    b,
                    "query_selector",
                    '[data-icon="msg-check"], [data-icon="msg-dblcheck"], [data-icon="msg-dblcheck-ack"], [data-icon="status-time"]',
                )

                is_outgoing = ("message-out" in class_attr) or (tail_out is not None) or (out_tick is not None)
                direction = "outgoing" if is_outgoing else "incoming"

                # Sender & Timestamp
                pre_plain = (await _safe_call(b, "get_attribute", "data-pre-plain-text")) or ""
                time_val = "Recent"
                sender = "You" if is_outgoing else last_incoming_author

                if pre_plain:
                    m_time = re.search(r"\[(.*?)\]", pre_plain)
                    if m_time:
                        time_val = m_time.group(1).strip()
                    m_author = re.search(r"\]\s*(.*?):", pre_plain)
                    if m_author and not is_outgoing:
                        author_str = m_author.group(1).strip()
                        if author_str:
                            sender = author_str
                            last_incoming_author = author_str

                if not is_outgoing and sender == "Contact":
                    author_el = await _safe_call(b, "query_selector", '[data-testid="author"]')
                    if author_el:
                        a_text = (await _safe_call(author_el, "inner_text")) or ""
                        if a_text.strip():
                            sender = a_text.strip()
                            last_incoming_author = sender

                # Message Body
                text_el = await _safe_call(b, "query_selector", SELECTORS.message_text)
                if not text_el:
                    text_el = await _safe_call(b, "query_selector", "span.selectable-text, .copyable-text")
                if not text_el:
                    continue

                raw_body = (await _safe_call(text_el, "inner_text")) or ""
                if not isinstance(raw_body, str):
                    continue
                raw_body = raw_body.strip()
                if not raw_body:
                    continue

                # Ignore pure date separators (e.g. "TODAY", "YESTERDAY")
                if re.match(r"^(today|yesterday|\d{1,2}/\d{1,2}/\d{2,4})$", raw_body.lower()):
                    continue

                # Extract links (a[href])
                links: List[str] = []
                link_els = await _safe_call(b, "query_selector_all", "a[href], a") or []
                for l in link_els:
                    href = await _safe_call(l, "get_attribute", "href")
                    if href and href.startswith("http") and href not in links:
                        links.append(href)

                # Security Isolation: wrap all external text in untrusted markers
                safe_body = outcome.untrusted("whatsapp", raw_body)

                extracted_messages.append(
                    {
                        "sender": sender,
                        "direction": direction,
                        "timestamp": time_val,
                        "text": safe_body,
                        "raw_text": raw_body,
                        "links": links,
                    }
                )
            except Exception:
                continue

        all_links = []
        for m in extracted_messages:
            for lk in m.get("links", []):
                if lk not in all_links:
                    all_links.append(lk)

        if not extracted_messages:
            return outcome.result_done(
                "whatsapp_read",
                f"No messages found for {contact_name or 'the current chat'}.",
                verified=True,
                data={"contact": contact_name, "count": 0, "messages": [], "links": []},
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
                "links": all_links,
                "timestamp_range": ts_range,
            },
        )

    async def _list_unread_chats_in_page(self, page) -> outcome.ActionResult:
        """List chats with unread counts for 'who messaged me'."""
        js_code = """
        () => {
            const pane = document.querySelector('#pane-side') || document.querySelector('#side');
            if (!pane) return [];
            const rows = pane.querySelectorAll('div[role="row"], div[data-testid^="list-item-"], div[data-testid="cell-frame-container"]');
            const unread = [];
            for (const r of rows) {
                const badge = r.querySelector('[aria-label*="unread"], [data-testid="icon-unread-count"], span[aria-label*="no leído"]');
                if (!badge) continue;
                const badgeText = (badge.innerText || badge.getAttribute('aria-label') || '').trim();
                const countMatch = badgeText.match(/(\\d+)/);
                const count = countMatch ? parseInt(countMatch[1], 10) : 1;

                const titleEl = r.querySelector('span[title], [data-testid="cell-frame-title"], span[dir="auto"]');
                const title = (titleEl ? (titleEl.getAttribute('title') || titleEl.innerText) : '').trim();
                if (!title) continue;

                const previewEl = r.querySelector('div[role="gridcell"] span[dir="ltr"], div[role="gridcell"] span[dir="auto"]:last-child');
                const preview = previewEl ? (previewEl.innerText || '').trim() : '';

                unread.push({
                    contact: title,
                    unread_count: count,
                    last_message: preview
                });
            }
            return unread;
        }
        """
        raw_unread = await _safe_call(page, "evaluate", js_code)
        if not isinstance(raw_unread, list):
            raw_unread = []

        if not raw_unread:
            badges = await _safe_call(page, "query_selector_all", '[aria-label*="unread"], span[data-testid="icon-unread-count"]') or []
            for b in badges:
                try:
                    txt = (await _safe_call(b, "inner_text")) or (await _safe_call(b, "get_attribute", "aria-label")) or ""
                    m = re.search(r"(\d+)", txt)
                    count = int(m.group(1)) if m else 1
                    parent_row = await _safe_call(b, "evaluate_handle", "el => el.closest('[role=\"row\"], [data-testid^=\"list-item-\"]')")
                    title = ""
                    if parent_row:
                        title_el = await _safe_call(parent_row, "query_selector", 'span[title], span[dir="auto"]')
                        if title_el:
                            title = (await _safe_call(title_el, "get_attribute", "title")) or (await _safe_call(title_el, "inner_text")) or ""
                    title = title.strip() if isinstance(title, str) else ""
                    if title and not any(u["contact"] == title for u in raw_unread):
                        raw_unread.append({"contact": title, "unread_count": count, "last_message": ""})
                except Exception:
                    continue

        if not raw_unread:
            return outcome.result_done(
                "whatsapp_read",
                "You have no unread messages on WhatsApp Web.",
                verified=True,
                data={"unread_chats": [], "total_unread": 0},
            )

        lines = [f"{idx}) {u['contact']} ({u['unread_count']} unread)" for idx, u in enumerate(raw_unread, 1)]
        spoken = f"You have unread messages in {len(raw_unread)} chat{'s' if len(raw_unread) != 1 else ''}: " + ", ".join(lines)
        return outcome.result_done(
            "whatsapp_read",
            spoken,
            verified=True,
            data={"unread_chats": raw_unread, "total_unread": sum(u.get("unread_count", 1) for u in raw_unread)},
        )

    def list_unread_chats(self, background: bool = True) -> outcome.ActionResult:
        """Synchronously list chats with unread messages."""
        state, page, reason = self.ensure_page_ready(background=background)
        if state != "ready" or page is None:
            if state == "blocked":
                return outcome.result_blocked("whatsapp_read", reason)
            return outcome.result_failed("whatsapp_read", reason)

        bm = self._get_browser_manager()
        try:
            return bm.run_coroutine(self._list_unread_chats_in_page(page), timeout=25.0)
        except Exception as e:
            return outcome.result_failed("whatsapp_read", f"Error listing unread chats: {e}")

    async def _open_links_in_tabs(self, bm, links: List[str]) -> outcome.ActionResult:
        """Open extracted links in separate tabs, reporting load or failure status.
        Never claim a link is 'unresolved' without trying!
        """
        if not links:
            return outcome.result_failed("open_links", "No links provided to open.")

        results: List[Dict[str, Any]] = []
        for url in links:
            url_str = (url or "").strip()
            if not url_str:
                continue
            try:
                if hasattr(bm, "create_page"):
                    tab = await bm.create_page()
                    await tab.goto(url_str, wait_until="domcontentloaded", timeout=20000)
                    t = await _safe_call(tab, "title")
                    title = t if isinstance(t, str) else url_str
                elif hasattr(bm, "_open_tab"):
                    tab_info = await bm._open_tab(url_str)
                    title = getattr(tab_info, "title", url_str)
                elif hasattr(bm, "_context") and bm._context:
                    tab = await bm._context.new_page()
                    await tab.goto(url_str, wait_until="domcontentloaded", timeout=20000)
                    title = await tab.title()
                else:
                    tab_info = bm.open_tab(url_str)
                    title = getattr(tab_info, "title", url_str)

                results.append({"url": url_str, "status": "loaded", "title": title, "error": None})
            except Exception as e:
                err_msg = str(e)
                results.append({"url": url_str, "status": "failed", "title": "", "error": err_msg})

        loaded = [r for r in results if r["status"] == "loaded"]
        failed = [r for r in results if r["status"] == "failed"]

        spoken_lines = [
            f"Opened {len(results)} link{'s' if len(results) != 1 else ''} in separate tabs: {len(loaded)} loaded, {len(failed)} failed."
        ]
        for r in results:
            if r["status"] == "loaded":
                spoken_lines.append(f"- {r['url']}: loaded ({r['title']})")
            else:
                spoken_lines.append(f"- {r['url']}: failed ({r['error']})")

        return outcome.result_done(
            "open_links",
            "\n".join(spoken_lines),
            verified=True,
            data={
                "results": results,
                "total": len(results),
                "loaded_count": len(loaded),
                "failed_count": len(failed),
            },
        )

    def open_links(self, links: List[str]) -> outcome.ActionResult:
        """Synchronously open links in separate tabs."""
        bm = self._get_browser_manager()
        try:
            return bm.run_coroutine(self._open_links_in_tabs(bm, links), timeout=45.0)
        except Exception as e:
            return outcome.result_failed("open_links", f"Error opening links: {e}")

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
