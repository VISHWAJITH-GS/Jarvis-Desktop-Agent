"""
actions/browser/cdp_provider.py — CDP Browser Engine Provider (Chrome & Microsoft Edge).
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from actions.action_executor import ActionExecutor, get_action_executor
from actions.browser.base_provider import BrowserProvider
from actions.browser.browser_models import BrowserStatus, SpaceInfo, TabInfo
from actions.browser_session import (
    BrowserSessionManager,
    CDPConnectionError,
    get_browser_session_manager,
)
from actions.dom_distiller import DOMDistiller, get_dom_distiller
from memory import config_manager

logger = logging.getLogger("cdp_provider")


class CDPProvider(BrowserProvider):
    """
    CDP-based Browser Provider controlling Google Chrome or Microsoft Edge.
    Preserves all existing perception, AXTree distillation, and hardware-level input handling.
    """

    def __init__(
        self,
        engine: Optional[str] = None,
        session_manager: Optional[BrowserSessionManager] = None,
        distiller: Optional[DOMDistiller] = None,
        executor: Optional[ActionExecutor] = None,
    ):
        self._configured_engine = (engine or config_manager.get_browser_engine() or "chrome").lower()
        self.session_mgr = session_manager or get_browser_session_manager(engine=self._configured_engine)
        self.distiller = distiller or get_dom_distiller()
        self.executor = executor or ActionExecutor(
            session_manager=self.session_mgr, distiller=self.distiller
        )

    @property
    def name(self) -> str:
        return f"{self._configured_engine}_cdp"

    @property
    def engine(self) -> str:
        return self._configured_engine

    @property
    def is_connected(self) -> bool:
        ok, _ = self.session_mgr.check_connection(timeout=0.8)
        return ok

    @property
    def active_page_url(self) -> Optional[str]:
        page = self.session_mgr.active_page
        if page and not page.is_closed():
            try:
                return page.url
            except Exception:
                pass
        return None

    @property
    def active_page_title(self) -> Optional[str]:
        page = self.session_mgr.active_page
        if page and not page.is_closed():
            try:
                return self.session_mgr.run_coroutine(page.title())
            except Exception:
                pass
        return None

    @property
    def active_page(self) -> Any:
        """Compatibility access for actions expecting a Playwright Page object."""
        return self.session_mgr.active_page

    def check_health(self, timeout: float = 2.0) -> Tuple[bool, str]:
        ok, msg = self.session_mgr.check_connection(timeout=timeout)
        if not ok and hasattr(self.session_mgr, "_try_auto_launch"):
            try:
                if self.session_mgr._try_auto_launch():
                    ok, msg = self.session_mgr.check_connection(timeout=timeout)
            except Exception as e:
                msg = f"{msg} (Auto-launch error: {e})"
        return ok, msg

    def get_status(self) -> BrowserStatus:
        st = self.session_mgr.get_session_state()
        tabs = [
            TabInfo(
                index=t.index,
                title=t.title,
                url=t.url,
                target_id=t.target_id,
                is_active=t.is_active,
            )
            for t in st.tabs
        ]
        spaces = [
            SpaceInfo(
                id="default",
                name=f"Dedicated {self._configured_engine.capitalize()} Session",
                ownership="user",
                profile_id="Default",
                profile_name="JarvisProfile",
                is_active=True,
                tab_count=len(tabs),
                recent_titles=[t.title for t in tabs[:5]],
            )
        ]
        return BrowserStatus(
            provider=self.name,
            is_connected=st.is_connected,
            version=self.name,
            active_space="default",
            active_title=st.active_title,
            active_url=st.active_url,
            tab_count=st.tab_count,
            tabs=tabs,
            health="healthy" if st.is_connected else "disconnected",
            last_error=st.error,
            spaces=spaces,
            console_errors_count=st.console_errors_count,
            network_errors_count=st.network_errors_count,
        )

    def navigate(self, url: str) -> str:
        return self.session_mgr.run_coroutine(self.executor.navigate_to(url))

    def observe(self, enable_som: bool = False) -> str:
        return self.session_mgr.run_coroutine(self.executor.observe_page(enable_som=enable_som))

    def click(
        self,
        index: Optional[int] = None,
        text: Optional[str] = None,
        selector: Optional[str] = None,
    ) -> str:
        if index is not None:
            return self.session_mgr.run_coroutine(self.executor.click_element(int(index)))
        target_text = (text or selector or "").strip().lower()
        if not target_text:
            return "[Error] Provide 'index' or 'text' for click."

        lookup = self.distiller.get_current_lookup_map()
        if not lookup:
            self.session_mgr.run_coroutine(self.executor.observe_page())
            lookup = self.distiller.get_current_lookup_map()

        matched_idx = None
        for idx, elem in lookup.items():
            label = (elem.aria_label or "").lower()
            txt = (elem.text or "").lower()
            name = (elem.name or "").lower()
            if target_text in (label, txt, name):
                matched_idx = idx
                break
            if target_text in label or target_text in txt or target_text in name:
                if matched_idx is None:
                    matched_idx = idx

        if matched_idx is not None:
            return self.session_mgr.run_coroutine(self.executor.click_element(matched_idx))
        return f"[Error] Element matching '{target_text}' not found."

    def type_text(
        self,
        text: str,
        index: Optional[int] = None,
        selector: Optional[str] = None,
        press_enter: bool = False,
    ) -> str:
        if index is not None:
            return self.session_mgr.run_coroutine(
                self.executor.input_text(int(index), text, press_enter=press_enter)
            )
        target_field = (selector or "").strip().lower()
        lookup = self.distiller.get_current_lookup_map()
        if not lookup:
            self.session_mgr.run_coroutine(self.executor.observe_page())
            lookup = self.distiller.get_current_lookup_map()

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
            return self.session_mgr.run_coroutine(
                self.executor.input_text(matched_idx, text, press_enter=press_enter)
            )
        return "[Error] Could not locate an active input field for typing."

    def press_key(self, key: str) -> str:
        return self.session_mgr.run_coroutine(self.executor.press_key(key))

    def scroll(
        self,
        direction: str = "down",
        amount: int = 500,
        index: Optional[int] = None,
    ) -> str:
        if index is not None:
            return self.session_mgr.run_coroutine(self.executor.scroll_element(int(index), direction))
        return self.session_mgr.run_coroutine(self.executor.scroll_page(direction, amount=amount))

    def screenshot(self, path: str = "browser_screenshot.png") -> str:
        page = self.session_mgr.active_page
        if not page:
            return "[Error] No active page for screenshot."
        self.session_mgr.run_coroutine(page.screenshot(path=path))
        return f"Saved screenshot to {path}."

    def new_tab(self, url: str = "https://google.com", index: Optional[int] = None) -> TabInfo | str:
        if index is not None and not url:
            return self.session_mgr.run_coroutine(self.executor.open_link_in_new_tab(int(index)))
        tab = self.session_mgr.open_tab(url)
        return TabInfo(
            index=tab.index,
            title=tab.title,
            url=tab.url,
            target_id=tab.target_id,
            is_active=tab.is_active,
        )

    def switch_tab(self, tab_index: int) -> str:
        return self.session_mgr.run_coroutine(self.executor.switch_tab(tab_index))

    def close_tab(self, tab_index: Optional[int] = None) -> str:
        return self.session_mgr.close_tab(tab_index)

    def list_tabs(self) -> List[TabInfo]:
        raw_tabs = self.session_mgr.list_tabs()
        return [
            TabInfo(
                index=t.index,
                title=t.title,
                url=t.url,
                target_id=t.target_id,
                is_active=t.is_active,
            )
            for t in raw_tabs
        ]

    def go_back(self) -> str:
        return self.session_mgr.run_coroutine(self.executor.go_back())

    def wait_page(self, seconds: float = 2.0) -> str:
        return self.session_mgr.run_coroutine(self.executor.wait_page(seconds))

    def open_browser(self, headed: bool = True) -> bool:
        if hasattr(self.session_mgr, "_try_auto_launch"):
            return bool(self.session_mgr._try_auto_launch(headless=not headed))
        return False

    def disconnect(self) -> None:
        try:
            self.session_mgr.close_session()
        except Exception as e:
            logger.debug(f"Disconnect error on CDP provider: {e}")
