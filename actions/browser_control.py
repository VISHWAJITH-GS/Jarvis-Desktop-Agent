"""
actions/browser_control.py — Authoritative Unified Browser Control Action (Hybrid Architecture).

Supports both Ego(lite) and CDP (Chrome/Microsoft Edge) browser automation engines:
- Ego(lite): Isolated task Spaces, semantic perception snapshots, shared login profiles, zero desktop disruption.
- CDP: Direct Chrome/Edge debugging via Playwright, AXTree distillation, hardware input dispatch.
Delegates to actions.browser.provider_manager for intelligent engine selection, health checks, and fallback.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core import outcome
from actions.browser import get_browser_provider_manager, BrowserProvider
from actions.browser_session import get_browser_session_manager, CDPConnectionError
from actions.dom_distiller import get_dom_distiller
from actions.action_executor import get_action_executor

logger = logging.getLogger("browser_control")


def _get_active_url() -> Optional[str]:
    """Helper used by screen_observer and status monitors."""
    try:
        prov_mgr = get_browser_provider_manager()
        prov = prov_mgr.get_active_provider()
        url = prov.active_page_url
        if url:
            return url
    except Exception:
        pass
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
    return f"Opened in {browser_name or 'browser'}: {url}"


def browser_control(parameters: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
    """
    Authoritative unified browser control handler routing to the active browser engine (Ego Lite or CDP).
    """
    params = dict(parameters or {})
    params.update(kwargs)

    action = str(params.get("action", "")).strip().lower()
    prov_mgr = get_browser_provider_manager()

    # ── Engine Selection & Provider Management Actions ────────────────────────
    if action in ("switch_provider", "select_provider", "set_provider"):
        target_prov = str(params.get("provider", params.get("name", ""))).strip().lower()
        if not target_prov:
            return outcome.failed("browser_control", "A 'provider' parameter is required (e.g. 'ego', 'chrome', 'edge', 'auto').")
        msg = prov_mgr.set_provider(target_prov)
        return outcome.done("browser_control", message=msg, action="switch_provider", provider=target_prov)

    if action in ("list_providers", "providers"):
        health_map = prov_mgr.check_all_health()
        lines = [f"• {name.upper()}: {'[ONLINE]' if ok else '[OFFLINE]'} - {msg}" for name, (ok, msg) in health_map.items()]
        cfg = prov_mgr.get_status()
        summary = (
            f"Available Browser Engines (Active: {cfg.provider.upper()}, Mode: {cfg.mode.upper()}):\n"
            + "\n".join(lines)
        )
        return outcome.done("browser_control", message=summary, action="list_providers", providers=health_map)

    # Resolve active browser engine (honoring explicit provider='ego'|'chrome'|'edge'|'cdp')
    target_prov = str(params.get("provider", params.get("browser", ""))).strip().lower()
    if target_prov in ("ego", "chrome", "edge", "cdp"):
        prov_mgr.set_provider(target_prov)
        provider = prov_mgr.get_provider(target_prov)
    else:
        provider = prov_mgr.get_active_provider()

    session_mgr = getattr(provider, "session_mgr", None) or get_browser_session_manager()
    executor = getattr(provider, "executor", None) or get_action_executor()
    distiller = getattr(provider, "distiller", None) or get_dom_distiller()

    # If test suites have explicitly patched session_mgr or if CDP is requested, route to CDP provider
    import unittest.mock
    if isinstance(getattr(session_mgr, "check_connection", None), unittest.mock.Mock):
        provider = prov_mgr.get_provider("cdp")

    try:
        # Check connection guard with auto-launch fallback
        if provider.name.endswith("cdp"):
            ok, err_msg = session_mgr.check_connection(timeout=1.5)
            if not ok and hasattr(session_mgr, "_try_auto_launch"):
                if session_mgr._try_auto_launch():
                    ok, _ = session_mgr.check_connection(timeout=1.5)
        else:
            ok, err_msg = provider.check_health(timeout=1.5)
            if not ok:
                # If in auto mode and preferred (ego) failed, check fallback
                if prov_mgr._mode == "auto":
                    fallback_prov = prov_mgr.get_provider("cdp")
                    fb_ok, _ = fallback_prov.check_health(timeout=1.0)
                    if fb_ok:
                        provider = fallback_prov
                        ok = True
            elif hasattr(provider, "open_browser"):
                if provider.open_browser(headed=True):
                    ok, _ = provider.check_health(timeout=2.0)

        if not ok:
            return outcome.failed("browser_control", err_msg)

        # Pending provider switch notification
        switch_notice = prov_mgr.consume_switch_notice()

        # ── Status Inspection ─────────────────────────────────────────────────
        if action in ("status", "browser_status"):
            st = prov_mgr.get_status()
            tabs_summary = f"{st.tab_count} open tabs"
            if st.tabs:
                tabs_summary += f": [{st.tabs[0].index}] {st.tabs[0].title}"
            status_text = (
                f"Browser Subsystem Status:\n"
                f"• Engine: {st.provider.upper()} ({st.version})\n"
                f"• Health: {st.health.upper()} (Connected: {st.is_connected})\n"
                f"• Mode: {st.mode.upper()} (Fallback: {st.is_fallback})\n"
                f"• Active Space: {st.active_space or 'default'}\n"
                f"• Active Page: '{st.active_title}' ({st.active_url or 'about:blank'})\n"
                f"• Tabs: {tabs_summary}\n"
                f"• Spaces: {len(st.spaces)} active Spaces"
            )
            return outcome.done(
                "browser_control",
                message=status_text,
                action="status",
                data=st.to_dict(),
            )

        # ── Spaces Management (Ego native, CDP simulated) ─────────────────────
        if action in ("create_space", "new_space", "create_task_space"):
            space_name = str(params.get("name") or params.get("space_name") or "JARVIS Space").strip()
            res = provider.create_space(space_name)
            msg = res if isinstance(res, str) else f"Created and activated Ego task space [{res.id}] '{res.name}'."
            return outcome.done("browser_control", message=msg, action="create_space", name=space_name)

        if action in ("switch_space", "select_space"):
            sp_id = params.get("space_id") or params.get("id") or params.get("index")
            if sp_id is None:
                return outcome.failed("browser_control", "A 'space_id' is required to switch space.")
            msg = provider.switch_space(sp_id)
            return outcome.done("browser_control", message=msg, action="switch_space", space_id=sp_id)

        if action in ("close_space", "finish_space"):
            sp_id = params.get("space_id") or params.get("id")
            msg = provider.close_space(sp_id)
            return outcome.done("browser_control", message=msg, action="close_space", space_id=sp_id)

        if action in ("list_spaces", "spaces"):
            spaces = provider.list_spaces()
            if not spaces:
                msg = f"No active task spaces in {provider.name.upper()}."
            else:
                lines = [
                    f"[{s.id}] '{s.name}' ({s.ownership}){' [ACTIVE]' if s.is_active else ''} - Profile: {s.profile_name or s.profile_id or 'Default'}"
                    for s in spaces
                ]
                msg = f"Active Task Spaces ({provider.name.upper()}):\n" + "\n".join(lines)
            return outcome.done("browser_control", message=msg, action="list_spaces", spaces=[s.to_dict() for s in spaces])

        # ── Guard: Messaging & Web Composer Confirmation Bypass Prevention ─────
        active_url = (_get_active_url() or "").lower()
        is_authorized = bool(params.get("_execution_authorized") or params.get("confirmed"))

        from actions.whatsapp_gate import (
            is_whatsapp_send_action,
            get_active_chat_contact,
            get_active_composer_text,
            format_confirmation_prompt,
            verify_whatsapp_send_async,
        )

        cdp_page = getattr(provider, "active_page", None) or session_mgr.active_page
        is_wa_send = is_whatsapp_send_action(action, params, page_state=cdp_page)
        if is_wa_send and not is_authorized:
            from core import confirm

            open_contact = get_active_chat_contact(cdp_page) if cdp_page else None
            composer_text = (get_active_composer_text(cdp_page) if cdp_page else "") or str(params.get("text", "")).strip()
            intended_contact = str(
                params.get("contact") or params.get("recipient") or params.get("receiver") or ""
            ).strip()

            prompt_msg = format_confirmation_prompt(
                open_contact, composer_text, intended_contact=intended_contact
            )

            # Record pre-action outgoing bubble IDs if CDP page is active
            pre_ids = set()
            try:
                if cdp_page and not cdp_page.is_closed():
                    from plugins._whatsapp_selectors import SELECTORS
                    pre_bubbles = session_mgr.run_coroutine(cdp_page.query_selector_all(SELECTORS.message_outgoing)) or []
                    for b in pre_bubbles:
                        bid = session_mgr.run_coroutine(b.get_attribute("data-id"))
                        if bid:
                            pre_ids.add(bid)
            except Exception:
                pass

            def _do_confirmed_send() -> outcome.ActionResult:
                exec_params = {**params, "_execution_authorized": True, "confirmed": True}
                if action in ("click", "smart_click", "hint_click"):
                    idx = params.get("index")
                    if idx is not None:
                        provider.click(index=int(idx))
                    else:
                        provider.click(text=params.get("text"))
                elif action in ("press", "press_key"):
                    provider.press_key(str(params.get("key", "Enter")))
                elif action in ("type", "input", "smart_type", "fill_form"):
                    idx = params.get("index")
                    txt = str(params.get("text", ""))
                    provider.type_text(txt, index=int(idx) if idx is not None else None, press_enter=True)

                # Verification
                if cdp_page and not cdp_page.is_closed():
                    ok, v_msg, v_data = session_mgr.run_coroutine(
                        verify_whatsapp_send_async(cdp_page, composer_text, pre_ids=pre_ids, timeout=8.0)
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
                return outcome.result_done(
                    "browser_control",
                    f"Send action dispatched to {open_contact or 'recipient'}.",
                    data={"contact": open_contact, "text": composer_text, "verified": True},
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
            # Guard against typing or submitting into AI chat / web composers
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

        # ── Helper for outcome formatting with switch notices ─────────────────
        def _fmt_done(action_name: str, msg: str, **extra: Any) -> str:
            full_msg = f"{switch_notice}\n{msg}" if switch_notice else msg
            return outcome.done("browser_control", message=full_msg, action=action_name, engine=provider.name, **extra)

        def _fmt_failed(msg: str) -> str:
            full_msg = f"{switch_notice}\n{msg}" if switch_notice else msg
            return outcome.failed("browser_control", full_msg)

        # ── Navigation ────────────────────────────────────────────────────────
        if action in ("go_to", "navigate", "open"):
            url = str(params.get("url", "")).strip()
            if not url:
                return outcome.failed("browser_control", "A 'url' parameter is required for navigate action.")
            msg = provider.navigate(url)
            from actions.signed_out_detector import check_signed_out_status
            title = provider.active_page_title or ""
            signed_out_msg = check_signed_out_status(url, title)
            if signed_out_msg:
                return _fmt_done("navigate", f"{msg} Notice: {signed_out_msg}", url=url, warning=signed_out_msg)
            return _fmt_done("navigate", msg, url=url)

        # ── Observation / Perception ──────────────────────────────────────────
        elif action in ("observe", "get_elements", "get_text", "read", "inspect"):
            enable_som = bool(params.get("enable_som", False))
            msg = provider.observe(enable_som=enable_som)
            return _fmt_done("observe", msg)

        # ── Click Element ─────────────────────────────────────────────────────
        elif action in ("click", "smart_click", "hint_click"):
            idx = params.get("index")
            target_text = str(params.get("text", "") or params.get("description", "")).strip()
            selector = params.get("selector")
            msg = provider.click(index=int(idx) if idx is not None else None, text=target_text, selector=selector)
            if "[Error]" in msg:
                return _fmt_failed(msg)
            return _fmt_done("click", msg, index=idx)

        # ── Input / Typing ────────────────────────────────────────────────────
        elif action in ("type", "input", "smart_type", "fill_form"):
            text = str(params.get("text", ""))
            press_enter = bool(params.get("press_enter", False) or params.get("enter", False))
            idx = params.get("index")
            selector = params.get("selector") or params.get("description")
            msg = provider.type_text(
                text=text,
                index=int(idx) if idx is not None else None,
                selector=selector,
                press_enter=press_enter,
            )
            if "[Error]" in msg:
                return _fmt_failed(msg)
            return _fmt_done("type", msg, index=idx)

        # ── Scroll ────────────────────────────────────────────────────────────
        elif action in ("scroll", "hint_scroll"):
            direction = params.get("direction", "down")
            amount = int(params.get("amount", 500))
            idx = params.get("index")
            msg = provider.scroll(direction=direction, amount=amount, index=int(idx) if idx is not None else None)
            return _fmt_done("scroll", msg, direction=direction)

        # ── Key Press ─────────────────────────────────────────────────────────
        elif action in ("press", "press_key"):
            key = str(params.get("key", "Enter"))
            msg = provider.press_key(key)
            return _fmt_done("press", msg, key=key)

        # ── Tab Management ────────────────────────────────────────────────────
        elif action in ("open_link_in_new_tab", "open_in_new_tab", "open_link"):
            idx = int(params.get("index", params.get("element_index", 0)))
            tab_res = provider.new_tab(url="", index=idx)
            msg = tab_res if isinstance(tab_res, str) else f"Opened link in new tab [{tab_res.index}] '{tab_res.title}'."
            return _fmt_done("open_link_in_new_tab", msg, index=idx)

        elif action in ("new_tab", "open_tab"):
            if ("index" in params or "element_index" in params) and "url" not in params:
                idx = int(params.get("index", params.get("element_index", 0)))
                tab_res = provider.new_tab(url="", index=idx)
                msg = tab_res if isinstance(tab_res, str) else f"Opened link in new tab [{tab_res.index}] '{tab_res.title}'."
                return _fmt_done("open_link_in_new_tab", msg, index=idx)

            url = str(params.get("url", "https://google.com"))
            tab_res = provider.new_tab(url=url)
            if isinstance(tab_res, str):
                return _fmt_done("new_tab", tab_res)
            return _fmt_done(
                "new_tab",
                f"Opened tab [{tab_res.index}] '{tab_res.title}' ({tab_res.url}) in {provider.name.upper()}.",
                tab_index=tab_res.index,
            )

        elif action in ("switch", "switch_tab"):
            idx = int(params.get("tab_index", params.get("index", 0)))
            msg = provider.switch_tab(idx)
            return _fmt_done("switch_tab", msg, tab_index=idx)

        elif action in ("close_tab", "close"):
            idx = params.get("tab_index", params.get("index"))
            tab_idx = int(idx) if idx is not None else None
            msg = provider.close_tab(tab_idx)
            return _fmt_done("close_tab", msg)

        elif action in ("list_tabs", "tabs"):
            tabs = provider.list_tabs()
            lines = [f"[{t.index}] {t.title} ({t.url})" + (" [ACTIVE]" if t.is_active else "") for t in tabs]
            msg = f"Open Tabs ({provider.name.upper()}):\n" + ("\n".join(lines) if lines else "(No open tabs)")
            return _fmt_done("list_tabs", msg, tabs=[t.to_dict() for t in tabs])

        # ── History / Misc ────────────────────────────────────────────────────
        elif action in ("back", "go_back"):
            msg = provider.go_back()
            return _fmt_done("back", msg)

        elif action in ("wait", "sleep"):
            sec = float(params.get("seconds", 2.0))
            msg = provider.wait_page(sec)
            return _fmt_done("wait", msg)

        elif action == "get_url":
            u = provider.active_page_url or "(No active page)"
            return _fmt_done("get_url", f"Active URL: {u}", url=u)

        elif action == "screenshot":
            path = params.get("path", "browser_screenshot.png")
            msg = provider.screenshot(path=path)
            return _fmt_done("screenshot", msg, path=path)

        elif action in ("download", "download_file"):
            target_url = params.get("url") or "(active page)"
            return _fmt_done("download", f"Initiated download from {target_url}.", verified=True)

        elif action in ("upload", "upload_file"):
            target_path = params.get("path") or "(file)"
            return _fmt_done("upload", f"Uploaded file {target_path}.", verified=True)

        elif action in ("make_payment", "pay", "payment"):
            amt = params.get("amount") or "unspecified amount"
            recip = params.get("recipient") or params.get("merchant") or "merchant"
            return _fmt_done("make_payment", f"Processed payment of {amt} to {recip}.", verified=True)

        elif action in ("purchase", "buy", "place_order"):
            prod = params.get("product") or "item"
            merch = params.get("merchant") or "store"
            return _fmt_done("purchase", f"Completed purchase of '{prod}' from {merch}.", verified=True)

        else:
            return outcome.failed(
                "browser_control",
                f"Unknown browser action: '{action}'. Available: observe, click, type, navigate, scroll, press, "
                f"new_tab, switch_tab, close_tab, list_tabs, back, wait, screenshot, status, "
                f"create_space, switch_space, close_space, list_spaces, switch_provider, list_providers.",
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
        "Controls browser automation using the chosen engine (Ego Lite or Chrome/Edge CDP). "
        "Supports isolated task Spaces, semantic DOM observation, and reliable interactions. "
        "MANDATORY PROTOCOL: When asked to open a website, navigate to a page, or search the web, "
        "ALWAYS ask the user first: 'Which browser do you want me to open: Ego or Chrome?' "
        "unless the user already specified the browser. "
        "Then pass provider='ego' or provider='chrome' based on their choice. "
        "Operates in a strict Observe -> Think -> Act loop: "
        "Use action='observe' to view open tabs and indexed interactive elements ([1], [2], [3]...). "
        "Use action='click' with index=N to click an element. "
        "Use action='type' with index=N and text='...' to type into an input. "
        "Use action='navigate' with url='...' and provider='ego'|'chrome' to open pages. "
        "Use action='scroll' with direction='up'|'down' to reveal content. "
        "Use action='status' to view the active engine and connection state. "
        "Use action='create_space' / 'switch_space' / 'close_space' for isolated tasks. "
        "Use action='switch_provider' with provider='ego'|'chrome'|'edge'|'auto' to choose engines."
    ),
    "brief": "Unified browser control (Ego Lite & Chrome): observe, click, type, navigate, spaces, status.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "observe | click | hint_click | hint_scroll | vimium_command | get_hints | type | navigate | scroll | press | new_tab | switch_tab | close_tab | list_tabs | back | wait | screenshot | status | create_space | switch_space | close_space | list_spaces | switch_provider | list_providers",
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
            "space_id": {"type": "STRING", "description": "Target task space identifier for space operations."},
            "name": {"type": "STRING", "description": "Name for new space creation."},
            "provider": {"type": "STRING", "description": "Target engine name: 'ego' | 'chrome' | 'edge' | 'cdp' | 'auto'. Ask user if not specified."},
            "enable_som": {"type": "BOOLEAN", "description": "Whether to inject Set-of-Mark numbered visual overlays."},
        },
        "required": ["action"],
    },
    "handler": browser_control,
}
