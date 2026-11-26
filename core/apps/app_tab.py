"""
core/apps/app_tab.py — Dedicated High-Level App Tab Controller over CDP.

Wraps a Playwright page with:
- ensure_open(url) — reuses open tab if already on the origin/domain, avoids duplicate navigations
- focus() — brings tab to front
- goto(deep_link) — instant direct-link navigation without UI wandering
- is_ready() — checks DOM readiness via event-based checks
- click_candidate() & type_candidate() — integrates with SelectorStore for self-healing selector fallback
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.apps.selector_store import get_selector_store

logger = logging.getLogger("app_tab")


class AppTab:
    def __init__(self, app_name: str, browser_mgr, page: Any = None):
        self.app_name = app_name.lower().strip()
        self.browser_mgr = browser_mgr
        self._page = page
        self.selector_store = get_selector_store()

    @property
    def page(self) -> Any:
        if self._page is None or (hasattr(self._page, "is_closed") and self._page.is_closed()):
            ctx = getattr(self.browser_mgr, "_context", None)
            if ctx:
                pages = getattr(ctx, "pages", [])
                for p in pages:
                    if not p.is_closed():
                        self._page = p
                        break
        return self._page

    @page.setter
    def page(self, val: Any) -> None:
        self._page = val

    def is_closed(self) -> bool:
        if self._page is None:
            return True
        try:
            return self._page.is_closed()
        except Exception:
            return True

    def url(self) -> str:
        if self.is_closed():
            return ""
        try:
            return str(self._page.url or "")
        except Exception:
            return ""

    def ensure_open(self, target_url: str, wait_selector: Optional[str] = None) -> bool:
        """Ensure tab is open at target_url. Reuses tab if already on target origin/path."""
        clean_target = target_url.strip()
        if not clean_target.startswith("http"):
            clean_target = "https://" + clean_target

        if hasattr(self.browser_mgr, "ensure_connection"):
            self.browser_mgr.ensure_connection()
        elif hasattr(self.browser_mgr, "_ensure_attached"):
            try:
                self.browser_mgr.run_coroutine(self.browser_mgr._ensure_attached())
            except Exception:
                pass

        if self.is_closed():
            # Find an existing tab in context or create a new one
            ctx = getattr(self.browser_mgr, "_context", None)
            if ctx:
                for p in ctx.pages:
                    if clean_target.split("?")[0].rstrip("/") in (p.url or "").split("?")[0].rstrip("/"):
                        self._page = p
                        break
            if self.is_closed():
                self._page = self.browser_mgr.run_coroutine(self._new_tab(clean_target))
                return True

        curr_url = self.url()
        # If already on destination origin/base, avoid unnecessary page reload
        base_target = clean_target.split("?")[0].rstrip("/")
        base_curr = curr_url.split("?")[0].rstrip("/")
        if base_target not in base_curr and base_curr not in base_target:
            self.goto(clean_target)
        else:
            self.focus()

        if wait_selector:
            return self.wait_for_selector(wait_selector, timeout_sec=10.0) is not None
        return True

    async def _new_tab(self, url: str) -> Any:
        if hasattr(self.browser_mgr, "_ensure_attached"):
            try:
                await self.browser_mgr._ensure_attached()
            except Exception as e:
                logger.debug("[AppTab] _ensure_attached notice: %s", e)
        ctx = getattr(self.browser_mgr, "_context", None)
        if not ctx:
            raise RuntimeError("Browser context is not initialized.")
        p = await ctx.new_page()
        await p.goto(url, wait_until="domcontentloaded", timeout=35000)
        return p

    def focus(self) -> None:
        """Bring this app tab to the front."""
        if not self.is_closed():
            try:
                self.browser_mgr.run_coroutine(self._page.bring_to_front())
            except Exception as e:
                logger.debug("[AppTab] focus error: %s", e)

    def goto(self, deep_link: str, wait_until: str = "domcontentloaded", timeout: float = 30.0) -> bool:
        """Directly navigate to deep link."""
        if self.is_closed():
            return self.ensure_open(deep_link)
        try:
            self.browser_mgr.run_coroutine(
                self._page.goto(deep_link, wait_until=wait_until, timeout=int(timeout * 1000))
            )
            return True
        except Exception as e:
            logger.warning("[AppTab] goto(%s) failed: %s", deep_link, e)
            return False

    def wait_for_selector(self, selector: str, timeout_sec: float = 5.0) -> Optional[Any]:
        if self.is_closed():
            return None
        try:
            return self.browser_mgr.run_coroutine(
                self._page.wait_for_selector(selector, timeout=int(timeout_sec * 1000))
            )
        except Exception:
            return None

    def find_first(self, element_key: str, default_candidates: List[str]) -> Tuple[Optional[Any], Optional[str]]:
        """Find the first matching element from candidate selectors, promoting the winner."""
        if self.is_closed():
            return None, None

        candidates = self.selector_store.get_candidates(self.app_name, element_key, default=default_candidates)
        for sel in candidates:
            try:
                el = self.browser_mgr.run_coroutine(self._page.query_selector(sel))
                if el:
                    # Verify visible if possible
                    is_vis = self.browser_mgr.run_coroutine(el.is_visible())
                    if is_vis:
                        self.selector_store.record_success(self.app_name, element_key, sel)
                        return el, sel
            except Exception:
                continue

        # Try default candidates if none worked
        for sel in default_candidates:
            if sel in candidates:
                continue
            try:
                el = self.browser_mgr.run_coroutine(self._page.query_selector(sel))
                if el and self.browser_mgr.run_coroutine(el.is_visible()):
                    self.selector_store.record_success(self.app_name, element_key, sel)
                    return el, sel
            except Exception:
                continue

        return None, None

    def click_candidate(self, element_key: str, default_candidates: List[str]) -> bool:
        """Click element using resilient candidate fallback."""
        el, _ = self.find_first(element_key, default_candidates)
        if not el:
            return False
        try:
            self.browser_mgr.run_coroutine(el.click())
            return True
        except Exception as e:
            logger.debug("[AppTab] Click failed on %s: %s", element_key, e)
            return False

    def type_candidate(
        self,
        element_key: str,
        text: str,
        default_candidates: List[str],
        press_enter: bool = False,
        clear_first: bool = True,
    ) -> bool:
        """Focus and type text into element using resilient candidate fallback."""
        el, _ = self.find_first(element_key, default_candidates)
        if not el:
            return False
        try:
            self.browser_mgr.run_coroutine(el.click())
            time.sleep(0.05)
            if clear_first:
                self.browser_mgr.run_coroutine(self._page.keyboard.press("Control+A"))
                self.browser_mgr.run_coroutine(self._page.keyboard.press("Backspace"))
                time.sleep(0.05)

            # Insert text via CDP if available
            bm = self.browser_mgr
            cdp = bm.run_coroutine(bm._get_cdp_session()) if hasattr(bm, "_get_cdp_session") else None
            if cdp:
                bm.run_coroutine(cdp.send("Input.insertText", {"text": text}))
            else:
                self.browser_mgr.run_coroutine(self._page.keyboard.type(text))

            if press_enter:
                time.sleep(0.05)
                self.browser_mgr.run_coroutine(self._page.keyboard.press("Enter"))
            return True
        except Exception as e:
            logger.warning("[AppTab] Type into %s failed: %s", element_key, e)
            return False

    def evaluate(self, js_expr: str, *args) -> Any:
        if self.is_closed():
            return None
        try:
            return self.browser_mgr.run_coroutine(self._page.evaluate(js_expr, *args))
        except Exception as e:
            logger.debug("[AppTab] evaluate error: %s", e)
            return None
