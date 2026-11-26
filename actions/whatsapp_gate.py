"""
actions/whatsapp_gate.py — Send-Time Confirmation Gate & Truthful Delivery Verification for WhatsApp Web.

Core Responsibilities:
1. is_whatsapp_send_action(action, params, page_state):
   Returns True ONLY when an action would actually submit a message on web.whatsapp.com:
   - click on Send button (role/aria-label/title/text indicates send or send icon)
   - press_key Enter/Return (without Shift) while the message composer is focused
   - type with press_enter=True into the composer (or while composer is focused)
   Allows freely:
   - typing into the contact search box
   - opening a chat (clicking a chat/contact row)
   - closing a chat / clearing search (Escape, back, clicking away)
   - typing or pasting a draft message into the composer (press_enter=False)
   - Shift+Enter (newline in composer)
   - scrolling, observing, navigating

2. Truthful & Informative Confirmation:
   - Reads current chat contact name and composer text directly from DOM.
   - Verifies open chat matches intended contact; shows a clear warning if it differs.
   - Prompt format: "Send to <contact>: '<message text>'? (yes/no)".
   - Only proceeds on explicit user confirmation (voice or UI); on 'no', leaves draft in composer.

3. Verified Send Outcome:
   - Inspects DOM after send to verify the message appears as the newest outgoing bubble with a sent tick.
   - Never claims success from tool call execution alone.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from core import confirm, outcome
from plugins._whatsapp_selectors import SELECTORS

logger = logging.getLogger("whatsapp_gate")


# ── Domain Guard Helper ───────────────────────────────────────────────────────

def is_whatsapp_url(url: Optional[str]) -> bool:
    """True if URL belongs to WhatsApp Web."""
    if not url:
        return False
    return "web.whatsapp.com" in str(url).lower()


# ── DOM Heuristic Matchers ────────────────────────────────────────────────────

_SEND_BUTTON_REGEX = re.compile(r"\b(send|enviar|bhejo)\b", re.IGNORECASE)


def is_send_button_text(text: str) -> bool:
    """True if text/label indicates a Send submission button."""
    t = str(text or "").strip().lower()
    if not t:
        return False
    if t in ("send", "enviar", "bhejo", "send message", "send button"):
        return True
    if any(k in t for k in ('data-icon="send"', 'data-testid="send"', 'aria-label="send"')):
        return True
    return bool(_SEND_BUTTON_REGEX.search(t))


def is_send_button_element(elem: Any) -> bool:
    """Check if a distilled or DOM element represents WhatsApp's Send button."""
    if elem is None:
        return False
    if isinstance(elem, dict):
        label = str(elem.get("aria_label") or elem.get("label") or "").lower()
        title = str(elem.get("title", "")).lower()
        text = str(elem.get("text", "")).lower()
        name = str(elem.get("name", "")).lower()
        data_icon = str(elem.get("data_icon", "")).lower()
        data_testid = str(elem.get("data_testid", "")).lower()
        role = str(elem.get("role", "")).lower()
    else:
        label = str(getattr(elem, "aria_label", "") or "").lower()
        title = str(getattr(elem, "title", "") or "").lower()
        text = str(getattr(elem, "text", "") or "").lower()
        name = str(getattr(elem, "name", "") or "").lower()
        data_icon = str(getattr(elem, "data_icon", "") or "").lower()
        data_testid = str(getattr(elem, "data_testid", "") or "").lower()
        role = str(getattr(elem, "role", "") or "").lower()

    if data_icon == "send" or data_testid == "send":
        return True
    combined = f"{label} {title} {text} {name}".strip()
    return is_send_button_text(combined)


