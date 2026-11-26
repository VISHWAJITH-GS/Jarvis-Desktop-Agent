"""
core/apps — High-Level Verified Web Applications Engine for JARVIS.

Components:
- BrowserSession: Manages persistent CDP attachment to dedicated Jarvis Chrome profile.
- AppTab: Dedicated per-app tab controller with deep links, event waits, and selector fallback.
- SessionGuard: Fast pre-execution check verifying active login state.
- SelectorStore: Self-healing candidate selector store with memory.
- AppConfirmationGate: Immutable human confirmation gate.
- AppVerifier: Fast DOM verification with vision fallback.
- Untrusted: Safety wrapping for prompt injection defense.
- ContactBook: Contact resolution and fuzzy spelling variant matching.
- ToolResult: Standard return shape for fast, reliable, honest actions.
"""

from core.apps.tool_result import (
    ToolResult,
    STATUS_OK,
    STATUS_PENDING_CONFIRMATION,
    STATUS_NEEDS_LOGIN,
    STATUS_NOT_FOUND,
    STATUS_FAILED,
    STATUS_UNCERTAIN,
)
from core.apps.untrusted import wrap_untrusted, is_untrusted, strip_untrusted_markers
from core.apps.contact_book import ContactBook, get_contact_book
from core.apps.selector_store import SelectorStore, get_selector_store
from core.apps.app_tab import AppTab
from core.apps.browser_session import AppBrowserSession, get_app_session
from core.apps.session_guard import SessionGuard
from core.apps.confirmation_gate import AppConfirmationGate
from core.apps.verifier import AppVerifier
from core.apps.metrics import AppMetricsTracker

__all__ = [
    "ToolResult",
    "STATUS_OK",
    "STATUS_PENDING_CONFIRMATION",
    "STATUS_NEEDS_LOGIN",
    "STATUS_NOT_FOUND",
    "STATUS_FAILED",
    "STATUS_UNCERTAIN",
    "wrap_untrusted",
    "is_untrusted",
    "strip_untrusted_markers",
    "ContactBook",
    "get_contact_book",
    "SelectorStore",
    "get_selector_store",
    "AppTab",
    "AppBrowserSession",
    "get_app_session",
    "SessionGuard",
    "AppConfirmationGate",
    "AppVerifier",
    "AppMetricsTracker",
]
