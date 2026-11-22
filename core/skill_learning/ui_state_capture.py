"""
core/skill_learning/ui_state_capture.py — Semantic UI State Inspection and Control Extraction.

Captures high-fidelity desktop UI state during user demonstration:
- Active window identification (title, process, executable)
- Accessible element hierarchy and actionable controls
- Control under cursor coordinates
- Password/credential protection
- Browser URL and state extraction
"""

from __future__ import annotations

import platform
import time
from typing import Any, Dict, List, Optional, Tuple

from core.skill_learning.models import UIState
from core.skill_learning.skill_security import is_privacy_risk_window, is_sensitive_control

_IS_WINDOWS = platform.system() == "Windows"

_ROLE_MAP = {
    "button": "button",
    "pushbutton": "button",
    "tabitem": "tab",
    "tab": "tab",
    "hyperlink": "link",
    "link": "link",
    "menuitem": "menu_item",
    "menu": "menu_item",
    "edit": "edit",
    "textbox": "edit",
    "checkbox": "checkbox",
    "radiobutton": "radio_button",
    "combobox": "combobox",
    "listitem": "list_item",
    "treeitem": "tree_item",
}


def normalize_semantic_role(control_type: str, class_name: str = "") -> str:
    """Normalize raw UIA control type or class name to standard semantic role."""
    ct = str(control_type or "").strip().lower()
    cn = str(class_name or "").strip().lower()
    for key, role in _ROLE_MAP.items():
        if key in ct or key in cn:
            return role
    return ct or "control"


def get_current_ui_state(max_elements: int = 25) -> UIState:
    """Capture snapshot of the active desktop window and actionable controls."""
    now = time.time()
    state = UIState(
        timestamp=now,
        app_name="Desktop",
        window_title="",
        process_name="",
        elements=[],
        focused_element=None,
        is_browser=False,
        url=None,
    )

    if not _IS_WINDOWS:
        return state

    try:
        from core import platform_hooks

        win_info = platform_hooks.call_hook("get_active_window", default=None)
        if win_info and win_info.get("status") == "success":
            state.window_title = win_info.get("title", "")
            state.process_name = win_info.get("process_name", "")
            p_lower = state.process_name.lower()
            state.app_name = state.process_name.replace(".exe", "").capitalize()

            # Detect if browser
            if any(b in p_lower for b in ("chrome", "msedge", "edge", "firefox", "brave", "opera")):
                state.is_browser = True
                try:
                    tab_info = platform_hooks.call_hook("get_active_browser_tab", default=None)
                    if tab_info and isinstance(tab_info, dict) and tab_info.get("url"):
                        state.url = tab_info["url"]
                except Exception:
                    pass

            # Privacy risk assessment
            is_risk, risk_type = is_privacy_risk_window(
                title=state.window_title,
                app_name=state.app_name,
                process_name=state.process_name,
                url=state.url,
            )
            state.is_privacy_risk = is_risk
            state.privacy_risk_type = risk_type

        # Inspect actionable controls
        ins = platform_hooks.call_hook(
            "inspect_window", max_depth=3, limit=max_elements, only_actionable=True, default=None
        )
        if ins and ins.get("status") == "success":
            clean_elems = []
            for e in ins.get("elements", []):
                # Flag sensitive controls
                is_sens = is_sensitive_control(e)
                role = normalize_semantic_role(e.get("control_type", ""), e.get("class_name", ""))
                clean_elems.append(
                    {
                        "name": "[PROTECTED]" if is_sens else e.get("name", ""),
                        "control_type": e.get("control_type", ""),
                        "role": role,
                        "automation_id": e.get("automation_id", ""),
                        "class_name": e.get("class_name", ""),
                        "bounding_rectangle": e.get("bounding_rectangle"),
                        "is_sensitive": is_sens,
                    }
                )
            state.elements = clean_elems

    except Exception as e:
        print(f"[UIStateCapture] Error capturing UI state: {e}")

    return state


def capture_element_at_point(x: int, y: int) -> Optional[dict]:
    """Inspect element directly under screen coordinates (x, y)."""
    if not _IS_WINDOWS:
        return None
    try:
        from core import platform_hooks

        elem_data = platform_hooks.call_hook("find_element_at_point", x, y, default=None)
        if elem_data and "info" in elem_data:
            info = dict(elem_data["info"])
            is_sens = is_sensitive_control(info)
            if is_sens:
                info["name"] = "[PROTECTED_PASSWORD_FIELD]"
                info["is_sensitive"] = True
            info["role"] = normalize_semantic_role(info.get("control_type", ""), info.get("class_name", ""))
            return info
    except Exception as e:
        print(f"[UIStateCapture] Point inspect error ({x}, {y}): {e}")
    return None