def is_composer_element(elem: Any) -> bool:
    """Check if an element is the WhatsApp message composer."""
    if elem is None:
        return False
    if isinstance(elem, dict):
        label = str(elem.get("aria_label") or elem.get("label") or "").lower()
        ph = str(elem.get("placeholder", "")).lower()
        testid = str(elem.get("data_testid", "")).lower()
        text = str(elem.get("text", "")).lower()
        role = str(elem.get("role", "")).lower()
        tag = str(elem.get("tag", "")).lower()
    else:
        label = str(getattr(elem, "aria_label", "") or "").lower()
        ph = str(getattr(elem, "placeholder", "") or "").lower()
        testid = str(getattr(elem, "data_testid", "") or "").lower()
        text = str(getattr(elem, "text", "") or "").lower()
        role = str(getattr(elem, "role", "") or "").lower()
        tag = str(getattr(elem, "tag", "") or "").lower()

    if "compose-box" in testid or "conversation-compose-box-input" in testid:
        return True
    combined = f"{label} {ph} {text}".strip()
    # Explicitly rule out search box
    if any(k in combined for k in ("search", "buscar", "start a new chat", "chat-list-search")):
        return False
    return any(k in combined for k in ("type a message", "escribe un mensaje", "message", "compose")) or role == "textbox" or tag == "footer"


def is_search_box_element(elem: Any) -> bool:
    """Check if an element is the contact/chat search box."""
    if elem is None:
        return False
    if isinstance(elem, dict):
        label = str(elem.get("aria_label") or elem.get("label") or "").lower()
        ph = str(elem.get("placeholder", "")).lower()
        testid = str(elem.get("data_testid", "")).lower()
        text = str(elem.get("text", "")).lower()
    else:
        label = str(getattr(elem, "aria_label", "") or "").lower()
        ph = str(getattr(elem, "placeholder", "") or "").lower()
        testid = str(getattr(elem, "data_testid", "") or "").lower()
        text = str(getattr(elem, "text", "") or "").lower()

    if "chat-list-search" in testid:
        return True
    combined = f"{label} {ph} {text}".strip()
    return any(k in combined for k in ("search", "buscar", "start a new chat", "search input"))


# ── The Gate Helper: is_whatsapp_send_action ──────────────────────────────────

