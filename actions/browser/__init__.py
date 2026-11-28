"""
actions/browser — Hybrid Browser Architecture Package for JARVIS Desktop Agent.
"""

from actions.browser.base_provider import BrowserProvider
from actions.browser.browser_models import (
    BrowserStatus,
    ElementRef,
    SpaceInfo,
    TabInfo,
)
from actions.browser.cdp_provider import CDPProvider
from actions.browser.ego_provider import EgoLiteProvider
from actions.browser.provider_manager import (
    BrowserProviderManager,
    get_browser_provider_manager,
)

__all__ = [
    "BrowserProvider",
    "CDPProvider",
    "EgoLiteProvider",
    "BrowserProviderManager",
    "get_browser_provider_manager",
    "BrowserStatus",
    "TabInfo",
    "SpaceInfo",
    "ElementRef",
]
