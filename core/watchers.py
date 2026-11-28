"""
core/watchers.py — Event-Driven Background Watchers for Contact Messages & Antigravity Agent.

Provides:
- Contact Notification Watcher:
  Subscribes to Windows toast notifications via WinRT UserNotificationListener
  (with graceful PowerShell / UIA / Title fallback). Alerts Main Key session immediately
  upon fuzzy contact match without polling delays.
- Google Antigravity Task Watcher:
  Monitors active Antigravity CLI/IDE processes (Antigravity.exe window titles via Win32)
  and ~/.gemini/antigravity-cli/ log updates via watchdog (with mtime fallback).
  Alerts user verbally via Main Key the moment the Antigravity agent finishes or pauses
  for review/terminal approval.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import platform
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("watchers")
_IS_WINDOWS = platform.system() == "Windows"


# ── Contact Notification Watcher ─────────────────────────────────────────────

@dataclass
class ContactMessageAlert:
    contact: str
    app: str
    sender: str
    preview: str
    timestamp: float = field(default_factory=time.time)


class ContactNotificationWatcher:
    """
    Subscribes to system toast notifications. When an incoming message matches
    a watched contact name (case-insensitive fuzzy match), immediately triggers
    voice and UI alerts on the Main Key session.
    """

    def __init__(self, alert_callback: Optional[Callable[[ContactMessageAlert], None]] = None):
        self.alert_callback = alert_callback
        self.watched_contacts: Dict[str, List[str]] = {}  # contact_name -> app_filter
        self._seen_notification_ids: Set[str] = set()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def add_watch(self, contact_name: str, app_filter: Optional[List[str]] = None) -> None:
        """Add a contact to watch list."""
        contact = contact_name.strip()
        apps = [a.lower() for a in (app_filter or ["WhatsApp", "Edge"])]
        with self._lock:
            self.watched_contacts[contact] = apps
        logger.info(f"[Watchers] Watching messages for contact='{contact}' apps={apps}")

    def remove_watch(self, contact_name: str) -> None:
        with self._lock:
            self.watched_contacts.pop(contact_name.strip(), None)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="contact-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _matches_contact(self, query_contact: str, text: str) -> bool:
        """Case-insensitive fuzzy match for contact name."""
        if not query_contact or not text:
            return False
        qc = query_contact.lower().strip()
        t = text.lower()

        # Direct substring
        if qc in t:
            return True

        # Word boundary or token check
        tokens = re.findall(r"\w+", t)
        if qc in tokens:
            return True

        # Fuzzy ratio check
        for token in tokens:
            if len(token) >= 3 and difflib.SequenceMatcher(None, qc, token).ratio() >= 0.82:
                return True

        return False

    def _monitor_loop(self) -> None:
        """Main background loop monitoring notifications."""
        # Try WinRT native listener first if available
        winrt_available = False
        try:
            import winrt.windows.ui.notifications.management as winrt_mgmt
            winrt_available = True
            logger.info("[Watchers] WinRT UserNotificationListener module detected.")
        except Exception:
            winrt_available = False

        while self._running:
            try:
                notifications = self._fetch_notifications(winrt_available)
                with self._lock:
                    active_watches = dict(self.watched_contacts)

                if active_watches and notifications:
                    for notif in notifications:
                        nid = notif.get("id", "")
                        app = notif.get("app", "")
                        title = notif.get("title", "")
                        body = notif.get("body", "")

                        if nid and nid in self._seen_notification_ids:
                            continue
                        if nid:
                            self._seen_notification_ids.add(nid)
                            if len(self._seen_notification_ids) > 1000:
                                self._seen_notification_ids.clear()

                        # Check against watched contacts
                        for contact, app_filters in active_watches.items():
                            app_match = True
                            if app_filters:
                                app_match = any(af in app.lower() for af in app_filters)
                            if not app_match:
                                continue

                            # Match contact in title or body
                            matched = self._matches_contact(contact, title) or self._matches_contact(contact, body)
                            if matched:
                                sender = title if title else contact
                                preview = body if body else title
                                alert = ContactMessageAlert(
                                    contact=contact,
                                    app=app,
                                    sender=sender,
                                    preview=preview,
                                )
                                logger.info(f"[Watchers] 🔔 Matched contact notification: {alert}")
                                if self.alert_callback:
                                    try:
                                        self.alert_callback(alert)
                                    except Exception as cb_err:
                                        logger.error(f"[Watchers] Alert callback error: {cb_err}")
            except Exception as e:
                logger.debug(f"[Watchers] Monitor loop note: {e}")

            time.sleep(1.5)

    def _fetch_notifications(self, winrt_available: bool) -> List[Dict[str, str]]:
        """Fetch current notifications using notification_reader or fallback."""
        if not _IS_WINDOWS:
            return []

        try:
            from actions.notification_reader import NotificationReader
            reader = NotificationReader()
            ok, notifs, _ = reader._query_winrt_notifications()
            if ok and notifs:
                return [
                    {
                        "id": getattr(n, "id", str(hash((n.title, n.body)))),
                        "app": getattr(n, "app_name", ""),
                        "title": getattr(n, "title", ""),
                        "body": getattr(n, "body", ""),
                    }
                    for n in notifs
                ]
        except Exception:
            pass

        return []


# ── Google Antigravity Task Watcher ──────────────────────────────────────────

@dataclass
class AntigravityStatusAlert:
    status: str  # "completed" | "waiting_approval" | "running"
    detail: str
    timestamp: float = field(default_factory=time.time)


class AntigravityTaskWatcher:
    """
    Monitors active Antigravity CLI and IDE processes:
    - Window title changes of Antigravity.exe via Win32 hooks
    - ~/.gemini/antigravity-cli/ logs via watchdog (with mtime polling fallback)
    Emits alerts when Antigravity completes a task or pauses waiting for terminal/review approval.
    """

    def __init__(self, alert_callback: Optional[Callable[[AntigravityStatusAlert], None]] = None):
        self.alert_callback = alert_callback
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_state: Optional[str] = None
        self._last_log_mtime: float = 0.0
        self._last_log_pos: int = 0
        self._antigravity_log_dir = Path.home() / ".gemini" / "antigravity-cli"

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="antigravity-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _monitor_loop(self) -> None:
        while self._running:
            try:
                # 1. Check Window Titles
                win_state, win_detail = self._check_window_titles()
                if win_state and win_state != self._last_state:
                    self._last_state = win_state
                    alert = AntigravityStatusAlert(status=win_state, detail=win_detail)
                    logger.info(f"[Watchers] 🚀 Antigravity window alert: {alert}")
                    if self.alert_callback:
                        self.alert_callback(alert)

                # 2. Check CLI log updates
                log_state, log_detail = self._check_cli_logs()
                if log_state and log_state != self._last_state:
                    self._last_state = log_state
                    alert = AntigravityStatusAlert(status=log_state, detail=log_detail)
                    logger.info(f"[Watchers] 📄 Antigravity log alert: {alert}")
                    if self.alert_callback:
                        self.alert_callback(alert)

            except Exception as e:
                logger.debug(f"[Watchers] Antigravity check error: {e}")

            time.sleep(1.8)

    def _check_window_titles(self) -> Tuple[Optional[str], str]:
        """Inspect window titles for Antigravity state transitions."""
        if not _IS_WINDOWS:
            return None, ""

        found_state = None
        found_detail = ""

        try:
            import win32gui

            def _enum_win_cb(hwnd: int, _: Any) -> bool:
                nonlocal found_state, found_detail
                if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd)
                if not title or "antigravity" not in title.lower():
                    return True

                t_lower = title.lower()
                if any(w in t_lower for w in ["waiting for approval", "review required", "approval required", "confirm"]):
                    found_state = "waiting_approval"
                    found_detail = f"Antigravity window '{title}' is waiting for user approval."
                    return False
                elif any(w in t_lower for w in ["task completed", "finished", "all tasks complete"]):
                    found_state = "completed"
                    found_detail = f"Antigravity window '{title}' finished task."
                    return False
                return True

            win32gui.EnumWindows(_enum_win_cb, None)
        except Exception:
            pass

        return found_state, found_detail

    def _check_cli_logs(self) -> Tuple[Optional[str], str]:
        """Inspect recent Antigravity log directory for completion or approval pause."""
        log_dir = self._antigravity_log_dir
        if not log_dir.exists():
            # Check fallback ~/.gemini/antigravity
            alt_dir = Path.home() / ".gemini" / "antigravity" / "logs"
            if alt_dir.exists():
                log_dir = alt_dir
            else:
                return None, ""

        try:
            # Find newest log file
            log_files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not log_files:
                return None, ""

            newest = log_files[0]
            st = newest.stat()
            if st.st_mtime <= self._last_log_mtime:
                return None, ""

            self._last_log_mtime = st.st_mtime
            # Read tail of log file
            with open(newest, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()[-3000:].lower()

            if any(k in content for k in ["waiting for approval", "approval required", "paused waiting", "user confirmation required"]):
                return "waiting_approval", f"Antigravity log indicates task paused waiting for review: {newest.name}"
            elif any(k in content for k in ["task completed successfully", "agent execution finished", "workflow completed"]):
                return "completed", f"Antigravity log indicates task completed: {newest.name}"
        except Exception:
            pass

        return None, ""


# ── Watcher Unified Manager ──────────────────────────────────────────────────

class WatcherService:
    """Central service managing all background watchers and feeding alerts to Main Key."""

    _instance: Optional[WatcherService] = None

    def __init__(self, live_session: Any = None, player: Any = None):
        self.live_session = live_session
        self.player = player
        self.contact_watcher = ContactNotificationWatcher(self._on_contact_alert)
        self.antigravity_watcher = AntigravityTaskWatcher(self._on_antigravity_alert)

    @classmethod
    def get_instance(cls, live_session: Any = None, player: Any = None) -> WatcherService:
        if cls._instance is None:
            cls._instance = WatcherService(live_session=live_session, player=player)
        else:
            if live_session:
                cls._instance.live_session = live_session
            if player:
                cls._instance.player = player
        return cls._instance

    def start(self) -> None:
        self.contact_watcher.start()
        self.antigravity_watcher.start()
        logger.info("[WatcherService] All event watchers started.")

    def stop(self) -> None:
        self.contact_watcher.stop()
        self.antigravity_watcher.stop()

    def watch_contact_messages(self, contact_name: str, app_filter: Optional[List[str]] = None) -> Dict[str, Any]:
        self.contact_watcher.add_watch(contact_name, app_filter)
        self.contact_watcher.start()
        return {
            "status": "watching",
            "contact": contact_name,
            "app_filter": app_filter or ["WhatsApp", "Edge"],
            "message": f"Now actively watching for incoming messages from '{contact_name}'.",
        }

    def watch_antigravity_task(self) -> Dict[str, Any]:
        self.antigravity_watcher.start()
        return {
            "status": "watching",
            "message": "Actively watching Google Antigravity agent process and logs for completion or approval requests.",
        }

    def _on_contact_alert(self, alert: ContactMessageAlert) -> None:
        """Inject alert into Main Key session and display UI toast."""
        spoken_msg = f"Sir, you have a new message from {alert.sender}: '{alert.preview}'. How would you like me to respond?"
        ui_msg = f"From {alert.sender} ({alert.app}): {alert.preview}"

        if self.player:
            try:
                self.player.write_log(f"ALERT: Message from {alert.sender}: {alert.preview}")
                if hasattr(self.player, "show_slide_alert"):
                    self.player.show_slide_alert(ui_msg, title="CONTACT MESSAGE")
            except Exception:
                pass

        # Speak via Main Key voice session
        self._speak_alert(spoken_msg)

    def _on_antigravity_alert(self, alert: AntigravityStatusAlert) -> None:
        """Inject Antigravity status alert into Main Key session."""
        if alert.status == "waiting_approval":
            spoken_msg = "Sir, the Google Antigravity agent is paused waiting for your terminal or review approval."
            ui_title = "ANTIGRAVITY: APPROVAL NEEDED"
        else:
            spoken_msg = "Sir, the Google Antigravity agent has finished its task."
            ui_title = "ANTIGRAVITY: TASK COMPLETED"

        if self.player:
            try:
                self.player.write_log(f"ALERT: {alert.detail}")
                if hasattr(self.player, "show_slide_alert"):
                    self.player.show_slide_alert(alert.detail, title=ui_title)
            except Exception:
                pass

        self._speak_alert(spoken_msg)

    def _speak_alert(self, spoken_text: str) -> None:
        if self.live_session and hasattr(self.live_session, "speak"):
            try:
                self.live_session.speak(spoken_text)
                return
            except Exception:
                pass

        # Fallback to platform_hooks
        try:
            from core import platform_hooks
            spk_fn = platform_hooks.get_hook("speak")
            if spk_fn:
                spk_fn(spoken_text)
        except Exception:
            pass