def is_whatsapp_send_action(action: str, params: Optional[dict] = None, page_state: Any = None) -> bool:
    """
    Authoritative helper: returns True if an action would submit/send a message on WhatsApp Web.

    Checks:
    1. Scope: Only active for web.whatsapp.com. Unrelated sites return False.
    2. Click on Send: Element role/aria-label/title/text indicates Send, or send icon.
    3. Enter in Composer: Key is Enter/Return (without Shift) while the message composer is focused.
    4. Type with Enter in Composer: type action with press_enter=True targeting the composer.
    """
    act = str(action or "").strip().lower()
    p = dict(params or {})

    # 1. Scope Guard: Check active site URL
    url = ""
    if isinstance(page_state, str):
        url = page_state
    elif isinstance(page_state, dict):
        url = str(page_state.get("url") or page_state.get("active_url") or "")
    elif hasattr(page_state, "url"):
        u = getattr(page_state, "url")
        url = u() if callable(u) else str(u or "")

    if not url:
        # Fall back to live browser active page if available
        try:
            from actions.browser_control import _get_active_url
            url = _get_active_url() or ""
        except Exception:
            url = ""

    # If URL is known and NOT WhatsApp Web, allow without WhatsApp gate
    if url and not is_whatsapp_url(url):
        return False

    # If page_state is explicitly a non-WhatsApp dict/url and no active URL was found
    if isinstance(page_state, dict) and "url" in page_state and not is_whatsapp_url(page_state["url"]):
        return False

    # 2. Click on Send button
    if act in ("click", "click_element", "smart_click", "hint_click"):
        # Target by text / description / selector
        target_text = str(
            p.get("text")
            or p.get("description")
            or p.get("aria_label")
            or p.get("name")
            or p.get("selector")
            or ""
        ).strip().lower()

        if target_text and is_send_button_text(target_text):
            return True

        # Target by integer element index
        if "index" in p and p["index"] is not None:
            idx = int(p["index"])
            elem = None
            if isinstance(page_state, dict) and "elements" in page_state:
                elem = page_state["elements"].get(idx)
            elif hasattr(page_state, "get_element_by_index"):
                elem = page_state.get_element_by_index(idx)
            else:
                try:
                    from actions.dom_distiller import get_dom_distiller
                    elem = get_dom_distiller().get_element_by_index(idx)
                except Exception:
                    elem = None

            if not elem:
                try:
                    from actions.browser_session import get_browser_session_manager
                    mgr = get_browser_session_manager()
                    live_p = mgr.active_page
                    if live_p and not live_p.is_closed():
                        attrs = mgr.run_coroutine(
                            live_p.evaluate(
                                """(idx) => {
                                    const el = document.querySelector(`[data-jarvis-idx="${idx}"]`) ||
                                               document.querySelectorAll('button, [role="button"], span[data-icon]')[idx];
                                    if (!el) return null;
                                    return {
                                        aria_label: el.getAttribute('aria-label') || '',
                                        title: el.getAttribute('title') || '',
                                        data_icon: el.getAttribute('data-icon') || '',
                                        data_testid: el.getAttribute('data-testid') || '',
                                        role: el.getAttribute('role') || '',
                                        text: (el.textContent || '').trim()
                                    };
                                }""",
                                idx,
                            )
                        )
                        if attrs:
                            elem = attrs
                except Exception:
                    pass

            if elem and is_send_button_element(elem):
                return True

        return False

    # 3. Key Press (Enter/Return)
    if act in ("press", "press_key"):
        key = str(p.get("key", "")).strip().lower()
        if key in ("enter", "return", "\r", "\n"):
            # Shift+Enter is newline -> allowed freely!
            if p.get("shift") or str(p.get("modifiers", "")).lower() == "shift" or "shift" in key:
                return False

            # Check if composer is focused
            if isinstance(page_state, dict):
                if page_state.get("is_composer_focused") is True or page_state.get("composer_focused") is True:
                    return True
                focused_el = page_state.get("focused_element")
                if focused_el is not None:
                    if is_composer_element(focused_el):
                        return True
                    if is_search_box_element(focused_el):
                        return False

            # Check live page activeElement if available
            try:
                page = getattr(page_state, "active_page", page_state)
                if hasattr(page, "evaluate"):
                    # Check if document.activeElement matches compose box
                    is_active = page.evaluate(
                        "() => !!document.activeElement && ("
                        "document.activeElement.matches('[data-testid=\"conversation-compose-box-input\"], footer [contenteditable=\"true\"], footer [role=\"textbox\"]')"
                        ")"
                    )
                    if is_active:
                        return True
            except Exception:
                pass

        return False

    # 4. Typing with press_enter=True
    if act in ("type", "input", "input_text", "smart_type", "fill_form"):
        press_enter = bool(p.get("press_enter") or p.get("enter"))
        if not press_enter:
            # Typing or pasting into composer without pressing enter is freely allowed!
            return False

        # If press_enter is True, check if targeting composer
        target_field = str(p.get("selector", "") or p.get("description", "")).strip().lower()
        if target_field:
            if is_search_box_element({"label": target_field, "placeholder": target_field}):
                return False
            if is_composer_element({"label": target_field, "placeholder": target_field}):
                return True

        if "index" in p and p["index"] is not None:
            idx = int(p["index"])
            elem = None
            if isinstance(page_state, dict) and "elements" in page_state:
                elem = page_state["elements"].get(idx)
            elif hasattr(page_state, "get_element_by_index"):
                elem = page_state.get_element_by_index(idx)
            else:
                try:
                    from actions.dom_distiller import get_dom_distiller
                    elem = get_dom_distiller().get_element_by_index(idx)
                except Exception:
                    elem = None

            if not elem:
                try:
                    from actions.browser_session import get_browser_session_manager
                    mgr = get_browser_session_manager()
                    live_p = mgr.active_page
                    if live_p and not live_p.is_closed():
                        attrs = mgr.run_coroutine(
                            live_p.evaluate(
                                """(idx) => {
                                    const el = document.querySelector(`[data-jarvis-idx="${idx}"]`) ||
                                               document.querySelectorAll('input, [contenteditable="true"], [role="textbox"]')[idx];
                                    if (!el) return null;
                                    return {
                                        aria_label: el.getAttribute('aria-label') || '',
                                        placeholder: el.getAttribute('placeholder') || '',
                                        data_testid: el.getAttribute('data-testid') || '',
                                        role: el.getAttribute('role') || '',
                                        tag: el.tagName ? el.tagName.toLowerCase() : ''
                                    };
                                }""",
                                idx,
                            )
                        )
                        if attrs:
                            elem = attrs
                except Exception:
                    pass

            if elem:
                if is_search_box_element(elem):
                    return False
                if is_composer_element(elem):
                    return True

        # Check if composer is focused
        if isinstance(page_state, dict):
            if page_state.get("is_composer_focused") is True or page_state.get("composer_focused") is True:
                return True
            focused_el = page_state.get("focused_element")
            if focused_el is not None:
                if is_composer_element(focused_el):
                    return True
                if is_search_box_element(focused_el):
                    return False

        # If no specific element given and live page has composer focused
        try:
            page = getattr(page_state, "active_page", page_state)
            if hasattr(page, "evaluate"):
                is_active = page.evaluate(
                    "() => !!document.activeElement && ("
                    "document.activeElement.matches('[data-testid=\"conversation-compose-box-input\"], footer [contenteditable=\"true\"], footer [role=\"textbox\"]')"
                    ")"
                )
                if is_active:
                    return True
        except Exception:
            pass

        return False

    return False


