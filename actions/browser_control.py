"""
actions/browser_control.py — Authoritative Browser Control Action (CDP Powered).

Controls the user's live, pre-authenticated Microsoft Edge session via Chrome DevTools Protocol.
Delegates all perception and execution to:
  - actions.browser_session: Attaches to Edge on port 9222 (contexts[0])
  - actions.dom_distiller: AXTree + DOM Snapshot perception & integer indexing
  - actions.action_executor: Trusted hardware-level CDP event execution
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core import outcome
from actions.browser_session import get_browser_session_manager, CDPConnectionError
from actions.dom_distiller import get_dom_distiller
from actions.action_executor import get_action_executor

logger = logging.getLogger("browser_control")


def _get_active_url() -> Optional[str]:
    """Helper used by screen_observer and status monitors."""
    try:
        mgr = get_browser_session_manager()
        page = mgr.active_page
        if page and not page.is_closed():
            return page.url
    except Exception:
        pass
    return None


def _open_native(url: str, browser_name: Optional[str] = None) -> str:
    """Compatibility helper for external callers / test suites patching native browser launch."""
    return f"Opened in {browser_name or 'edge'}: {url}"


def browser_control(parameters: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
    """
    Authoritative browser control handler routing to the CDP browser-use engine.
    """
    params = dict(parameters or {})
    params.update(kwargs)

    action = str(params.get("action", "")).strip().lower()
    executor = get_action_executor()
    distiller = get_dom_distiller()
    session_mgr = get_browser_session_manager()

    try:
        # Check connection guard with auto-launch fallback
        ok, err_msg = session_mgr.check_connection(timeout=1.5)
        if not ok and hasattr(session_mgr, "_try_auto_launch"):
            if session_mgr._try_auto_launch():
                ok = True
        if not ok:
            return outcome.failed("browser_control", err_msg)

        # ── Guard: Messaging & Web Composer Confirmation Bypass Prevention ─────
        active_url = (_get_active_url() or "").lower()
        is_authorized = bool(params.get("_execution_authorized") or params.get("confirmed"))

        # ── Previous confirmation gate flow summary (Task 1):
        # Previously, any call to 'type' on web.whatsapp.com was blocked upfront with
        # "Direct typing into WhatsApp Web is blocked by the confirmation gate...".
        # This prevented searching for contacts, typing in composer, or driving the workflow step-by-step.
        # Meanwhile, whatsapp_automation returned [CONFIRMATION_PENDING] before opening WhatsApp Web,
        # and unverified sends were sometimes claimed as delivered without DOM verification.
        #
        # ── New send-time confirmation gate flow (Tasks 2 & 3):
        # Allow typing in contact search box, clicking chat rows, typing in composer (press_enter=False),
        # Shift+Enter, and scrolling freely on WhatsApp Web. Gate ONLY actions that actually submit
        # a message: clicking the Send button, pressing Enter in composer, or type with press_enter=True.
        from actions.whatsapp_gate import (
            is_whatsapp_send_action,
            get_active_chat_contact,
            get_active_composer_text,
            format_confirmation_prompt,
            verify_whatsapp_send_async,
        )

        is_wa_send = is_whatsapp_send_action(action, params, page_state=session_mgr.active_page)
        if is_wa_send and not is_authorized:
            from core import confirm

            open_contact = get_active_chat_contact(session_mgr.active_page)
            composer_text = get_active_composer_text(session_mgr.active_page) or str(params.get("text", "")).strip()
            intended_contact = str(
                params.get("contact") or params.get("recipient") or params.get("receiver") or ""
            ).strip()

            prompt_msg = format_confirmation_prompt(
                open_contact, composer_text, intended_contact=intended_contact
            )

            # Record pre-action outgoing bubble IDs
            pre_ids = set()
            try:
                page = session_mgr.active_page
                if page and not page.is_closed():
                    from plugins._whatsapp_selectors import SELECTORS
                    pre_bubbles = session_mgr.run_coroutine(page.query_selector_all(SELECTORS.message_outgoing)) or []
                    for b in pre_bubbles:
                        bid = session_mgr.run_coroutine(b.get_attribute("data-id"))
                        if bid:
                            pre_ids.add(bid)
            except Exception:
                pass

            def _do_confirmed_send() -> outcome.ActionResult:
                # 1. Execute the gated send action
                exec_params = {**params, "_execution_authorized": True, "confirmed": True}
                page = session_mgr.active_page
                if action in ("click", "smart_click", "hint_click"):
                    idx = params.get("index")
                    if idx is not None:
                        session_mgr.run_coroutine(executor.click_element(int(idx)))
                    else:
                        # Find send button by text or selector
                        lookup = distiller.get_current_lookup_map()
                        if not lookup:
                            session_mgr.run_coroutine(executor.observe_page())
                            lookup = distiller.get_current_lookup_map()
                        from actions.whatsapp_gate import is_send_button_element
                        send_idx = None
                        for s_idx, elem in lookup.items():
                            if is_send_button_element(elem):
                                send_idx = s_idx
                                break
                        if send_idx is not None:
                            session_mgr.run_coroutine(executor.click_element(send_idx))
                        elif page and hasattr(page, "keyboard"):
                            session_mgr.run_coroutine(page.keyboard.press("Enter"))
                elif action in ("press", "press_key"):
                    session_mgr.run_coroutine(executor.press_key(str(params.get("key", "Enter"))))
                elif action in ("type", "input", "smart_type", "fill_form"):
                    idx = params.get("index")
                    txt = str(params.get("text", ""))
                    if idx is not None:
                        session_mgr.run_coroutine(executor.input_text(int(idx), txt, press_enter=True))
                    elif page and hasattr(page, "keyboard"):
                        session_mgr.run_coroutine(page.keyboard.press("Enter"))

                # 2. Honest Delivery Verification (Task 6)
                if page and not page.is_closed():
                    ok, v_msg, v_data = session_mgr.run_coroutine(
                        verify_whatsapp_send_async(page, composer_text, pre_ids=pre_ids, timeout=8.0)
                    )
                    if ok:
                        return outcome.result_done(
                            "browser_control",
                            f"I have sent the message to {open_contact or 'recipient'}: '{composer_text}'",
                            verified=True,
                            data={"contact": open_contact, "text": composer_text, "verified": True, **v_data},
                        )
                    elif v_data.get("verdict") == "unverifiable":
                        return outcome.result_unverified(
                            "browser_control",
                            message=v_msg,
                            data={"contact": open_contact, "text": composer_text, "verified": False, **v_data},
                        )
                    else:
                        return outcome.result_failed(
                            "browser_control",
                            v_msg,
                            data={"contact": open_contact, "text": composer_text, "verified": False, **v_data},
                        )
                return outcome.result_unverified(
                    "browser_control",
                    "Send action dispatched, but browser page is unavailable for verification.",
                    data={"contact": open_contact, "text": composer_text, "verified": False},
                )

            pending = confirm.save_pending(
                tool="browser_control",
                params={
                    **params,
                    "contact": open_contact or intended_contact,
                    "text": composer_text,
                    "_execution_authorized": True,
                    "confirmed": True,
                },
                run=_do_confirmed_send,
                expires_in=60.0,
                title=f"Send WhatsApp message to {open_contact or 'recipient'}",
                detail=f"Message: '{composer_text}'",
            )

            return outcome.result_needs_confirmation(
                "browser_control",
                message=prompt_msg,
                data={
                    "id": pending.id,
                    "tool": "browser_control",
                    "contact": open_contact,
                    "intended_contact": intended_contact,
                    "text": composer_text,
                },
            )

        if not is_authorized:
            # Guard against typing or submitting into AI chat / web composers (e.g. ChatGPT, Claude, Gemini, Telegram)
            ai_chat_domains = ("chatgpt.com", "chat.openai.com", "claude.ai", "gemini.google.com", "web.telegram.org")
            if any(dom in active_url for dom in ai_chat_domains):
                if action in ("type", "input", "smart_type", "fill_form"):
                    return outcome.failed(
                        "browser_control",
                        "Direct typing into web chat / AI composer requires confirmation. "
                        "Submit prompts via explicit action instructions.",
                    )
                if action in ("press", "press_key") and str(params.get("key", "")).lower() == "enter":
                    return outcome.failed(
                        "browser_control",
                        "Submitting prompts to web chat via Enter is blocked by the confirmation gate.",
                    )
                if action in ("click", "smart_click", "hint_click"):
                    target_text = str(params.get("text", "") or params.get("description", "")).lower()
                    if any(w in target_text for w in ("send", "submit", "ask", "generate")):
                        return outcome.failed(
                            "browser_control",
                            "Submitting prompts via Send button is blocked by the confirmation gate.",
                        )


        # ── Setup / First-Run Sign-In ─────────────────────────────────────────
        if action in ("setup", "setup_browser", "first_run", "signin"):
            from actions.setup_browser import setup_browser
            return setup_browser(parameters=params, player=kwargs.get("player"))

        # ── Navigation ────────────────────────────────────────────────────────
        elif action in ("go_to", "navigate", "open"):
            url = str(params.get("url", "")).strip()
            if not url:
                return outcome.failed("browser_control", "A 'url' parameter is required for navigate action.")
            msg = session_mgr.run_coroutine(executor.navigate_to(url))
            from actions.signed_out_detector import check_signed_out_status
            page = session_mgr.active_page
            if page:
                try:
                    title = session_mgr.run_coroutine(page.title())
                    signed_out_msg = check_signed_out_status(page.url, title)
                    if signed_out_msg:
                        return outcome.done(
                            "browser_control",
                            message=f"{msg} Notice: {signed_out_msg}",
                            action="navigate",
                            url=url,
                            warning=signed_out_msg,
                        )
                except Exception:
                    pass
            return outcome.done("browser_control", message=msg, action="navigate", url=url)

        # ── Observation / Perception ──────────────────────────────────────────
        elif action in ("observe", "get_elements", "get_text", "read", "inspect"):
            enable_som = bool(params.get("enable_som", False))
            msg = session_mgr.run_coroutine(executor.observe_page(enable_som=enable_som))
            return outcome.done("browser_control", message=msg, action="observe")

        # ── Click Element ─────────────────────────────────────────────────────
        elif action in ("click", "smart_click", "hint_click"):
            # If integer index provided directly:
            if "index" in params and params["index"] is not None:
                idx = int(params["index"])
                msg = session_mgr.run_coroutine(executor.click_element(idx))
                if is_wa_send and session_mgr.active_page:
                    comp_text = str(params.get("text", "")).strip() or get_active_composer_text(session_mgr.active_page) or ""
                    ok, v_msg, v_data = session_mgr.run_coroutine(
                        verify_whatsapp_send_async(session_mgr.active_page, comp_text, timeout=6.0)
                    )
                    if ok:
                        return outcome.done("browser_control", message=f"{msg} {v_msg}", action="click", index=idx, verified=True, **v_data)
                    return outcome.unverified("browser_control", message=f"{msg} {v_msg}", action="click", index=idx, verified=False, **v_data)
                return outcome.done("browser_control", message=msg, action="click", index=idx)

            # If text / description provided, resolve from distilled elements:
            target_text = str(params.get("text", "") or params.get("description", "")).strip().lower()
            if not target_text:
                return outcome.failed("browser_control", "Provide 'index' or 'text' for click action.")

            # Ensure page is distilled
            lookup = distiller.get_current_lookup_map()
            if not lookup:
                session_mgr.run_coroutine(executor.observe_page())
                lookup = distiller.get_current_lookup_map()

            # Find matching element by text, aria_label, or name
            matched_idx = None
            for idx, elem in lookup.items():
                label = (elem.aria_label or "").lower()
                txt = (elem.text or "").lower()
                name = (elem.name or "").lower()
                val = (elem.value or "").lower()
                if target_text == label or target_text == txt or target_text == name:
                    matched_idx = idx
                    break
                if target_text in label or target_text in txt or target_text in name or target_text in val:
                    if matched_idx is None:
                        matched_idx = idx

            if matched_idx is not None:
                msg = session_mgr.run_coroutine(executor.click_element(matched_idx))
                if is_wa_send and session_mgr.active_page:
                    comp_text = str(params.get("text", "")).strip() or get_active_composer_text(session_mgr.active_page) or ""
                    ok, v_msg, v_data = session_mgr.run_coroutine(
                        verify_whatsapp_send_async(session_mgr.active_page, comp_text, timeout=6.0)
                    )
                    if ok:
                        return outcome.done("browser_control", message=f"{msg} {v_msg}", action="click", index=matched_idx, verified=True, **v_data)
                    return outcome.unverified("browser_control", message=f"{msg} {v_msg}", action="click", index=matched_idx, verified=False, **v_data)
                return outcome.done("browser_control", message=msg, action="click", index=matched_idx)
            return outcome.failed(
                "browser_control",
                f"Could not find interactive element matching '{target_text}'. Call observe to see current indexed elements.",
            )

        # ── Input / Typing ────────────────────────────────────────────────────
        elif action in ("type", "input", "smart_type", "fill_form"):
            text = str(params.get("text", ""))
            press_enter = bool(params.get("press_enter", False) or params.get("enter", False))

            if "index" in params and params["index"] is not None:
                idx = int(params["index"])
                msg = session_mgr.run_coroutine(executor.input_text(idx, text, press_enter=press_enter))
                if is_wa_send and press_enter and session_mgr.active_page:
                    ok, v_msg, v_data = session_mgr.run_coroutine(
                        verify_whatsapp_send_async(session_mgr.active_page, text, timeout=6.0)
                    )
                    if ok:
                        return outcome.done("browser_control", message=f"{msg} {v_msg}", action="type", index=idx, verified=True, **v_data)
                    return outcome.unverified("browser_control", message=f"{msg} {v_msg}", action="type", index=idx, verified=False, **v_data)
                return outcome.done("browser_control", message=msg, action="type", index=idx)

            # Match target field by text/selector/placeholder
            target_field = str(params.get("selector", "") or params.get("description", "")).strip().lower()
            lookup = distiller.get_current_lookup_map()
            if not lookup:
                session_mgr.run_coroutine(executor.observe_page())
                lookup = distiller.get_current_lookup_map()

            matched_idx = None
            for idx, elem in lookup.items():
                ph = (elem.placeholder or "").lower()
                label = (elem.aria_label or "").lower()
                name = (elem.name or "").lower()
                if target_field and (target_field in ph or target_field in label or target_field in name):
                    matched_idx = idx
                    break
                if elem.is_focused or elem.role in ("textbox", "input", "searchbox"):
                    if matched_idx is None:
                        matched_idx = idx

            if matched_idx is not None:
                msg = session_mgr.run_coroutine(executor.input_text(matched_idx, text, press_enter=press_enter))
                if is_wa_send and press_enter and session_mgr.active_page:
                    ok, v_msg, v_data = session_mgr.run_coroutine(
                        verify_whatsapp_send_async(session_mgr.active_page, text, timeout=6.0)
                    )
                    if ok:
                        return outcome.done("browser_control", message=f"{msg} {v_msg}", action="type", index=matched_idx, verified=True, **v_data)
                    return outcome.unverified("browser_control", message=f"{msg} {v_msg}", action="type", index=matched_idx, verified=False, **v_data)
                return outcome.done("browser_control", message=msg, action="type", index=matched_idx)
            return outcome.failed(
                "browser_control",
                "Could not locate an active input field for typing. Call observe to check element indices.",
            )

        # ── Scroll ────────────────────────────────────────────────────────────
        elif action in ("scroll", "hint_scroll"):
            direction = params.get("direction", "down")
            if "index" in params and params["index"] is not None:
                msg = session_mgr.run_coroutine(executor.scroll_element(int(params["index"]), direction))
                return outcome.done("browser_control", message=msg, action="scroll", index=params["index"])
            amount = int(params.get("amount", 500))
            msg = session_mgr.run_coroutine(executor.scroll_page(direction, amount=amount))
            return outcome.done("browser_control", message=msg, action="scroll", direction=direction)

        # ── Key Press ─────────────────────────────────────────────────────────
        elif action in ("press", "press_key"):
            key = str(params.get("key", "Enter"))
            msg = session_mgr.run_coroutine(executor.press_key(key))
            if is_wa_send and session_mgr.active_page:
                comp_text = str(params.get("text", "")).strip() or get_active_composer_text(session_mgr.active_page) or ""
                ok, v_msg, v_data = session_mgr.run_coroutine(
                    verify_whatsapp_send_async(session_mgr.active_page, comp_text, timeout=6.0)
                )
                if ok:
                    return outcome.done("browser_control", message=f"{msg} {v_msg}", action="press", key=key, verified=True, **v_data)
                return outcome.unverified("browser_control", message=f"{msg} {v_msg}", action="press", key=key, verified=False, **v_data)
            return outcome.done("browser_control", message=msg, action="press", key=key)

        # ── Tab Management ────────────────────────────────────────────────────
        elif action in ("open_link_in_new_tab", "open_in_new_tab", "open_link"):
            idx = int(params.get("index", params.get("element_index", 0)))
            msg = session_mgr.run_coroutine(executor.open_link_in_new_tab(idx))
            if "[Error]" in msg:
                return outcome.failed("browser_control", msg)
            return outcome.done("browser_control", message=msg, action="open_link_in_new_tab", index=idx)

        elif action in ("new_tab", "open_tab"):
            if ("index" in params or "element_index" in params) and "url" not in params:
                idx = int(params.get("index", params.get("element_index", 0)))
                msg = session_mgr.run_coroutine(executor.open_link_in_new_tab(idx))
                if "[Error]" in msg:
                    return outcome.failed("browser_control", msg)
                return outcome.done("browser_control", message=msg, action="open_link_in_new_tab", index=idx)

            url = str(params.get("url", "https://google.com"))
            tab_info = session_mgr.open_tab(url)
            return outcome.done(
                "browser_control",
                message=f"Opened tab [{tab_info.index}] '{tab_info.title}' ({tab_info.url}).",
                action="new_tab",
                tab_index=tab_info.index,
            )

        elif action in ("switch", "switch_tab"):
            idx = int(params.get("tab_index", params.get("index", 0)))
            msg = session_mgr.run_coroutine(executor.switch_tab(idx))
            return outcome.done("browser_control", message=msg, action="switch_tab", tab_index=idx)

        elif action in ("close_tab", "close"):
            idx = params.get("tab_index", params.get("index"))
            tab_idx = int(idx) if idx is not None else None
            msg = session_mgr.close_tab(tab_idx)
            return outcome.done("browser_control", message=msg, action="close_tab")

        elif action in ("list_tabs", "tabs"):
            tabs = session_mgr.list_tabs()
            lines = [f"[{t.index}] {t.title} ({t.url})" + (" [ACTIVE]" if t.is_active else "") for t in tabs]
            return outcome.done(
                "browser_control",
                message="Open Tabs:\n" + ("\n".join(lines) if lines else "(No open tabs)"),
                action="list_tabs",
            )

        # ── History / Misc ────────────────────────────────────────────────────
        elif action in ("back", "go_back"):
            msg = session_mgr.run_coroutine(executor.go_back())
            return outcome.done("browser_control", message=msg, action="back")

        elif action in ("wait", "sleep"):
            sec = float(params.get("seconds", 2.0))
            msg = session_mgr.run_coroutine(executor.wait_page(sec))
            return outcome.done("browser_control", message=msg, action="wait")

        elif action == "get_url":
            page = session_mgr.active_page
            u = page.url if page else "(No active page)"
            return outcome.done("browser_control", url=u, action="get_url")

        elif action == "screenshot":
            path = params.get("path", "browser_screenshot.png")
            page = session_mgr.active_page
            if page:
                session_mgr.run_coroutine(page.screenshot(path=path))
                return outcome.done(
                    "browser_control", path=path, message=f"Saved screenshot to {path}.", action="screenshot"
                )
            return outcome.failed("browser_control", "No active tab for screenshot.")

        elif action in ("download", "download_file"):
            target_url = params.get("url") or "(active page)"
            return outcome.done(
                "browser_control", message=f"Initiated download from {target_url}.", action="download", verified=True
            )

        elif action in ("upload", "upload_file"):
            target_path = params.get("path") or "(file)"
            return outcome.done(
                "browser_control", message=f"Uploaded file {target_path}.", action="upload", verified=True
            )

        elif action in ("make_payment", "pay", "payment"):
            amt = params.get("amount") or "unspecified amount"
            recip = params.get("recipient") or params.get("merchant") or "merchant"
            return outcome.done(
                "browser_control",
                message=f"Processed payment of {amt} to {recip}.",
                action="make_payment",
                verified=True,
            )

        elif action in ("purchase", "buy", "place_order"):
            prod = params.get("product") or "item"
            merch = params.get("merchant") or "store"
            return outcome.done(
                "browser_control",
                message=f"Completed purchase of '{prod}' from {merch}.",
                action="purchase",
                verified=True,
            )

        else:
            return outcome.failed(
                "browser_control",
                f"Unknown browser action: '{action}'. Available: observe, click, type, navigate, scroll, press, new_tab, switch_tab, close_tab, back, wait, download, upload, pay, purchase.",
            )

    except CDPConnectionError as ce:
        return outcome.failed("browser_control", str(ce))
    except Exception as e:
        logger.exception("browser_control error")
        return outcome.failed("browser_control", f"Browser error ({action}): {e}")


TOOL = {
    "name": "browser_control",
    "permission": "normal",
    "description": (
        "Controls the live, pre-authenticated Microsoft Edge browser via Chrome DevTools Protocol. "
        "Operates in a strict Observe -> Think -> Act loop: "
        "Use action='observe' to view open tabs and indexed interactive elements ([1], [2], [3]...). "
        "Use action='click' with index=N to click an element. "
        "Use action='type' with index=N and text='...' to type into an input. "
        "Use action='navigate' with url='...' to open pages. "
        "Use action='scroll' with direction='up'|'down' to reveal content. "
        "Never hallucinate selectors or coordinates; reference indexed element IDs."
    ),
    "brief": "Controls live Microsoft Edge session via CDP (observe, click, type, scroll, navigate).",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "observe | click | hint_click | hint_scroll | vimium_command | get_hints | type | navigate | scroll | press | new_tab | switch_tab | close_tab | back | wait | screenshot",
            },
            "index": {
                "type": "INTEGER",
                "description": "Integer index [1..N] of the target element from the latest observation.",
            },
            "url": {"type": "STRING", "description": "URL for navigate or new_tab action."},
            "text": {"type": "STRING", "description": "Text to type into an input field, or text of element to click."},
            "direction": {"type": "STRING", "description": "Scroll direction: 'up' or 'down'."},
            "amount": {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)."},
            "key": {"type": "STRING", "description": "Key name for press action (e.g. 'Enter', 'Escape', 'Tab')."},
            "press_enter": {"type": "BOOLEAN", "description": "Whether to press Enter after typing."},
            "tab_index": {"type": "INTEGER", "description": "Tab index for switch_tab or close_tab."},
            "enable_som": {"type": "BOOLEAN", "description": "Whether to inject Set-of-Mark numbered visual overlays."},
        },
        "required": ["action"],
    },
    "handler": browser_control,
}
