"""
tests/test_watchers.py — Unit Tests for Event-Driven Background Watchers & Proactive Interrupts.

Tests:
1. ContactWatcher case-insensitive fuzzy matching & app filtering
2. ContactWatcher deduplication & one-shot auto-removal
3. Real-time toast payload parsing and Gemini Live voice interrupt injection
4. AntigravityWatcher log transition detection (COMPLETED, FAILED, WAITING_FOR_APPROVAL)
5. AntigravityWatcher UI Automation & window prompt monitoring
6. AntigravityWatcher deduplication & verbal interrupt formatting
7. WatcherManager tool dispatch, lifecycle management, and HUD badge signaling
8. SQLite database fallback parsing for Windows notifications
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.watchers import (
    AntigravityStatusAlert,
    AntigravityWatcher,
    ContactMessageAlert,
    ContactWatcher,
    WatchedContactRule,
    WatcherManager,
)


# ── Mocks ─────────────────────────────────────────────────────────────────────

class MockPlayer:
    """Mock JarvisUI for testing thread-safe Qt signals and logs."""

    def __init__(self):
        self.logs: List[str] = []
        self.slide_alerts: List[tuple[str, str]] = []
        self.badge_history: List[tuple[str, bool]] = []
        self.active_badge: str = ""
        self.badge_visible: bool = False

    def write_log(self, text: str) -> None:
        self.logs.append(text)

    def show_slide_alert(self, message: str, title: str = "SYSTEM ALERT") -> None:
        self.slide_alerts.append((title, message))

    def set_watcher_badge(self, text: str, active: bool = True) -> None:
        self.active_badge = text if active else ""
        self.badge_visible = active
        self.badge_history.append((text, active))


class MockLiveSession:
    """Mock JarvisLive session simulating Gemini Live websocket connection."""

    def __init__(self):
        self.spoken: List[str] = []
        self.client_contents: List[dict] = []
        self._loop = asyncio.new_event_loop()
        self.session = MagicMock()

        async def _mock_send_client_content(turns: Any = None, turn_complete: bool = True):
            self.client_contents.append({"turns": turns, "turn_complete": turn_complete})
            return True

        self.session.send_client_content = AsyncMock(side_effect=_mock_send_client_content)

    def speak(self, text: str) -> None:
        self.spoken.append(text)


# ── 1. Contact Message Matching & App Filtering ───────────────────────────────

def test_contact_watcher_fuzzy_matching():
    """Verify case-insensitive fuzzy matching against contact names."""
    watcher = ContactWatcher()

    # Exact substring & token matches
    assert watcher._matches_contact("Elon Musk", "New message from Elon Musk: Rocket launch scheduled.")
    assert watcher._matches_contact("elon musk", "ELON MUSK: Meeting at 3pm")
    assert watcher._matches_contact("Sarah", "Sarah sent a photo")
    assert watcher._matches_contact("sarah", "SARAH: Project ready")

    # Word boundary / punctuation
    assert watcher._matches_contact("Alex", "Alex: Hey, are you free?")
    assert watcher._matches_contact("Alex", "[Alex] Document attached")

    # Negative matches
    assert not watcher._matches_contact("David", "John: Hello there")
    assert not watcher._matches_contact("Alice", "Bob: Are you going to the party?")


def test_contact_watcher_app_filtering():
    """Verify notifications are filtered by targeted apps (e.g. WhatsApp, Edge)."""
    dispatched_alerts: List[ContactMessageAlert] = []
    watcher = ContactWatcher(alert_callback=lambda a: dispatched_alerts.append(a))

    # Watch contact with specific app filter
    watcher.add_watch("Alice", app_filter=["WhatsApp", "Edge"], one_shot=False)

    # 1. Matching app (WhatsApp) -> Alert triggered
    notif_wa = {
        "id": "notif_1",
        "app": "WhatsApp",
        "title": "Alice",
        "body": "Hey Jarvis, where is the report?",
    }
    watcher._process_notification(notif_wa)
    assert len(dispatched_alerts) == 1
    assert dispatched_alerts[0].contact == "Alice"
    assert dispatched_alerts[0].app == "WhatsApp"

    # 2. Non-matching app (Slack) -> Ignored
    notif_slack = {
        "id": "notif_2",
        "app": "Slack",
        "title": "Alice",
        "body": "Check this channel",
    }
    watcher._process_notification(notif_slack)
    assert len(dispatched_alerts) == 1  # Unchanged

    # 3. Matching app (Microsoft Edge) -> Alert triggered
    notif_edge = {
        "id": "notif_3",
        "app": "Microsoft Edge",
        "title": "Alice",
        "body": "Sent you a message",
    }
    watcher._process_notification(notif_edge)
    assert len(dispatched_alerts) == 2
    assert dispatched_alerts[1].app == "Microsoft Edge"


# ── 2. Deduplication & One-Shot Behavior ──────────────────────────────────────

def test_contact_watcher_deduplication():
    """Verify duplicate notifications (by ID or payload hash) are ignored."""
    dispatched_alerts: List[ContactMessageAlert] = []
    watcher = ContactWatcher(alert_callback=lambda a: dispatched_alerts.append(a))
    watcher.add_watch("Bob", app_filter=["WhatsApp"], one_shot=False)

    notif = {
        "id": "toast_bob_101",
        "app": "WhatsApp",
        "title": "Bob",
        "body": "Lunch at noon?",
    }

    # First arrival -> dispatched
    watcher._process_notification(notif)
    assert len(dispatched_alerts) == 1

    # Second arrival with same ID -> suppressed
    watcher._process_notification(notif)
    assert len(dispatched_alerts) == 1

    # Third arrival with different ID but identical content hash -> suppressed
    notif_dup_content = {
        "id": "toast_bob_102",
        "app": "WhatsApp",
        "title": "Bob",
        "body": "Lunch at noon?",
    }
    watcher._process_notification(notif_dup_content)
    assert len(dispatched_alerts) == 1

    # Distinct content -> dispatched
    notif_new = {
        "id": "toast_bob_103",
        "app": "WhatsApp",
        "title": "Bob",
        "body": "Actually, make it 1pm",
    }
    watcher._process_notification(notif_new)
    assert len(dispatched_alerts) == 2


def test_contact_watcher_one_shot_auto_removal():
    """Verify watch rule is automatically removed after first trigger when one_shot=True."""
    dispatched: List[ContactMessageAlert] = []
    watcher = ContactWatcher(alert_callback=lambda a: dispatched.append(a))

    # Add one-shot rule
    watcher.add_watch("Charlie", app_filter=["WhatsApp"], one_shot=True)
    assert "Charlie" in watcher.watched_contacts

    notif_1 = {"id": "c1", "app": "WhatsApp", "title": "Charlie", "body": "Are you there?"}
    watcher._process_notification(notif_1)

    assert len(dispatched) == 1
    # Rule must be automatically removed
    assert "Charlie" not in watcher.watched_contacts

    # Subsequent message from Charlie will not trigger
    notif_2 = {"id": "c2", "app": "WhatsApp", "title": "Charlie", "body": "Second message"}
    watcher._process_notification(notif_2)
    assert len(dispatched) == 1


# ── 3. Gemini Live Voice Interrupt & HUD Badge Signaling ───────────────────────

def test_contact_watcher_voice_interrupt_injection():
    """Verify matched contact toast injects mandatory verbal interrupt into Gemini Live session."""
    player = MockPlayer()
    live = MockLiveSession()
    mgr = WatcherManager(live_session=live, player=player)

    # Watch contact
    mgr.watch_contact_messages("Alice", app_filter=["WhatsApp"], one_shot=True)
    assert player.badge_visible
    assert "Alice" in player.active_badge

    # Trigger contact alert
    alert = ContactMessageAlert(
        contact="Alice",
        app="WhatsApp",
        sender="Alice",
        preview="Can you send the contract?",
        body="Can you send the contract?",
    )
    mgr._on_contact_alert(alert)

    # Verify Gemini Live session received high-priority client content frame
    assert len(live.client_contents) == 1
    content = live.client_contents[0]
    turns = content["turns"]
    injected_text = turns["parts"][0]["text"]

    expected_prefix = "Interrupt alert: New WhatsApp message from Alice: 'Can you send the contract?'"
    assert expected_prefix in injected_text
    assert "Read this to the user and ask what action or reply they want to take." in injected_text

    # Verify HUD slide alert and activity log
    assert any("Alice" in l for l in player.logs)
    assert any("Alice" in alert_body for _, alert_body in player.slide_alerts)

    # Because one_shot=True was removed, badge should now be cleared
    assert not player.badge_visible or "Alice" not in player.active_badge


# ── 4. Antigravity Log Transitions & Dual-Signal Monitoring ───────────────────

def test_antigravity_watcher_log_transition_detection():
    """Verify log file transitions: WAITING_FOR_APPROVAL, COMPLETED, FAILED."""
    alerts: List[AntigravityStatusAlert] = []
    watcher = AntigravityWatcher(alert_callback=lambda a: alerts.append(a))

    # Parse approval prompt
    st1, det1 = watcher._parse_log_chunk("Status: WAITING_FOR_APPROVAL - Request Review required", "task.log")
    assert st1 == "waiting_approval"
    assert "approval" in det1.lower()

    # Parse completion
    st2, det2 = watcher._parse_log_chunk("Agent execution: COMPLETED - all tasks complete", "agent.jsonl")
    assert st2 == "completed"
    assert "finished" in det2.lower()

    # Parse failure
    st3, det3 = watcher._parse_log_chunk("Worker fatal error: FAILED - exception raised", "run.log")
    assert st3 == "failed"
    assert "failed" in det3.lower()


def test_antigravity_watcher_filesystem_observer():
    """Verify watchdog observer reads newly written log chunk and dispatches alert."""
    alerts: List[AntigravityStatusAlert] = []
    watcher = AntigravityWatcher(alert_callback=lambda a: alerts.append(a))
    watcher.is_active = True

    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = Path(tmpdir) / "session.log"
        log_file.write_text("Starting agent...\n", encoding="utf-8")

        # Simulate change handler
        watcher._on_file_changed(str(log_file))
        assert len(alerts) == 0

        # Append completion
        with open(log_file, "a", encoding="utf-8") as f:
            f.write("Task execution finished: COMPLETED successfully.\n")

        watcher._on_file_changed(str(log_file))
        assert len(alerts) == 1
        assert alerts[0].status == "completed"


def test_antigravity_watcher_deduplication():
    """Verify repeating identical Antigravity status alerts within debounce window are suppressed."""
    alerts: List[AntigravityStatusAlert] = []
    watcher = AntigravityWatcher(alert_callback=lambda a: alerts.append(a))

    watcher._trigger_alert("waiting_approval", "Paused waiting for user approval.")
    assert len(alerts) == 1

    # Immediate second trigger with same state -> suppressed
    watcher._trigger_alert("waiting_approval", "Paused waiting for user approval.")
    assert len(alerts) == 1

    # Transition to completed -> dispatched
    watcher._trigger_alert("completed", "Task completed.")
    assert len(alerts) == 2


def test_antigravity_watcher_proactive_verbal_alert():
    """Verify Antigravity state change injects proper verbal prompt into Gemini Live session."""
    player = MockPlayer()
    live = MockLiveSession()
    mgr = WatcherManager(live_session=live, player=player)

    # 1. Approval alert
    appr_alert = AntigravityStatusAlert(
        status="waiting_approval",
        detail="Antigravity paused waiting for review approval.",
    )
    mgr._on_antigravity_alert(appr_alert)

    assert len(live.client_contents) == 1
    content1 = live.client_contents[0]["turns"]["parts"][0]["text"]
    assert "Antigravity update: Your background coding agent has paused waiting for your approval." in content1

    # 2. Completion alert
    comp_alert = AntigravityStatusAlert(
        status="completed",
        detail="Antigravity finished coding task.",
    )
    mgr._on_antigravity_alert(comp_alert)

    assert len(live.client_contents) == 2
    content2 = live.client_contents[1]["turns"]["parts"][0]["text"]
    assert "Antigravity update: Your background coding agent has finished its task." in content2


# ── 5. WatcherManager Tool Registration & HUD Management ──────────────────────

def test_watcher_manager_tool_interface():
    """Verify tool registration and invocation for contact and Antigravity watchers."""
    player = MockPlayer()
    live = MockLiveSession()
    mgr = WatcherManager(live_session=live, player=player)

    # 1. Watch contact
    res1 = mgr.watch_contact_messages(contact_name="Alice", app_filter=["WhatsApp"], one_shot=True)
    assert res1["status"] == "watching"
    assert res1["contact"] == "Alice"
    assert "Alice" in mgr.contact_watcher.watched_contacts
    assert player.badge_visible
    assert "Alice" in player.active_badge

    # 2. Watch Antigravity
    res2 = mgr.watch_antigravity_task(workspace_hint="/tmp/my_proj")
    assert res2["status"] == "watching"
    assert mgr.antigravity_watcher.is_active
    assert "ANTIGRAVITY" in player.active_badge

    # 3. Stop watching contact
    res3 = mgr.stop_watching_contact("Alice")
    assert res3["status"] == "stopped"
    assert "Alice" not in mgr.contact_watcher.watched_contacts
    # Antigravity is still active
    assert player.badge_visible
    assert "ANTIGRAVITY" in player.active_badge

    # 4. Stop watching Antigravity
    res4 = mgr.stop_watching_antigravity()
    assert res4["status"] == "stopped"
    assert not mgr.antigravity_watcher.is_active
    # All watchers stopped -> badge hidden
    assert not player.badge_visible


# ── 6. SQLite Notification DB Fallback Parsing ────────────────────────────────

def test_sqlite_fallback_parsing():
    """Verify _fetch_from_wpn_database extracts notifications from SQLite schema."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE Notification (
            Id INTEGER PRIMARY KEY,
            HandlerId INTEGER,
            Type TEXT,
            Payload BLOB,
            ArrivalTime INT64
        );
    """)
    c.execute("""
        CREATE TABLE NotificationHandler (
            RecordId INTEGER PRIMARY KEY,
            PrimaryId TEXT
        );
    """)

    # Insert handler (WhatsApp)
    c.execute("INSERT INTO NotificationHandler (RecordId, PrimaryId) VALUES (1, '5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App')")

    # Insert toast with XML payload
    toast_xml = b"""<toast><visual><binding template="ToastGeneric"><text>Sarah Connor</text><text>Come with me if you want to live.</text></binding></visual></toast>"""
    c.execute("INSERT INTO Notification (Id, HandlerId, Type, Payload, ArrivalTime) VALUES (5001, 1, 'toast', ?, 13440000000)", (toast_xml,))
    conn.commit()
    conn.close()

    watcher = ContactWatcher()
    watcher._db_path = db_path

    results = watcher._fetch_from_wpn_database()
    assert len(results) == 1
    assert results[0]["id"] == "5001"
    assert results[0]["app"] == "WhatsApp"
    assert results[0]["title"] == "Sarah Connor"
    assert results[0]["body"] == "Come with me if you want to live."
