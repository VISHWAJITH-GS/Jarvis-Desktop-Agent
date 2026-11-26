"""
core/apps/verifier.py — High-Speed DOM Verifier with Vision Fallback.

Reads real page state after an action and returns verified | failed | uncertain.
Uses high-speed DOM reads first (~20-50ms); uses vision-based verify_outcome
only as a fallback when DOM inspection cannot resolve the state.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

from core.apps.app_tab import AppTab

logger = logging.getLogger("app_verifier")

VERDICT_VERIFIED = "verified"
VERDICT_FAILED = "failed"
VERDICT_UNCERTAIN = "uncertain"


class AppVerifier:
    @classmethod
    def verify_dom_toast(cls, tab: AppTab, toast_selector: str, expected_snippet: str = "", timeout: float = 4.0) -> Tuple[bool, str]:
        """Check for success notification or toast message in DOM."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            toast = tab.wait_for_selector(toast_selector, timeout_sec=0.5)
            if toast:
                try:
                    text = tab.browser_mgr.run_coroutine(toast.inner_text()) or ""
                    if not expected_snippet or expected_snippet.lower() in text.lower():
                        return True, f"Verified success toast: '{text.strip()}'"
                except Exception:
                    return True, "Verified toast appearance."
            time.sleep(0.2)
        return False, "Success toast was not detected in DOM."

    @classmethod
    def verify_element_presence(cls, tab: AppTab, selector: str, timeout: float = 3.0) -> Tuple[bool, str]:
        """Check if expected element is present and visible in DOM."""
        el = tab.wait_for_selector(selector, timeout_sec=timeout)
        if el:
            return True, f"Verified presence of '{selector}'."
        return False, f"Element '{selector}' not found in DOM."

    @classmethod
    def verify_text_content(cls, tab: AppTab, selector: str, expected_text: str, timeout: float = 4.0) -> Tuple[bool, str]:
        """Check if element text matches or contains expected text."""
        deadline = time.monotonic() + timeout
        exp = expected_text.strip().lower()
        while time.monotonic() < deadline:
            try:
                el = tab.wait_for_selector(selector, timeout_sec=0.5)
                if el:
                    text = (tab.browser_mgr.run_coroutine(el.inner_text()) or "").strip().lower()
                    if exp in text or text in exp:
                        return True, f"Verified text match: '{text}'."
            except Exception:
                pass
            time.sleep(0.2)
        return False, f"Expected text '{expected_text}' not found in '{selector}'."

    @classmethod
    def verify_with_vision_fallback(cls, expected_outcome: str, player=None) -> Tuple[bool, str]:
        """Optional vision-based fallback when DOM checks cannot resolve."""
        try:
            from actions.verify_outcome import verify_outcome
            res = verify_outcome({"expected": expected_outcome, "scope": "browser_tab"}, player=player)
            if hasattr(res, "is_success") and res.is_success():
                return True, res.message or "Visually verified."
            return False, res.message or "Visual verification failed."
        except Exception as e:
            logger.debug("[AppVerifier] Vision fallback failed: %s", e)
            return False, f"Vision fallback error: {e}"
