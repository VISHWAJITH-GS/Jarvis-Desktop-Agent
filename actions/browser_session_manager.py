"""
actions/browser_session_manager.py — Compatibility bridge redirecting to actions.browser_session.

Deprecated legacy isolated test profile launcher in favor of live Edge CDP session attachment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Optional

from actions.browser_session import (
    BrowserSessionManager,
    BrowserSessionState,
    CDPConnectionError,
    DEFAULT_CDP_URL,
    DEFAULT_CHROME_PORT,
    DEFAULT_EDGE_PORT,
    FrameInfo,
    TabInfo,
    build_browser_launch_command,
    get_browser_session_manager,
    get_default_cdp_port,
    get_default_cdp_url,
    locate_browser_executable,
    verify_browser_identity,
)


@dataclass
class BrowserNetworkError:
    url: str
    method: str
    status: Optional[int]
    error_text: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class BrowserConsoleMessage:
    msg_type: str
    text: str
    location: str
    timestamp: float = field(default_factory=time.time)


__all__ = [
    "BrowserSessionManager",
    "BrowserSessionState",
    "CDPConnectionError",
    "FrameInfo",
    "TabInfo",
    "get_browser_session_manager",
    "DEFAULT_CDP_URL",
    "DEFAULT_CHROME_PORT",
    "DEFAULT_EDGE_PORT",
    "get_default_cdp_port",
    "get_default_cdp_url",
    "locate_browser_executable",
    "build_browser_launch_command",
    "verify_browser_identity",
    "BrowserNetworkError",
    "BrowserConsoleMessage",
]