# ── DOM Perception Helpers for Truthful Confirmations ─────────────────────────

def get_active_chat_contact(page=None) -> Optional[str]:
    """Read the active conversation header title directly from WhatsApp Web DOM."""
    try:
        if page is None:
            from actions.browser_session import get_browser_session_manager
            session_mgr = get_browser_session_manager()
            page = session_mgr.active_page
            if not page or page.is_closed():
                return None
            res = session_mgr.run_coroutine(_get_active_chat_contact_async(page))
            return res
        return asyncio.run(_get_active_chat_contact_async(page))
    except Exception as e:
        logger.debug("[WhatsAppGate] get_active_chat_contact failed: %s", e)
        return None


async def _get_active_chat_contact_async(page) -> Optional[str]:
    if not page or page.is_closed():
        return None
    try:
        title_el = await page.query_selector(SELECTORS.chat_header_title)
        if title_el:
            text = (await title_el.inner_text()) or (await title_el.get_attribute("title")) or ""
            if text:
                return text.splitlines()[0].strip()
    except Exception:
        pass
    return None


def get_active_composer_text(page=None) -> Optional[str]:
    """Read the message text currently drafted in the WhatsApp Web composer box."""
    try:
        if page is None:
            from actions.browser_session import get_browser_session_manager
            session_mgr = get_browser_session_manager()
            page = session_mgr.active_page
            if not page or page.is_closed():
                return None
            return session_mgr.run_coroutine(_get_active_composer_text_async(page))
        return asyncio.run(_get_active_composer_text_async(page))
    except Exception as e:
        logger.debug("[WhatsAppGate] get_active_composer_text failed: %s", e)
        return None


async def _get_active_composer_text_async(page) -> Optional[str]:
    if not page or page.is_closed():
        return None
    try:
        compose_el = await page.query_selector(SELECTORS.compose_box)
        if compose_el:
            text = (await compose_el.inner_text()) or ""
            return text.strip()
    except Exception:
        pass
    return None


