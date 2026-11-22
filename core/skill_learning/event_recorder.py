"""
core/skill_learning/event_recorder.py — Desktop Event Listener & Interaction Recorder.

Records user interactions during a demonstration session:
- Semantic click capture (role, accessible name/aria-label, DOM/UIA automation id, app, URL; never bare coordinates)
- Keyboard shortcuts (Ctrl+A, Ctrl+C, Ctrl+V, Ctrl+S) and typed text with credential/secret redaction
- Navigation (URL changes, tab switches, window activations)
- Clipboard copy/paste events with character count only (never full text)
- File dialogs (saved paths and filenames)
- User speech narration aligned by timestamp to UI events
- Privacy protections: memory-only traces, auto-pause on password managers/banking/lock screen
"""

from __future__ import annotations

import os
from pathlib import Path
import platform
import threading
import time
from typing import Callable, List, Optional

from core.skill_learning.models import UIEvent
from core.skill_learning.skill_security import (
    is_privacy_risk_window,
    is_sensitive_control,
    redact_secret_with_length,
    sanitize_input_value,
)
from core.skill_learning.ui_state_capture import (
    capture_element_at_point,
    get_current_ui_state,
    normalize_semantic_role,
)

_IS_WINDOWS = platform.system() == "Windows"


class EventRecorder:
    """Thread-safe user interaction recorder for demonstration sessions."""

    def __init__(self, on_event_cb: Optional[Callable[[UIEvent], None]] = None):
        self.on_event_cb = on_event_cb
        self._events: list[UIEvent] = []
        self._is_recording = False
        self._is_paused = False
        self._privacy_paused = False
        self._lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._last_window_title = ""
        self._last_process_name = ""
        self._last_url = ""

    def start(self) -> None:
        """Start recording user actions."""
        with self._lock:
            self._events.clear()
            self._is_recording = True
            self._is_paused = False
            self._privacy_paused = False
            self._last_window_title = ""
            self._last_process_name = ""
            self._last_url = ""

        self._monitor_thread = threading.Thread(
            target=self._poll_desktop_state,
            daemon=True,
            name="SkillEventRecorderThread",
        )
        self._monitor_thread.start()

    def pause(self) -> None:
        """Temporarily pause event capture."""
        with self._lock:
            self._is_paused = True

    def resume(self) -> None:
        """Resume event capture."""
        with self._lock:
            self._is_paused = False

    def stop(self) -> list[UIEvent]:
        """Stop recording and return captured events."""
        with self._lock:
            self._is_recording = False
            self._is_paused = False
            self._privacy_paused = False
            return list(self._events)

    def is_active(self) -> bool:
        with self._lock:
            return self._is_recording and not self._is_paused and not self._privacy_paused

    def is_privacy_paused(self) -> bool:
        with self._lock:
            return self._privacy_paused

    def record_click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> Optional[UIEvent]:
        """Record a mouse click on screen mapped to semantic UI element."""
        if not self.is_active():
            return None

        now = time.time()
        elem_info = capture_element_at_point(x, y) or {}
        ui_state = get_current_ui_state(max_elements=10)

        # Semantic identification
        role = elem_info.get("role") or normalize_semantic_role(elem_info.get("control_type", ""), elem_info.get("class_name", ""))
        elem_name = elem_info.get("name", "")
        if elem_name == "[PROTECTED_PASSWORD_FIELD]":
            elem_name = "[PASSWORD_INPUT]"
        auto_id = elem_info.get("automation_id", "")
        app_name = ui_state.app_name or elem_info.get("class_name", "Unknown")
        win_title = ui_state.window_title
        url = ui_state.url

        # Build semantic selector — NEVER bare coordinates alone
        selector = {
            "role": role,
            "name": elem_name,
            "automation_id": auto_id,
            "app": app_name,
        }
        if url:
            selector["url"] = url
        # Strip empty attributes
        selector = {k: v for k, v in selector.items() if v}
        if not selector:
            selector = {"role": "control", "app": app_name, "window": win_title}

        bbox = elem_info.get("bounding_rectangle") or {"x": x, "y": y, "width": 0, "height": 0}

        event = UIEvent(
            timestamp=now,
            event_type="double_click" if clicks == 2 else "click",
            app_name=app_name,
            window_title=win_title,
            element_name=elem_name,
            control_type=elem_info.get("control_type", role),
            automation_id=auto_id,
            role=role,
            selector=selector,
            action=f"click_{button}",
            input_value="",
            bounding_box=bbox,
            url=url,
        )
        self._emit_event(event)
        return event

    def record_text_input(self, text: str, element_info: Optional[dict] = None) -> Optional[UIEvent]:
        """Record text input with password and secret redaction."""
        if not self.is_active():
            return None

        now = time.time()
        elem = element_info or {}
        ui_state = get_current_ui_state(max_elements=5)

        # Strictly redact passwords and secrets at capture time: placeholder + length
        clean_text, is_sec, sec_len = redact_secret_with_length(elem, text)

        role = elem.get("role") or normalize_semantic_role(elem.get("control_type", "Edit"), elem.get("class_name", ""))
        elem_name = elem.get("name", "Text Field")
        auto_id = elem.get("automation_id", "")
        app_name = ui_state.app_name or "Application"

        selector = {
            "role": role,
            "name": elem_name if not elem.get("is_sensitive") else "[PASSWORD_INPUT]",
            "automation_id": auto_id,
            "app": app_name,
        }
        selector = {k: v for k, v in selector.items() if v}

        event = UIEvent(
            timestamp=now,
            event_type="type_text",
            app_name=app_name,
            window_title=ui_state.window_title,
            element_name=elem_name,
            control_type=elem.get("control_type", "Edit"),
            automation_id=auto_id,
            role=role,
            selector=selector,
            action="type_text",
            input_value=clean_text,
            url=ui_state.url,
            is_secret=is_sec,
            secret_length=sec_len if is_sec else None,
        )
        self._emit_event(event)
        return event

    def record_hotkey(self, shortcut: str, app_name: str = "", window_title: str = "") -> Optional[UIEvent]:
        """Record keyboard shortcut (Ctrl+A, Ctrl+C, Ctrl+V, Ctrl+S, etc.)."""
        if not self.is_active():
            return None

        now = time.time()
        ui_state = get_current_ui_state(max_elements=3) if not app_name else None
        app = app_name or (ui_state.app_name if ui_state else "Desktop")
        title = window_title or (ui_state.window_title if ui_state else "")

        event = UIEvent(
            timestamp=now,
            event_type="hotkey",
            app_name=app,
            window_title=title,
            element_name=shortcut,
            action="hotkey",
            shortcut=shortcut,
            input_value=shortcut,
            selector={"shortcut": shortcut, "app": app},
        )
        self._emit_event(event)
        return event

    def record_clipboard(self, action: str, char_count: int, app_name: str = "", window_title: str = "") -> Optional[UIEvent]:
        """Record copy/paste event with character count only (never full text)."""
        if not self.is_active():
            return None

        now = time.time()
        ui_state = get_current_ui_state(max_elements=3) if not app_name else None
        app = app_name or (ui_state.app_name if ui_state else "Desktop")
        title = window_title or (ui_state.window_title if ui_state else "")

        event = UIEvent(
            timestamp=now,
            event_type="clipboard",
            app_name=app,
            window_title=title,
            action=action,  # "copy" | "paste"
            char_count=char_count,
            input_value=f"[CLIPBOARD:{char_count} chars]",
            selector={"action": action, "app": app},
        )
        self._emit_event(event)
        return event

    def record_navigation(
        self,
        url: str,
        app_name: str = "",
        window_title: str = "",
        nav_type: str = "url_change",
    ) -> Optional[UIEvent]:
        """Record URL navigation or tab switch."""
        if not self.is_active():
            return None

        now = time.time()
        event = UIEvent(
            timestamp=now,
            event_type="navigation",
            app_name=app_name or "Browser",
            window_title=window_title,
            action=nav_type,
            url=url,
            input_value=url,
            selector={"url": url, "app": app_name},
        )
        self._emit_event(event)
        return event

    def record_file_dialog(
        self,
        action: str,
        file_path: str,
        filename: str = "",
        app_name: str = "",
        window_title: str = "",
    ) -> Optional[UIEvent]:
        """Record file dialog interaction (Open / Save As)."""
        if not self.is_active():
            return None

        now = time.time()
        fname = filename or Path(file_path).name if file_path else ""
        event = UIEvent(
            timestamp=now,
            event_type="file_dialog",
            app_name=app_name or "Explorer",
            window_title=window_title or f"File {action.capitalize()}",
            action=action,  # "save" | "open"
            file_path=file_path,
            filename=fname,
            input_value=file_path,
            selector={"dialog_action": action, "filename": fname},
        )
        self._emit_event(event)
        return event

    def record_narration(self, text: str, timestamp: Optional[float] = None) -> Optional[UIEvent]:
        """Record user spoken narration aligned to demonstration events."""
        if not self._is_recording:
            return None

        now = timestamp or time.time()
        event = UIEvent(
            timestamp=now,
            event_type="narration",
            app_name="JarvisVoice",
            window_title="SpokenNarration",
            element_name="narration",
            action="user_narration",
            input_value=text.strip(),
        )
        self._emit_event(event)
        return event

    def record_custom_event(
        self,
        event_type: str,
        app_name: str,
        window_title: str,
        action: str,
        element_name: str = "",
        selector: Optional[dict] = None,
        input_value: str = "",
    ) -> UIEvent:
        """Inject or record custom structured event."""
        now = time.time()
        clean_val, _, _ = redact_secret_with_length(None, input_value)

        event = UIEvent(
            timestamp=now,
            event_type=event_type,
            app_name=app_name,
            window_title=window_title,
            element_name=element_name,
            selector=selector or {},
            action=action,
            input_value=clean_val,
        )
        self._emit_event(event)
        return event

    def _emit_event(self, event: UIEvent) -> None:
        with self._lock:
            self._events.append(event)

        evt_kind = event.event_type
        if evt_kind in ("click", "double_click"):
            el_lower = (event.element_name or "").lower()
            if "profile" in el_lower or "student" in el_lower or "user" in el_lower or "@" in el_lower:
                evt_kind = "profile_selected"
            else:
                evt_kind = "button_clicked"
        elif evt_kind == "window_activate":
            if any(b in event.app_name.lower() for b in ("chrome", "msedge", "edge", "firefox", "browser")):
                evt_kind = "browser_navigation"
            else:
                evt_kind = "window_activated"

        print(f"[Observation] Event captured: {evt_kind}")

        if self.on_event_cb:
            try:
                self.on_event_cb(event)
            except Exception:
                pass

    def _poll_desktop_state(self) -> None:
        """Background thread polling for clicks, shortcuts, window switches, and privacy safety."""
        import ctypes
        from ctypes import wintypes

        last_lbutton = False
        last_click_time = 0.0
        last_window_check = 0.0
        last_ctrl_state = False
        last_shortcut_time = 0.0

        # Virtual-Key codes
        VK_LBUTTON = 0x01
        VK_CONTROL = 0x11
        VK_A = 0x41
        VK_C = 0x43
        VK_V = 0x56
        VK_S = 0x53

        while True:
            with self._lock:
                if not self._is_recording:
                    break
                paused = self._is_paused

            if not paused and _IS_WINDOWS:
                now = time.time()
                try:
                    # 1. Privacy Safety & Window Changes (~every 250ms)
                    if now - last_window_check > 0.25:
                        last_window_check = now
                        from core import platform_hooks

                        win = platform_hooks.call_hook("get_active_window", default=None)
                        if win and win.get("status") == "success":
                            title = win.get("title", "")
                            pname = win.get("process_name", "")
                            app_name = pname.replace(".exe", "").capitalize()

                            # Check privacy risk
                            is_risk, rtype = is_privacy_risk_window(title=title, app_name=app_name, process_name=pname)
                            with self._lock:
                                if is_risk and not self._privacy_paused:
                                    self._privacy_paused = True
                                    print(f"[Observation] ⚠️ Privacy risk detected ({rtype} on '{title}'). Observation paused.")
                                elif not is_risk and self._privacy_paused:
                                    self._privacy_paused = False
                                    print(f"[Observation] Safe window active ('{title}'). Observation resumed.")

                            # Window activation event
                            if not self._privacy_paused:
                                if (title and title != self._last_window_title) or (pname and pname != self._last_process_name):
                                    self._last_window_title = title
                                    self._last_process_name = pname
                                    evt = UIEvent(
                                        timestamp=now,
                                        event_type="window_activate",
                                        app_name=app_name,
                                        window_title=title,
                                        action="activate_window",
                                    )
                                    self._emit_event(evt)

                    # 2. Mouse clicks
                    if not self._privacy_paused:
                        lbutton_down = bool(ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
                        if lbutton_down and not last_lbutton and (now - last_click_time > 0.15):
                            last_click_time = now
                            pt = wintypes.POINT()
                            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                            self.record_click(pt.x, pt.y)
                        last_lbutton = lbutton_down

                    # 3. Keyboard shortcuts (Ctrl+A, Ctrl+C, Ctrl+V, Ctrl+S)
                    if not self._privacy_paused:
                        ctrl_down = bool(ctypes.windll.user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                        if ctrl_down and (now - last_shortcut_time > 0.3):
                            if ctypes.windll.user32.GetAsyncKeyState(VK_A) & 0x8000:
                                last_shortcut_time = now
                                self.record_hotkey("Ctrl+A")
                            elif ctypes.windll.user32.GetAsyncKeyState(VK_C) & 0x8000:
                                last_shortcut_time = now
                                self.record_hotkey("Ctrl+C")
                                self.record_clipboard("copy", 0)
                            elif ctypes.windll.user32.GetAsyncKeyState(VK_V) & 0x8000:
                                last_shortcut_time = now
                                self.record_hotkey("Ctrl+V")
                                self.record_clipboard("paste", 0)
                            elif ctypes.windll.user32.GetAsyncKeyState(VK_S) & 0x8000:
                                last_shortcut_time = now
                                self.record_hotkey("Ctrl+S")

                except Exception:
                    pass

            time.sleep(0.03)
