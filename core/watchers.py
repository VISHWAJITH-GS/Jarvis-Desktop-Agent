"""
core/watchers.py — Event-Driven Background Watchers for Contact Messages & Antigravity Agent.

Provides:
1. Contact Notification Watcher (ContactWatcher / ContactNotificationWatcher):
   - Real-time toast notifications via Windows Runtime (WinRT UserNotificationListener & NotificationKinds.TOAST)
   - Robust polling fallback via Windows Notification SQLite database (wpndatabase.db) and UIA Action Center
   - Case-insensitive fuzzy matching (rapidfuzz / difflib) against watched contacts and app filters (WhatsApp, Edge, etc.)
   - High-priority proactive voice interrupt injection into active Gemini Live session
   - One-shot auto-removal and thread-safe HUD watcher badge signaling
2. Google Antigravity Task Watcher (AntigravityWatcher / AntigravityTaskWatcher):
   - Dual-signal monitoring:
     * Filesystem / session logs via watchdog.observers.Observer (monitoring ~/.gemini/antigravity-cli/,
       ~/.gemini/antigravity/, and workspace .gemini logs) for COMPLETED, FAILED, and WAITING_FOR_APPROVAL transitions
     * Window title and UI Automation (uiautomation) monitoring for prompts like "Request Review",
       "Walkthrough", "Approve", or "Run command"
   - Proactive verbal alert injection into Gemini Live session
3. Watcher Unified Manager (WatcherManager / WatcherService):
   - Central coordinator orchestrating watchers, state caching, deduplication, and thread-safe HUD updates
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import os
import platform
import re
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("watchers")
_IS_WINDOWS = platform.system() == "Windows"

# Fuzzy matching support (rapidfuzz preferred, difflib fallback)
try:
    from rapidfuzz import fuzz

    def _fuzzy_match_score(query: str, text: str) -> float:
        if not query or not text:
            return 0.0
        q = query.lower().strip()
        t = text.lower().strip()
        if q in t:
            return 1.0
        r_direct = fuzz.ratio(q, t)
        r_partial = fuzz.partial_ratio(q, t)
        return max(r_direct, r_partial) / 100.0

except ImportError:
    import difflib

    def _fuzzy_match_score(query: str, text: str) -> float:
        if not query or not text:
            return 0.0
        q = query.lower().strip()
        t = text.lower().strip()
        if q in t:
            return 1.0
        return difflib.SequenceMatcher(None, q, t).ratio()


# ── Data Models ───────────────────────────────────────────────────────────────

@dataclass
class ContactMessageAlert:
    contact: str
    app: str
    sender: str
    preview: str
    body: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.body:
            self.body = self.preview


@dataclass
class WatchedContactRule:
    contact_name: str
    app_filter: List[str] = field(default_factory=lambda: ["whatsapp", "edge"])
    one_shot: bool = True
    created_at: float = field(default_factory=time.time)

    def matches_app(self, app_name: str) -> bool:
        if not self.app_filter:
            return True
        app_lower = app_name.lower().strip()
        return any(f.lower().strip() in app_lower for f in self.app_filter)


@dataclass
class AntigravityStatusAlert:
    status: str  # "completed" | "waiting_approval" | "failed" | "running"
    detail: str
    timestamp: float = field(default_factory=time.time)


# ── Contact Message Watcher ───────────────────────────────────────────────────

class ContactWatcher:
    """
    Subscribes to system toast notifications in real-time using Windows Runtime APIs
    (UserNotificationListener) with clean polling fallbacks via Windows Notification DB
    and UI Automation. When an incoming message matches a watched contact name (fuzzy match),
    triggers voice interrupts and HUD badge updates.
    """

    def __init__(self, alert_callback: Optional[Callable[[ContactMessageAlert], None]] = None):
        self.alert_callback = alert_callback
        self.watched_contacts: Dict[str, WatchedContactRule] = {}
        self._seen_notification_ids: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._seen_hashes: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._max_cache_size = 1000
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._winrt_listener: Any = None
        self._winrt_token: Any = None
        self._winrt_available = False
        self._db_path = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db")

    def add_watch(
        self,
        contact_name: str,
        app_filter: Optional[List[str]] = None,
        one_shot: bool = True,
    ) -> None:
        """Add or update a watched contact rule."""
        c_clean = contact_name.strip()
        if not c_clean:
            return

        if app_filter is None:
            apps = ["whatsapp", "edge"]
        elif isinstance(app_filter, str):
            apps = [a.strip().lower() for a in app_filter.split(",") if a.strip()]
        else:
            apps = [str(a).strip().lower() for a in app_filter if str(a).strip()]

        rule = WatchedContactRule(contact_name=c_clean, app_filter=apps, one_shot=one_shot)
        with self._lock:
            self.watched_contacts[c_clean] = rule

        logger.info(f"[ContactWatcher] Watching contact='{c_clean}' apps={apps} one_shot={one_shot}")

    def remove_watch(self, contact_name: str) -> bool:
        """Remove a contact from the active watch list."""
        c_clean = contact_name.strip()
        with self._lock:
            # Case-insensitive removal
            keys_to_remove = [k for k in self.watched_contacts if k.lower() == c_clean.lower()]
            for k in keys_to_remove:
                self.watched_contacts.pop(k, None)
            return len(keys_to_remove) > 0

    def clear_watches(self) -> None:
        with self._lock:
            self.watched_contacts.clear()

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="contact-watcher")
            self._thread.start()
            logger.info("[ContactWatcher] Service loop started.")

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._detach_winrt_listener()

    def _matches_contact(self, query_contact: str, text: str) -> bool:
        """
        Case-insensitive fuzzy match for contact name.
        Matches if contact is exact substring, word boundary token, or fuzzy ratio >= 0.82.
        """
        if not query_contact or not text:
            return False

        qc = query_contact.lower().strip()
        t = text.lower()

        # 1. Direct substring
        if qc in t:
            return True

        # 2. Token / word boundary check
        tokens = re.findall(r"[\w']+", t)
        if qc in tokens:
            return True

        # 3. Fuzzy ratio match against tokens and full text
        for token in tokens:
            if len(token) >= 3 and _fuzzy_match_score(qc, token) >= 0.82:
                return True

        return _fuzzy_match_score(qc, t) >= 0.82

    def _is_duplicate(self, notif_id: str, app: str, title: str, body: str) -> bool:
        """Deduplicate notifications by ID and payload hash."""
        now = time.time()
        with self._lock:
            # Check by notification ID
            if notif_id and notif_id in self._seen_notification_ids:
                return True

            # Check by content hash
            content_key = f"{app.lower()}|{title.lower()}|{body.lower()}"
            h = hashlib.sha256(content_key.encode("utf-8", errors="ignore")).hexdigest()
            if h in self._seen_hashes:
                return True

            # Record in rolling caches
            if notif_id:
                self._seen_notification_ids[notif_id] = now
                if len(self._seen_notification_ids) > self._max_cache_size:
                    self._seen_notification_ids.popitem(last=False)

            self._seen_hashes[h] = now
            if len(self._seen_hashes) > self._max_cache_size:
                self._seen_hashes.popitem(last=False)

            return False

    def _monitor_loop(self) -> None:
        """Background thread running event listener or polling loop."""
        self._init_winrt_listener()

        while self._running:
            try:
                with self._lock:
                    has_watches = bool(self.watched_contacts)

                if has_watches:
                    notifs = self._fetch_notifications()
                    for item in notifs:
                        self._process_notification(item)
            except Exception as e:
                logger.debug(f"[ContactWatcher] Monitor loop error: {e}")

            time.sleep(1.2)

    def _init_winrt_listener(self) -> None:
        """Attempt to initialize WinRT UserNotificationListener and subscribe to events."""
        if not _IS_WINDOWS:
            return

        try:
            import winrt.windows.ui.notifications as n
            import winrt.windows.ui.notifications.management as m

            listener = m.UserNotificationListener.current
            self._winrt_listener = listener
            self._winrt_available = True

            # Check access status
            status = listener.get_access_status()
            logger.info(f"[ContactWatcher] WinRT UserNotificationListener access status: {status}")

            # Try to register native event callback
            try:
                def _winrt_callback(sender: Any, args: Any) -> None:
                    try:
                        nid = getattr(args, "user_notification_id", None)
                        if nid is not None and self._running:
                            notif = sender.get_notification(nid)
                            if notif:
                                parsed = self._parse_winrt_notification(notif)
                                if parsed:
                                    self._process_notification(parsed)
                    except Exception as cb_err:
                        logger.debug(f"[ContactWatcher] WinRT event dispatch error: {cb_err}")

                self._winrt_token = listener.add_notification_changed(_winrt_callback)
                logger.info("[ContactWatcher] Subscribed to native WinRT notification_changed events.")
            except Exception as ev_err:
                logger.info(f"[ContactWatcher] WinRT event subscription unavailable ({ev_err}), falling back to API polling.")
                self._winrt_token = None
        except Exception as init_err:
            logger.info(f"[ContactWatcher] WinRT notifications module unavailable ({init_err}), using SQLite DB fallback.")
            self._winrt_available = False

    def _detach_winrt_listener(self) -> None:
        if self._winrt_listener and self._winrt_token:
            try:
                self._winrt_listener.remove_notification_changed(self._winrt_token)
            except Exception:
                pass
            self._winrt_token = None

    def _fetch_notifications(self) -> List[Dict[str, str]]:
        """Fetch notifications using WinRT API -> SQLite DB -> Reader fallback."""
        if not _IS_WINDOWS:
            return []

        # 1. Try WinRT API directly
        if self._winrt_available and self._winrt_listener:
            try:
                import winrt.windows.ui.notifications as n
                # Run async get_notifications_async synchronously in background thread
                loop = asyncio.new_event_loop()
                try:
                    notifs = loop.run_until_complete(
                        self._winrt_listener.get_notifications_async(n.NotificationKinds.TOAST)
                    )
                    results = []
                    for notif in notifs:
                        parsed = self._parse_winrt_notification(notif)
                        if parsed:
                            results.append(parsed)
                    return results
                finally:
                    loop.close()
            except Exception as e:
                logger.debug(f"[ContactWatcher] WinRT get_notifications_async note: {e}")

        # 2. Clean fallback: Windows Notification SQLite Database (wpndatabase.db)
        if os.path.exists(self._db_path):
            try:
                return self._fetch_from_wpn_database()
            except Exception as e:
                logger.debug(f"[ContactWatcher] SQLite fallback note: {e}")

        # 3. Tertiary fallback: NotificationReader
        try:
            from actions.notification_reader import NotificationReader
            reader = NotificationReader()
            ok, notifs, _ = reader._query_winrt_notifications()
            if ok and notifs:
                return [
                    {
                        "id": getattr(n, "id", ""),
                        "app": getattr(n, "app_name", ""),
                        "title": getattr(n, "title", ""),
                        "body": getattr(n, "body", ""),
                    }
                    for n in notifs
                ]
        except Exception:
            pass

        return []

    def _parse_winrt_notification(self, notif: Any) -> Optional[Dict[str, str]]:
        """Extract id, app display name, title, and body from a WinRT UserNotification object."""
        try:
            import winrt.windows.ui.notifications as n

            nid = str(getattr(notif, "id", ""))
            app = ""
            try:
                app = notif.app_info.display_info.display_name
            except Exception:
                app = "Windows"

            title = ""
            body = ""
            visual = getattr(getattr(notif, "notification", None), "visual", None)
            if visual:
                binding = visual.get_binding(n.KnownNotificationBindings.toast_generic)
                if binding:
                    texts = binding.get_text_elements()
                    text_vals = [t.text.strip() for t in texts if t and t.text and t.text.strip()]
                    if text_vals:
                        title = text_vals[0]
                    if len(text_vals) > 1:
                        body = " — ".join(text_vals[1:])

            return {"id": nid, "app": app, "title": title, "body": body}
        except Exception as e:
            logger.debug(f"[ContactWatcher] WinRT parse error: {e}")
            return None

    def _fetch_from_wpn_database(self) -> List[Dict[str, str]]:
        """Query read-only SQLite database wpndatabase.db for active toasts."""
        results: List[Dict[str, str]] = []
        conn = None
        try:
            uri = f"file:{self._db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=1.0)
            c = conn.cursor()
            c.execute(
                """
                SELECT n.Id, h.PrimaryId, n.Payload, n.ArrivalTime
                FROM Notification n
                LEFT JOIN NotificationHandler h ON n.HandlerId = h.RecordId
                WHERE n.Type = 'toast'
                ORDER BY n.ArrivalTime DESC
                LIMIT 25
                """
            )
            rows = c.fetchall()
            for row in rows:
                nid, handler, payload, _ = row
                xml_str = payload.decode("utf-8", errors="ignore") if payload else ""
                title, body = "", ""
                if xml_str:
                    try:
                        root = ET.fromstring(xml_str)
                        texts = [elem.text.strip() for elem in root.iter("text") if elem.text and elem.text.strip()]
                        if texts:
                            title = texts[0]
                        if len(texts) > 1:
                            body = " — ".join(texts[1:])
                    except Exception:
                        pass

                # Derive human-friendly app name from handler ID
                app = self._friendly_app_name(str(handler or ""))
                results.append({"id": str(nid), "app": app, "title": title, "body": body})
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        return results

    def _friendly_app_name(self, raw_handler: str) -> str:
        """Map Windows package/handler IDs to friendly names like WhatsApp, Edge, etc."""
        rh = raw_handler.lower()
        if "whatsapp" in rh:
            return "WhatsApp"
        if "microsoftedge" in rh or "msedge" in rh or "edge" in rh:
            return "Edge"
        if "telegram" in rh:
            return "Telegram"
        if "slack" in rh:
            return "Slack"
        if "discord" in rh:
            return "Discord"
        if "code" in rh or "antigravity" in rh:
            return "Antigravity"
        if "chrome" in rh:
            return "Chrome"
        return raw_handler.split("!")[-1].split("_")[0] if raw_handler else "Windows"

    def _process_notification(self, notif: Dict[str, str]) -> None:
        """Check a notification against active watch rules and dispatch alert on match."""
        nid = notif.get("id", "")
        app = notif.get("app", "")
        title = notif.get("title", "")
        body = notif.get("body", "")

        if not title and not body:
            return

        if self._is_duplicate(nid, app, title, body):
            return

        with self._lock:
            active_rules = list(self.watched_contacts.values())

        for rule in active_rules:
            # 1. Filter by application
            if not rule.matches_app(app):
                continue

            # 2. Fuzzy match contact against title and body
            matched = self._matches_contact(rule.contact_name, title) or self._matches_contact(rule.contact_name, body)
            if matched:
                sender = title if title else rule.contact_name
                preview = body if body else title
                alert = ContactMessageAlert(
                    contact=rule.contact_name,
                    app=app,
                    sender=sender,
                    preview=preview,
                    body=body,
                )

                # 3. Handle one_shot removal
                if rule.one_shot:
                    self.remove_watch(rule.contact_name)

                # 4. Dispatch callback to WatcherManager
                if self.alert_callback:
                    try:
                        self.alert_callback(alert)
                    except Exception as cb_err:
                        logger.error(f"[ContactWatcher] Alert callback error: {cb_err}")


# ── Google Antigravity Task Watcher ──────────────────────────────────────────

class AntigravityWatcher:
    """
    Dual-signal monitor for Google Antigravity coding agents:
    1. Filesystem / Session Logs: Monitors ~/.gemini/antigravity-cli/, ~/.gemini/antigravity/,
       and active workspace .gemini logs via watchdog (with mtime polling fallback) for
       COMPLETED, FAILED, and WAITING_FOR_APPROVAL transitions.
    2. Window & UI State: Monitors Antigravity.exe window titles and UI Automation controls
       via uiautomation for prompts like 'Request Review', 'Walkthrough', 'Approve', 'Run command'.
    """

    def __init__(self, alert_callback: Optional[Callable[[AntigravityStatusAlert], None]] = None):
        self.alert_callback = alert_callback
        self.is_active = False
        self.workspace_hint = ""
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._last_state: Optional[str] = None
        self._seen_hashes: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._observer: Any = None
        self._file_positions: Dict[str, int] = {}
        self._log_dirs: List[Path] = []

    def watch(self, workspace_hint: str = "") -> None:
        """Start actively watching Antigravity agent process, UI, and logs."""
        with self._lock:
            self.workspace_hint = workspace_hint.strip()
            self.is_active = True
            self._setup_log_dirs()
            if not self._running:
                self._running = True
                self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="antigravity-watcher")
                self._thread.start()
                self._start_filesystem_observer()

        logger.info(f"[AntigravityWatcher] Watching active (workspace_hint='{self.workspace_hint}')")

    def stop_watching(self) -> None:
        """Stop watching Antigravity agent."""
        with self._lock:
            self.is_active = False
            self._running = False
            self._stop_filesystem_observer()
        logger.info("[AntigravityWatcher] Watching stopped.")

    start = watch
    stop = stop_watching

    def _setup_log_dirs(self) -> None:
        """Build list of candidate Antigravity log directories."""
        home = Path.home()
        candidates = [
            home / ".gemini" / "antigravity-cli",
            home / ".gemini" / "antigravity" / "logs",
            home / ".gemini" / "antigravity",
            home / ".gemini" / "logs",
        ]

        if self.workspace_hint:
            w_path = Path(self.workspace_hint)
            candidates.extend([w_path / ".gemini", w_path / ".gemini" / "logs"])

        # Also add current directory .gemini
        candidates.append(Path.cwd() / ".gemini")

        self._log_dirs = [d for d in candidates if d.exists()]

    def _start_filesystem_observer(self) -> None:
        """Initialize and start watchdog observer on available log directories."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class _LogChangeHandler(FileSystemEventHandler):
                def __init__(self, watcher_ref: AntigravityWatcher):
                    self.watcher = watcher_ref

                def on_modified(self, event: Any) -> None:
                    if not event.is_directory:
                        self.watcher._on_file_changed(event.src_path)

                def on_created(self, event: Any) -> None:
                    if not event.is_directory:
                        self.watcher._on_file_changed(event.src_path)

            self._observer = Observer()
            handler = _LogChangeHandler(self)

            dirs_watched = 0
            for d in self._log_dirs:
                if d.exists() and d.is_dir():
                    try:
                        self._observer.schedule(handler, str(d), recursive=False)
                        dirs_watched += 1
                    except Exception:
                        pass

            if dirs_watched > 0:
                self._observer.start()
                logger.info(f"[AntigravityWatcher] Watchdog observer started for {dirs_watched} directories.")
        except Exception as e:
            logger.info(f"[AntigravityWatcher] Watchdog observer init note: {e}")
            self._observer = None

    def _stop_filesystem_observer(self) -> None:
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=1.0)
            except Exception:
                pass
            self._observer = None

    def _on_file_changed(self, file_path_str: str) -> None:
        """Called by watchdog when a log file is modified or created."""
        if not self.is_active:
            return

        p = Path(file_path_str)
        if not p.suffix.lower() in [".log", ".jsonl", ".txt", ".pbtxt"]:
            return

        try:
            pos = self._file_positions.get(file_path_str, 0)
            if not p.exists():
                return
            size = p.stat().st_size
            if size <= pos:
                return

            with open(p, "r", encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                new_chunk = f.read()
                self._file_positions[file_path_str] = f.tell()

            st, detail = self._parse_log_chunk(new_chunk, p.name)
            if st:
                self._trigger_alert(st, detail)
        except Exception as e:
            logger.debug(f"[AntigravityWatcher] Error reading file change: {e}")

    def _parse_log_chunk(self, content: str, filename: str) -> Tuple[Optional[str], str]:
        """Detect transitions in log chunk: COMPLETED, WAITING_FOR_APPROVAL, FAILED."""
        if not content:
            return None, ""

        c = content.lower()

        # Approval prompts
        if any(
            k in c
            for k in [
                "waiting_for_approval",
                "waiting for approval",
                "approval required",
                "review required",
                "paused waiting",
                "user confirmation required",
                "request review",
                "run command",
            ]
        ):
            return "waiting_approval", f"Antigravity log ({filename}) requires user review or approval."

        # Completion
        if any(
            k in c
            for k in [
                "completed",
                "task completed",
                "all tasks complete",
                "finished",
                "agent execution finished",
                "workflow completed successfully",
            ]
        ):
            return "completed", f"Antigravity log ({filename}) indicates task finished."

        # Failure
        if any(k in c for k in ["failed", "task failed", "error encountered", "fatal error"]):
            return "failed", f"Antigravity log ({filename}) indicates task failed."

        return None, ""

    def _monitor_loop(self) -> None:
        """Periodic background checks for window titles, UI Automation controls, and log tail."""
        while self._running:
            if self.is_active:
                try:
                    # 1. Check window titles
                    win_st, win_det = self._check_window_titles()
                    if win_st:
                        self._trigger_alert(win_st, win_det)

                    # 2. Check UI Automation status changes
                    uia_st, uia_det = self._check_ui_controls()
                    if uia_st:
                        self._trigger_alert(uia_st, uia_det)

                    # 3. Polling check on CLI logs (fallback if watchdog didn't catch)
                    log_st, log_det = self._check_cli_logs()
                    if log_st:
                        self._trigger_alert(log_st, log_det)

                except Exception as e:
                    logger.debug(f"[AntigravityWatcher] Monitor loop check note: {e}")

            time.sleep(1.8)

    def _trigger_alert(self, status: str, detail: str) -> None:
        """Deduplicate and dispatch Antigravity status alerts."""
        h = hashlib.sha256(f"{status}|{detail}".encode("utf-8", errors="ignore")).hexdigest()
        now = time.time()

        with self._lock:
            # Deduplicate if fired within last 10 seconds or identical state
            if status == self._last_state and h in self._seen_hashes and (now - self._seen_hashes[h] < 12.0):
                return

            self._last_state = status
            self._seen_hashes[h] = now
            if len(self._seen_hashes) > 500:
                self._seen_hashes.popitem(last=False)

        alert = AntigravityStatusAlert(status=status, detail=detail)
        logger.info(f"[AntigravityWatcher] Alert triggered: {alert}")
        if self.alert_callback:
            try:
                self.alert_callback(alert)
            except Exception as cb_err:
                logger.error(f"[AntigravityWatcher] Alert callback error: {cb_err}")

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
                if not title:
                    return True
                t_lower = title.lower()
                if "antigravity" not in t_lower:
                    return True

                if any(
                    w in t_lower
                    for w in [
                        "waiting for approval",
                        "review required",
                        "approval required",
                        "request review",
                        "confirm",
                        "walkthrough",
                    ]
                ):
                    found_state = "waiting_approval"
                    found_detail = f"Antigravity window '{title}' is waiting for user approval."
                    return False
                elif any(w in t_lower for w in ["task completed", "finished", "all tasks complete"]):
                    found_state = "completed"
                    found_detail = f"Antigravity window '{title}' finished task."
                    return False
                elif any(w in t_lower for w in ["failed", "error"]):
                    found_state = "failed"
                    found_detail = f"Antigravity window '{title}' encountered an error."
                    return False
                return True

            win32gui.EnumWindows(_enum_win_cb, None)
        except Exception:
            pass

        return found_state, found_detail

    def _check_ui_controls(self) -> Tuple[Optional[str], str]:
        """Use uiautomation to inspect buttons/dialogs inside Antigravity windows."""
        if not _IS_WINDOWS:
            return None, ""

        try:
            import uiautomation as auto

            root = auto.GetRootControl()
            if not root:
                return None, ""

            # Check for top-level windows matching Antigravity
            for win in root.GetChildren():
                name = getattr(win, "Name", "") or ""
                cls_name = getattr(win, "ClassName", "") or ""
                if "antigravity" in name.lower() or "antigravity" in cls_name.lower():
                    # Check for approval prompts/buttons inside this window
                    for prompt_word in ["Request Review", "Walkthrough", "Approve", "Run command", "Allow"]:
                        btn = win.ButtonControl(searchDepth=6, SubName=prompt_word)
                        if btn and btn.Exists(0, 0):
                            return "waiting_approval", f"Antigravity UI prompt '{prompt_word}' waiting for user approval."

                    # Check for completion badge
                    done_elem = win.TextControl(searchDepth=6, SubName="Task Completed")
                    if done_elem and done_elem.Exists(0, 0):
                        return "completed", "Antigravity UI reports task completed."
        except Exception:
            pass

        return None, ""

    def _check_cli_logs(self) -> Tuple[Optional[str], str]:
        """Inspect recent Antigravity log files for completion or approval state."""
        if not self._log_dirs:
            self._setup_log_dirs()

        for log_dir in self._log_dirs:
            if not log_dir.exists():
                continue
            try:
                log_files = sorted(
                    [p for p in log_dir.glob("*.log")] + [p for p in log_dir.glob("*.jsonl")],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if not log_files:
                    continue

                newest = log_files[0]
                with open(newest, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()[-3000:]
                st, det = self._parse_log_chunk(content, newest.name)
                if st:
                    return st, det
            except Exception:
                pass

        return None, ""


# ── Watcher Unified Manager ──────────────────────────────────────────────────

class WatcherManager:
    """
    Central service managing all background watchers, injecting high-priority
    alerts into the active Gemini Live session, and driving HUD watcher badge signals.
    """

    _instance: Optional[WatcherManager] = None

    def __init__(self, live_session: Any = None, player: Any = None):
        self.live_session = live_session
        self.player = player
        self.contact_watcher = ContactWatcher(self._on_contact_alert)
        self.antigravity_watcher = AntigravityWatcher(self._on_antigravity_alert)

    @classmethod
    def get_instance(cls, live_session: Any = None, player: Any = None) -> WatcherManager:
        if cls._instance is None:
            cls._instance = WatcherManager(live_session=live_session, player=player)
        else:
            if live_session is not None:
                cls._instance.live_session = live_session
            if player is not None:
                cls._instance.player = player
        return cls._instance

    def start(self) -> None:
        """Start watcher services on app boot."""
        self.contact_watcher.start()
        # Antigravity watcher background loop starts when watch_antigravity_task is called
        logger.info("[WatcherManager] Background watcher services initialized.")

    def stop(self) -> None:
        """Cleanly shutdown all watchers."""
        self.contact_watcher.stop()
        self.antigravity_watcher.stop()
        self.update_hud_badge()
        logger.info("[WatcherManager] Background watcher services stopped.")

    # ── Tool Handlers ────────────────────────────────────────────────────────

    def watch_contact_messages(
        self,
        contact_name: str,
        app_filter: Optional[List[str]] = None,
        one_shot: bool = True,
    ) -> Dict[str, Any]:
        """Expose watch_contact_messages tool."""
        self.contact_watcher.add_watch(contact_name, app_filter=app_filter, one_shot=one_shot)
        self.contact_watcher.start()
        self.update_hud_badge()
        effective_filter = app_filter if app_filter else ["WhatsApp", "Edge"]
        return {
            "status": "watching",
            "contact": contact_name,
            "app_filter": effective_filter,
            "one_shot": one_shot,
            "message": f"Now actively watching for incoming messages from '{contact_name}' on {effective_filter}.",
        }

    def stop_watching_contact(self, contact_name: str) -> Dict[str, Any]:
        """Expose stop_watching_contact tool."""
        removed = self.contact_watcher.remove_watch(contact_name)
        self.update_hud_badge()
        return {
            "status": "stopped" if removed else "not_found",
            "contact": contact_name,
            "message": f"Stopped watching messages for '{contact_name}'."
            if removed
            else f"Contact '{contact_name}' was not actively watched.",
        }

    def watch_antigravity_task(self, workspace_hint: str = "") -> Dict[str, Any]:
        """Expose watch_antigravity_task tool."""
        self.antigravity_watcher.watch(workspace_hint=workspace_hint)
        self.update_hud_badge()
        return {
            "status": "watching",
            "workspace_hint": workspace_hint,
            "message": "Actively watching Google Antigravity agent process, UI, and logs for completion or approval requests.",
        }

    def stop_watching_antigravity(self) -> Dict[str, Any]:
        """Expose stop_watching_antigravity tool."""
        self.antigravity_watcher.stop_watching()
        self.update_hud_badge()
        return {
            "status": "stopped",
            "message": "Stopped watching Google Antigravity agent.",
        }

    # ── Alerts & Live Session Interruption ───────────────────────────────────

    def _on_contact_alert(self, alert: ContactMessageAlert) -> None:
        """Handle contact notification match: inject live session interrupt and update UI."""
        # Ensure one-shot watch rule is removed if not already removed by processor
        with self.contact_watcher._lock:
            rule = self.contact_watcher.watched_contacts.get(alert.contact)
            if rule and rule.one_shot:
                self.contact_watcher.remove_watch(alert.contact)

        # 1. Update HUD badge
        self.update_hud_badge()

        # 2. Format mandatory interrupt alert prompt
        spoken_interrupt = (
            f"Interrupt alert: New {alert.app} message from {alert.contact}: '{alert.body}'. "
            f"Read this to the user and ask what action or reply they want to take."
        )
        ui_msg = f"From {alert.sender} ({alert.app}): {alert.body}"

        # 3. Log to player HUD
        if self.player:
            try:
                self.player.write_log(f"ALERT: Message from {alert.sender}: {alert.body}")
                if hasattr(self.player, "show_slide_alert"):
                    self.player.show_slide_alert(ui_msg, title="CONTACT MESSAGE ALERT")
            except Exception:
                pass

        # 4. Inject interrupt into active Gemini Live session
        self.inject_live_interrupt(spoken_interrupt)

    def _on_antigravity_alert(self, alert: AntigravityStatusAlert) -> None:
        """Handle Antigravity status transition: inject live session interrupt and update UI."""
        if alert.status == "waiting_approval":
            spoken_interrupt = "Antigravity update: Your background coding agent has paused waiting for your approval."
            ui_title = "ANTIGRAVITY: APPROVAL REQUIRED"
        elif alert.status == "failed":
            spoken_interrupt = "Antigravity update: Your background coding agent has failed its task."
            ui_title = "ANTIGRAVITY: TASK FAILED"
        else:
            spoken_interrupt = "Antigravity update: Your background coding agent has finished its task."
            ui_title = "ANTIGRAVITY: TASK COMPLETED"

        # 1. Log to player HUD
        if self.player:
            try:
                self.player.write_log(f"ALERT: {alert.detail}")
                if hasattr(self.player, "show_slide_alert"):
                    self.player.show_slide_alert(alert.detail, title=ui_title)
            except Exception:
                pass

        # 2. Inject interrupt into active Gemini Live session
        self.inject_live_interrupt(spoken_interrupt)

    def inject_live_interrupt(self, prompt_text: str) -> bool:
        """
        Immediately inject a high-priority system/client_content message frame into the active
        Gemini Live session so Jarvis speaks the alert aloud.
        """
        session = getattr(self.live_session, "session", None)
        loop = getattr(self.live_session, "_loop", None)

        # 1. Primary: Gemini Live session send_client_content
        if session and loop and not loop.is_closed():
            async def _send_frame():
                if hasattr(session, "send_client_content"):
                    try:
                        await session.send_client_content(
                            turns={"role": "user", "parts": [{"text": prompt_text}]},
                            turn_complete=True,
                        )
                        return True
                    except Exception as e:
                        logger.debug(f"[WatcherManager] send_client_content note: {e}")

                if hasattr(session, "send"):
                    try:
                        await session.send(text=prompt_text, end_of_turn=True)
                        return True
                    except Exception as e:
                        logger.debug(f"[WatcherManager] session.send note: {e}")
                return False

            try:
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(_send_frame(), loop)
                else:
                    loop.run_until_complete(_send_frame())
                return True
            except Exception as sched_err:
                logger.debug(f"[WatcherManager] Failed to schedule session injection: {sched_err}")

        # 2. Fallback to live_session.speak
        if self.live_session and hasattr(self.live_session, "speak"):
            try:
                self.live_session.speak(prompt_text)
                return True
            except Exception:
                pass

        # 3. Fallback to platform_hooks speak
        try:
            from core import platform_hooks
            spk = platform_hooks.get_hook("speak")
            if spk:
                spk(prompt_text)
                return True
        except Exception:
            pass

        return False

    def update_hud_badge(self) -> None:
        """Emit thread-safe Qt signal to update the active watcher badge on the HUD."""
        if not self.player:
            return

        badges = []
        with self.contact_watcher._lock:
            contacts = list(self.contact_watcher.watched_contacts.keys())

        if contacts:
            if len(contacts) == 1:
                badges.append(f"👁 MSG: {contacts[0]}")
            else:
                badges.append(f"👁 MSG: {contacts[0]} +{len(contacts)-1}")

        if self.antigravity_watcher.is_active:
            badges.append("👁 ANTIGRAVITY")

        if badges:
            text = " | ".join(badges)
            if hasattr(self.player, "set_watcher_badge"):
                self.player.set_watcher_badge(text, True)
        else:
            if hasattr(self.player, "set_watcher_badge"):
                self.player.set_watcher_badge("", False)


# ── Backwards Compatibility Aliases ──────────────────────────────────────────

WatcherService = WatcherManager
ContactNotificationWatcher = ContactWatcher
AntigravityTaskWatcher = AntigravityWatcher

__all__ = [
    "ContactMessageAlert",
    "WatchedContactRule",
    "AntigravityStatusAlert",
    "ContactWatcher",
    "ContactNotificationWatcher",
    "AntigravityWatcher",
    "AntigravityTaskWatcher",
    "WatcherManager",
    "WatcherService",
]
