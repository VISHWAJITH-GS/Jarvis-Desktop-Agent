"""
actions/browser/base_provider.py — Abstract Base Class for Browser Providers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List, Optional, Tuple

from actions.browser.browser_models import BrowserStatus, SpaceInfo, TabInfo


class BrowserProvider(ABC):
    """
    Abstract interface for all browser engine implementations (EgoLite and CDP).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier: 'ego', 'chrome', 'edge', 'cdp'."""
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """True if the browser session is currently active and reachable."""
        pass

    @property
    @abstractmethod
    def active_page_url(self) -> Optional[str]:
        """Active page URL, or None if no page is attached."""
        pass

    @property
    @abstractmethod
    def active_page_title(self) -> Optional[str]:
        """Active page title, or None if no page is attached."""
        pass

    @abstractmethod
    def check_health(self, timeout: float = 2.0) -> Tuple[bool, str]:
        """Perform a live health probe. Returns (is_healthy, status_message)."""
        pass

    @abstractmethod
    def get_status(self) -> BrowserStatus:
        """Return structured status of this provider."""
        pass

    @abstractmethod
    def navigate(self, url: str) -> str:
        """Navigate active tab to url and return outcome summary."""
        pass

    @abstractmethod
    def observe(self, enable_som: bool = False) -> str:
        """Extract fresh perception observation (indexed elements and page summary)."""
        pass

    @abstractmethod
    def click(
        self,
        index: Optional[int] = None,
        text: Optional[str] = None,
        selector: Optional[str] = None,
    ) -> str:
        """Click an element by integer index, visible text, or selector."""
        pass

    @abstractmethod
    def type_text(
        self,
        text: str,
        index: Optional[int] = None,
        selector: Optional[str] = None,
        press_enter: bool = False,
    ) -> str:
        """Input text into target element with optional Enter submission."""
        pass

    @abstractmethod
    def press_key(self, key: str) -> str:
        """Dispatch hardware-level keyboard key press (Enter, Tab, Escape, etc.)."""
        pass

    @abstractmethod
    def scroll(
        self,
        direction: str = "down",
        amount: int = 500,
        index: Optional[int] = None,
    ) -> str:
        """Scroll the page or targeted container."""
        pass

    @abstractmethod
    def screenshot(self, path: str = "browser_screenshot.png") -> str:
        """Save a viewport screenshot to the specified path."""
        pass

    @abstractmethod
    def new_tab(self, url: str = "https://google.com", index: Optional[int] = None) -> TabInfo | str:
        """Open a new tab or open element link in new tab."""
        pass

    @abstractmethod
    def switch_tab(self, tab_index: int) -> str:
        """Switch active tab focus by tab index."""
        pass

    @abstractmethod
    def close_tab(self, tab_index: Optional[int] = None) -> str:
        """Close specified tab (or active tab if None)."""
        pass

    @abstractmethod
    def list_tabs(self) -> List[TabInfo]:
        """List open tabs in the current session/space."""
        pass

    @abstractmethod
    def go_back(self) -> str:
        """Navigate backwards in page history."""
        pass

    @abstractmethod
    def wait_page(self, seconds: float = 2.0) -> str:
        """Explicitly pause/wait for dynamic content or settling."""
        pass

    @abstractmethod
    def open_browser(self, headed: bool = True) -> bool:
        """Launch or open browser window if not currently running."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Cleanly disconnect session or release hooks."""
        pass

    # ── Spaces Management (Ego native, CDP simulated) ─────────────────────────

    def create_space(self, name: str) -> SpaceInfo | str:
        """Create an isolated workspace (Space). Default fallback for providers without spaces."""
        return f"Spaces are not natively supported by {self.name} provider."

    def switch_space(self, space_id: int | str) -> str:
        """Switch active task space. Default fallback."""
        return f"Spaces are not natively supported by {self.name} provider."

    def close_space(self, space_id: Optional[int | str] = None) -> str:
        """Finish and close a task space. Default fallback."""
        return f"Spaces are not natively supported by {self.name} provider."

    def list_spaces(self) -> List[SpaceInfo]:
        """List active task spaces."""
        return []
