"""
core/apps/browser_session.py — Persistent App Browser Session Manager.

Attaches to the dedicated Jarvis Chrome profile via remote debugging port 9222.
Reuses one persistent AppTab per app (WhatsApp, Gmail, Spotify, ChatGPT).
Reattaches automatically if the browser was closed.
Never launches an isolated or unauthenticated automation browser.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from actions.browser_session import get_browser_session_manager, BrowserSessionManager
from core.apps.app_tab import AppTab

logger = logging.getLogger("app_browser_session")


class AppBrowserSession:
    """Singleton session coordinator managing persistent per-app tabs."""

    _instance: Optional[AppBrowserSession] = None

    def __init__(self, session_manager: Optional[BrowserSessionManager] = None):
        self._session_mgr = session_manager or get_browser_session_manager()
        self._app_tabs: Dict[str, AppTab] = {}

    @classmethod
    def get_instance(cls) -> AppBrowserSession:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def raw_session_manager(self) -> BrowserSessionManager:
        return self._session_mgr

    def ensure_connection(self) -> Tuple[bool, str]:
        """Ensure connection to the dedicated Chrome profile; auto-launch if needed."""
        ok, err = self._session_mgr.check_connection(timeout=1.5)
        if not ok and hasattr(self._session_mgr, "_try_auto_launch"):
            if self._session_mgr._try_auto_launch():
                ok = True
        if ok and hasattr(self._session_mgr, "_ensure_attached"):
            try:
                self._session_mgr.run_coroutine(self._session_mgr._ensure_attached())
            except Exception as e:
                logger.debug("[AppBrowserSession] CDP attach notice: %s", e)
        return ok, err

    def get_tab(self, app_name: str, default_url: str = "") -> AppTab:
        """Get or create the persistent AppTab for the requested app."""
        app = app_name.lower().strip()
        self.ensure_connection()

        tab = self._app_tabs.get(app)
        if tab is None or tab.is_closed():
            # Check if an existing page in browser context already matches app's domain
            ctx = getattr(self._session_mgr, "_context", None)
            matched_page = None
            if ctx:
                domain = default_url.replace("https://", "").replace("http://", "").split("/")[0]
                for p in ctx.pages:
                    if domain and domain in (p.url or ""):
                        matched_page = p
                        break

            tab = AppTab(app, self._session_mgr, page=matched_page)
            self._app_tabs[app] = tab

        if default_url and tab.is_closed():
            tab.ensure_open(default_url)

        return tab


def get_app_session() -> AppBrowserSession:
    return AppBrowserSession.get_instance()