def format_confirmation_prompt(
    open_contact: Optional[str],
    composer_text: str,
    intended_contact: Optional[str] = None,
) -> str:
    """Format an informative and truthful confirmation prompt with recipient verification."""
    clean_msg = composer_text.strip() if composer_text else "(empty message)"
    target_contact = open_contact.strip() if open_contact else "active chat"

    warning = ""
    if intended_contact and open_contact:
        c_low = intended_contact.strip().lower()
        o_low = open_contact.strip().lower()
        if c_low not in o_low and o_low not in c_low:
            warning = f"⚠️ Warning: Active chat '{open_contact}' does not match intended recipient '{intended_contact}'! "

    if not open_contact:
        warning = "⚠️ Warning: No active chat detected. "

    return f"{warning}Send to {target_contact}: '{clean_msg}'? (yes/no)"


# ── Delivery Verification (Fix False-Success Problem) ─────────────────────────

async def verify_whatsapp_send_async(
    page,
    expected_text: str,
    pre_ids: Optional[Set[str]] = None,
    timeout: float = 8.0,
) -> Tuple[bool, str, dict]:
    """
    Authoritative post-send DOM verification:
    Checks if a NEW outgoing message bubble containing expected_text appeared with a delivery tick.
    Returns: (is_sent: bool, message: str, data: dict)
    """
    if not page or page.is_closed():
        return False, "Cannot verify message: browser page is closed.", {"verdict": "unverifiable"}

    pre_ids = pre_ids or set()
    deadline = time.monotonic() + timeout
    clean_expected = (expected_text or "").strip().lower()
    new_bubble = None
    tick_seen = None

    while time.monotonic() < deadline:
        try:
            bubbles = await page.query_selector_all(SELECTORS.message_outgoing)
            for b in reversed(bubbles):
                b_id = (await b.get_attribute("data-id")) or ""
                if b_id and b_id in pre_ids:
                    continue  # Old bubble from before send!

                # Inspect message text
                text_el = (await b.query_selector(SELECTORS.message_text)) or b
                cand_text = ((await text_el.inner_text()) or "").strip().lower()

                if not clean_expected or clean_expected in cand_text or cand_text in clean_expected:
                    new_bubble = b
                    # Check ticks
                    dbl_tick = await b.query_selector(SELECTORS.tick_delivered_double)
                    if not dbl_tick:
                        dbl_tick = await b.query_selector(SELECTORS.tick_read_blue)
                    sent_tick = await b.query_selector(SELECTORS.tick_sent_single)
                    pending_clock = await b.query_selector(SELECTORS.tick_pending_clock)

                    if dbl_tick and (await dbl_tick.is_visible()) is not False:
                        tick_seen = "delivered"
                        break
                    elif sent_tick and (await sent_tick.is_visible()) is not False:
                        tick_seen = "sent"
                        break
                    elif pending_clock and (await pending_clock.is_visible()) is not False:
                        tick_seen = "pending"
                        break

            if tick_seen in ("sent", "delivered"):
                break
        except Exception as e:
            logger.debug("[WhatsAppGate] Verification check tick error: %s", e)

        await asyncio.sleep(0.4)

    if new_bubble and tick_seen in ("sent", "delivered"):
        return True, f"I have sent the message: \"{expected_text}\"", {
            "verified": True,
            "verdict": "verified",
            "tick": tick_seen,
        }
    elif new_bubble and tick_seen == "pending":
        return False, "Message queued locally in WhatsApp Web (clock icon), waiting for phone connection.", {
            "verified": False,
            "verdict": "unverifiable",
            "tick": "pending",
        }
    elif new_bubble:
        return False, f"Message was typed, but delivery could not be confirmed.", {
            "verified": False,
            "verdict": "unverifiable",
            "tick": "unconfirmed",
        }

    return False, "Message was not sent: no new outgoing bubble appeared in chat.", {
        "verified": False,
        "verdict": "not_done",
    }
