"""
tests/test_productivity.py — Comprehensive tests for Productivity plugin.
"""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from core.outcome import STATUS_DONE, STATUS_FAILED
from core import undo
from plugins import productivity


class TestProductivityPart1(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store_file = Path(self.test_dir) / "productivity.json"
        self.store = productivity.ProductivityStore(self.store_file)
        # Point global store to this test store
        self._orig_store = productivity._global_store
        productivity._global_store = self.store
        undo.clear()

    def tearDown(self):
        productivity._global_store = self._orig_store
        undo.clear()
        try:
            shutil.rmtree(self.test_dir, ignore_errors=True)
        except Exception:
            pass

    def test_add_and_list_tasks(self):
        # 1. Add task with natural date
        res = productivity.run({
            "action": "add_task",
            "title": "Buy groceries",
            "notes": "Milk, eggs, coffee",
            "due": "tomorrow 6pm",
            "priority": "high",
        })
        self.assertIn("status=done", res)
        self.assertIn("title=\"Buy groceries\"", res)

        # 2. List tasks
        list_res = productivity.run({"action": "list_tasks"})
        self.assertIn("status=done", list_res)
        self.assertIn("Buy groceries", list_res)
        self.assertIn("count=1", list_res)

    def test_complete_task_and_recurrence_rollover(self):
        # Add recurring task
        add_res = productivity.run({
            "action": "add_task",
            "title": "Daily Standup",
            "due": "today 9am",
            "recurrence": "daily",
            "scope": "daily",
        })
        self.assertIn("status=done", add_res)

        # Complete task via fuzzy title
        comp_res = productivity.run({
            "action": "complete_task",
            "title": "standup",
        })
        self.assertIn("status=done", comp_res)
        self.assertIn("recurring_next_id", comp_res)

        # Check store: one done task, one new pending task
        data = self.store.load()
        tasks = data["tasks"]
        self.assertEqual(len(tasks), 2)
        done_tasks = [t for t in tasks.values() if t["status"] == "done"]
        pending_tasks = [t for t in tasks.values() if t["status"] == "pending"]
        self.assertEqual(len(done_tasks), 1)
        self.assertEqual(len(pending_tasks), 1)
        self.assertEqual(done_tasks[0]["title"], "Daily Standup")
        self.assertEqual(pending_tasks[0]["title"], "Daily Standup")

    def test_recurrence_calculation_scopes(self):
        # Test weekdays recurrence rollover from Friday
        base_friday = datetime(2026, 11, 20, 10, 0, 0)  # Friday
        next_dt = productivity.compute_next_recurrence(base_friday, "weekdays", base_friday)
        # Should be Monday (Nov 23)
        self.assertEqual(next_dt.weekday(), 0)
        self.assertEqual(next_dt.day, 23)

        # Test weekly
        next_weekly = productivity.compute_next_recurrence(base_friday, "weekly", base_friday)
        self.assertEqual(next_weekly.day, 27)

        # Test monthly
        base_jan31 = datetime(2026, 1, 31, 10, 0, 0)
        next_monthly = productivity.compute_next_recurrence(base_jan31, "monthly", base_jan31)
        self.assertEqual(next_monthly.month, 2)
        self.assertEqual(next_monthly.day, 28)  # Clamped to Feb 28

    def test_fuzzy_matching_ambiguity(self):
        # Add two similarly named tasks
        productivity.run({"action": "add_task", "title": "Submit tax report"})
        productivity.run({"action": "add_task", "title": "Submit project report"})

        # Try completing ambiguous query "Submit report"
        res = productivity.run({"action": "complete_task", "title": "Submit report"})
        # Must return status=ask rather than guessing!
        self.assertIn("status=ask", res)
        self.assertIn("Which one did you mean?", res)

    def test_corrupt_json_recovery(self):
        # Write corrupted garbage to productivity.json
        self.store_file.write_text("{invalid json garbage... broken", encoding="utf-8")

        # Load should recover by backing up and providing clean data
        data = self.store.load()
        self.assertIn("tasks", data)
        self.assertEqual(len(data["tasks"]), 0)

        # Check that backup file was created
        backups = list(self.store_file.parent.glob("productivity.corrupt.*"))
        self.assertGreaterEqual(len(backups), 1)

    def test_undo_restores_state(self):
        # 1. Add task
        add_res = productivity.run({"action": "add_task", "title": "Call Doctor"})
        self.assertIn("status=done", add_res)
        data = self.store.load()
        self.assertEqual(len(data["tasks"]), 1)

        # Undo add
        undo_res = undo.undo_last()
        self.assertIn("Undone: add task 'Call Doctor'", undo_res)
        data = self.store.load()
        self.assertEqual(len(data["tasks"]), 0)

        # 2. Add and complete recurring task, then undo complete
        productivity.run({
            "action": "add_task",
            "title": "Exercise",
            "recurrence": "daily",
        })
        comp_res = productivity.run({"action": "complete_task", "title": "Exercise"})
        self.assertIn("status=done", comp_res)
        self.assertEqual(len(self.store.load()["tasks"]), 2)  # 1 done + 1 recurring pending

        # Undo completion
        undo_comp_res = undo.undo_last()
        self.assertIn("Undone: complete task 'Exercise'", undo_comp_res)
        data = self.store.load()
        self.assertEqual(len(data["tasks"]), 1)
        task = list(data["tasks"].values())[0]
        self.assertEqual(task["status"], "pending")
        self.assertIsNone(task["completed_at"])

    def test_daily_summary_and_missed_rollover(self):
        now = datetime.now()
        yesterday_dt = now - timedelta(days=1)

        # Add an overdue daily task
        productivity.run({
            "action": "add_task",
            "title": "Missed Daily Chore",
            "scope": "daily",
            "due": yesterday_dt.strftime("%Y-%m-%d %H:%M"),
        })

        summary_res = productivity.run({"action": "daily_summary"})
        self.assertIn("status=done", summary_res)
        self.assertIn("rolled_over=1", summary_res)

        # Verify task due date rolled over to today
        data = self.store.load()
        chore = list(data["tasks"].values())[0]
        self.assertTrue(chore["due"].startswith(now.strftime("%Y-%m-%d")))


class TestProductivityPart2(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store_file = Path(self.test_dir) / "productivity.json"
        self.store = productivity.ProductivityStore(self.store_file)
        self._orig_store = productivity._global_store
        productivity._global_store = self.store
        undo.clear()

    def tearDown(self):
        productivity._global_store = self._orig_store
        undo.clear()
        try:
            shutil.rmtree(self.test_dir, ignore_errors=True)
        except Exception:
            pass

    def test_reminder_fires_exactly_once(self):
        from unittest.mock import patch

        t0 = datetime(2026, 11, 21, 10, 0, 0)
        productivity.run({
            "action": "add_task",
            "title": "Meeting with team",
            "due": t0.isoformat(),
        })

        with patch("plugins.productivity._dispatch_desktop_notification", return_value=True) as mock_notify:
            # Check at t0
            dispatched1 = productivity.check_due_reminders(self.store, now=t0)
            self.assertEqual(len(dispatched1), 1)
            self.assertEqual(dispatched1[0]["title"], "Meeting with team")
            self.assertEqual(mock_notify.call_count, 1)

            # Check 1 minute later
            t1 = t0 + timedelta(minutes=1)
            dispatched2 = productivity.check_due_reminders(self.store, now=t1)
            # Must NOT fire duplicate
            self.assertEqual(len(dispatched2), 0)
            self.assertEqual(mock_notify.call_count, 1)

    def test_reminder_survives_restart_no_dupes(self):
        from unittest.mock import patch

        t0 = datetime(2026, 11, 21, 10, 0, 0)
        productivity.run({
            "action": "add_task",
            "title": "Doctor appointment",
            "due": t0.isoformat(),
        })

        with patch("plugins.productivity._dispatch_desktop_notification", return_value=True) as mock_notify:
            dispatched = productivity.check_due_reminders(self.store, now=t0)
            self.assertEqual(len(dispatched), 1)

            # Simulate restart: new ProductivityStore instance reading the same JSON file
            restarted_store = productivity.ProductivityStore(self.store_file)
            dispatched_after_restart = productivity.check_due_reminders(restarted_store, now=t0 + timedelta(minutes=5))
            self.assertEqual(len(dispatched_after_restart), 0)
            self.assertEqual(mock_notify.call_count, 1)

    def test_missed_while_closed_fires_once(self):
        from unittest.mock import patch

        t_close = datetime(2026, 11, 21, 8, 0, 0)
        # Store records last check before closing
        data = self.store.load()
        data["last_missed_check"] = t_close.isoformat()
        self.store.save_and_verify(data)

        # A task that was due while JARVIS was closed (e.g. at 08:30)
        t_due = datetime(2026, 11, 21, 8, 30, 0)
        productivity.run({
            "action": "add_task",
            "title": "Offline Chore",
            "due": t_due.isoformat(),
        })

        # JARVIS launches at 09:00 and performs boot check
        t_launch = datetime(2026, 11, 21, 9, 0, 0)
        with patch("plugins.productivity._dispatch_desktop_notification", return_value=True) as mock_notify:
            dispatched = productivity.check_due_reminders(self.store, now=t_launch, boot_check=True)
            self.assertEqual(len(dispatched), 1)
            self.assertTrue(dispatched[0]["missed"])
            self.assertEqual(mock_notify.call_count, 1)

            # Subsequent check does not fire it again
            dispatched2 = productivity.check_due_reminders(self.store, now=t_launch + timedelta(minutes=1))
            self.assertEqual(len(dispatched2), 0)
            self.assertEqual(mock_notify.call_count, 1)

    def test_snooze_reschedules_and_undoable(self):
        t0 = datetime(2026, 11, 21, 10, 0, 0)
        productivity.run({
            "action": "add_task",
            "title": "Drink Water",
            "due": t0.isoformat(),
        })
        # Trigger reminder so last_reminded_task_id is set
        productivity.check_due_reminders(self.store, now=t0)

        # Snooze 15 minutes
        snooze_res = productivity.run({"action": "snooze", "duration_minutes": 15})
        self.assertIn("status=done", snooze_res)
        self.assertIn("snoozed_minutes=15", snooze_res)

        data = self.store.load()
        task = list(data["tasks"].values())[0]
        self.assertNotEqual(task["due"], t0.isoformat())

        # Undo snooze
        undo_res = undo.undo_last()
        self.assertIn("Undone: snooze task 'Drink Water' for 15m", undo_res)
        data = self.store.load()
        task = list(data["tasks"].values())[0]
        self.assertEqual(task["due"], t0.isoformat())


class TestProductivityPart3(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store_file = Path(self.test_dir) / "productivity.json"
        self.store = productivity.ProductivityStore(self.store_file)
        self._orig_store = productivity._global_store
        productivity._global_store = self.store
        undo.clear()

    def tearDown(self):
        productivity._global_store = self._orig_store
        undo.clear()
        try:
            shutil.rmtree(self.test_dir, ignore_errors=True)
        except Exception:
            pass

    def test_add_log_and_list_habits(self):
        add_res = productivity.run({
            "action": "add_habit",
            "name": "Morning Meditation",
            "target": "daily",
            "reminder_time": "07:30",
        })
        self.assertIn("status=done", add_res)
        self.assertIn("Morning Meditation", add_res)

        # Log completion
        log_res = productivity.run({
            "action": "log_habit",
            "name": "meditation",
        })
        self.assertIn("status=done", log_res)
        self.assertIn("streak=1", log_res)

        # List habits
        list_res = productivity.run({"action": "list_habits"})
        self.assertIn("status=done", list_res)
        self.assertIn("Morning Meditation", list_res)

    def test_streak_calculation_across_boundaries_and_timezones(self):
        from datetime import date

        # 1. Test streaks across month boundary (Oct 30, Oct 31, Nov 01)
        log_dates = ["2026-10-30", "2026-10-31", "2026-11-01"]
        streak, longest = productivity.calculate_habit_streaks(log_dates, today=date(2026, 11, 1))
        self.assertEqual(streak, 3)
        self.assertEqual(longest, 3)

        # If today is Nov 2 (not logged yet, but yesterday was), streak remains 3
        streak_next_day, _ = productivity.calculate_habit_streaks(log_dates, today=date(2026, 11, 2))
        self.assertEqual(streak_next_day, 3)

        # If today is Nov 3 (gap of a day), streak drops to 0
        streak_gap, _ = productivity.calculate_habit_streaks(log_dates, today=date(2026, 11, 3))
        self.assertEqual(streak_gap, 0)

        # 2. Test leap year boundary (Feb 28, Feb 29, Mar 01 on leap year 2028)
        leap_dates = ["2028-02-28", "2028-02-29", "2028-03-01"]
        leap_streak, _ = productivity.calculate_habit_streaks(leap_dates, today=date(2028, 3, 1))
        self.assertEqual(leap_streak, 3)

        # 3. Test duplicate logs on same day (idempotent)
        dupe_dates = ["2026-11-20", "2026-11-20", "2026-11-21"]
        dupe_streak, _ = productivity.calculate_habit_streaks(dupe_dates, today=date(2026, 11, 21))
        self.assertEqual(dupe_streak, 2)

    def test_habit_stats_and_best_weekday(self):
        productivity.run({
            "action": "add_habit",
            "name": "Evening Reading",
            "target": "daily",
        })

        # Insufficient history initially
        stats_empty = productivity.run({"action": "habit_stats", "name": "reading"})
        self.assertIn("status=done", stats_empty)
        self.assertIn("Not enough history yet", stats_empty)

        # Log completions on specific dates (Mondays: 2026-11-02, 2026-11-09, 2026-11-16)
        productivity.run({"action": "log_habit", "name": "reading", "date": "2026-11-02"})
        productivity.run({"action": "log_habit", "name": "reading", "date": "2026-11-09"})
        productivity.run({"action": "log_habit", "name": "reading", "date": "2026-11-16"})

        stats_res = productivity.run({"action": "habit_stats", "name": "reading"})
        self.assertIn("status=done", stats_res)
        self.assertIn("best_weekday=Monday", stats_res)
        self.assertIn("total_completions=3", stats_res)

    def test_archive_habit_and_undo(self):
        productivity.run({"action": "add_habit", "name": "Guitar Practice"})
        archive_res = productivity.run({"action": "archive_habit", "name": "guitar"})
        self.assertIn("status=done", archive_res)
        self.assertIn("archived=true", archive_res)

        # List habits (default active) should not contain archived habit
        list_res = productivity.run({"action": "list_habits"})
        self.assertNotIn("Guitar Practice", list_res)

        # Undo archive
        undo_res = undo.undo_last()
        self.assertIn("Undone: archive habit 'Guitar Practice'", undo_res)
        list_res2 = productivity.run({"action": "list_habits"})
        self.assertIn("Guitar Practice", list_res2)

    def test_weekly_review(self):
        # 1. Weekly review with insufficient history
        rev_insufficient = productivity.run({"action": "weekly_review"})
        self.assertIn("status=done", rev_insufficient)
        self.assertIn("Not enough history", rev_insufficient)

        # 2. Add tasks and habit logs to establish history
        productivity.run({"action": "add_task", "title": "Deploy update", "due": "today 5pm"})
        productivity.run({"action": "complete_task", "title": "Deploy update"})

        productivity.run({"action": "add_habit", "name": "Workout", "reminder_time": "18:00"})
        for i in range(5):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            productivity.run({"action": "log_habit", "name": "Workout", "date": d})

        rev_res = productivity.run({"action": "weekly_review"})
        self.assertIn("status=done", rev_res)
        self.assertIn("tasks_completed=1", rev_res)
        self.assertIn("suggestion=", rev_res)


class TestProductivityPart4(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store_file = Path(self.test_dir) / "productivity.json"
        self.store = productivity.ProductivityStore(self.store_file)
        self._orig_store = productivity._global_store
        productivity._global_store = self.store
        undo.clear()

    def tearDown(self):
        productivity._global_store = self._orig_store
        undo.clear()
        try:
            shutil.rmtree(self.test_dir, ignore_errors=True)
        except Exception:
            pass

    def test_quiet_hours_blocks_nudges(self):
        # Config has quiet hours 22:00 to 08:00
        t_night = datetime(2026, 11, 21, 23, 30, 0)
        t_early = datetime(2026, 11, 21, 6, 15, 0)
        t_day = datetime(2026, 11, 21, 14, 0, 0)

        self.assertTrue(productivity.is_in_quiet_hours(t_night, "22:00", "08:00"))
        self.assertTrue(productivity.is_in_quiet_hours(t_early, "22:00", "08:00"))
        self.assertFalse(productivity.is_in_quiet_hours(t_day, "22:00", "08:00"))

        # Add habit with active streak
        productivity.run({"action": "add_habit", "name": "Deep Work"})
        # Log past 2 days
        d1 = (t_night - timedelta(days=2)).strftime("%Y-%m-%d")
        d2 = (t_night - timedelta(days=1)).strftime("%Y-%m-%d")
        productivity.run({"action": "log_habit", "name": "Deep Work", "date": d1})
        productivity.run({"action": "log_habit", "name": "Deep Work", "date": d2})

        # Nudge at night must be blocked by quiet hours
        nudge_night = productivity.generate_coaching_nudge(self.store, now=t_night)
        self.assertIsNone(nudge_night)

        # Nudge during daytime is allowed
        nudge_day = productivity.generate_coaching_nudge(self.store, now=t_day)
        self.assertIsNotNone(nudge_day)
        self.assertEqual(nudge_day["type"], "streak_protection")

    def test_rate_limiting_nudges(self):
        # Configure max 2 nudges per day
        data = self.store.load()
        data["config"]["max_proactive_nudges_per_day"] = 2
        self.store.save_and_verify(data)

        # Set up a habit needing attention
        t_day = datetime(2026, 11, 21, 15, 0, 0)
        productivity.run({"action": "add_habit", "name": "Yoga"})
        d1 = (t_day - timedelta(days=2)).strftime("%Y-%m-%d")
        d2 = (t_day - timedelta(days=1)).strftime("%Y-%m-%d")
        productivity.run({"action": "log_habit", "name": "Yoga", "date": d1})
        productivity.run({"action": "log_habit", "name": "Yoga", "date": d2})

        # 1st nudge fires
        nudge1 = productivity.generate_coaching_nudge(self.store, now=t_day, record=True)
        self.assertIsNotNone(nudge1)

        # 2nd nudge fires
        nudge2 = productivity.generate_coaching_nudge(self.store, now=t_day + timedelta(hours=1), record=True)
        self.assertIsNotNone(nudge2)

        # 3rd nudge must be blocked by rate limit
        nudge3 = productivity.generate_coaching_nudge(self.store, now=t_day + timedelta(hours=2), record=True)
        self.assertIsNone(nudge3)

    def test_no_invented_data_when_history_sparse(self):
        # Clean store with no habits or tasks
        t_day = datetime(2026, 11, 21, 14, 0, 0)
        nudge = productivity.generate_coaching_nudge(self.store, now=t_day)
        # Must not invent data!
        self.assertIsNone(nudge)

    def test_proactive_engine_prompt_integration(self):
        from actions.proactive import ProactiveEngine

        engine = ProactiveEngine()
        # Add habit with streak
        t_now = datetime.now()
        productivity.run({"action": "add_habit", "name": "Meditation"})
        d1 = (t_now - timedelta(days=1)).strftime("%Y-%m-%d")
        productivity.run({"action": "log_habit", "name": "Meditation", "date": d1})

        prompt = engine.build_prompt(memory={}, monitors=[], recent_turns=[])
        self.assertIn("[PROACTIVE_CHECK]", prompt)


class TestProductivityPart5(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store_file = Path(self.test_dir) / "productivity.json"
        self.store = productivity.ProductivityStore(self.store_file)
        self._orig_store = productivity._global_store
        productivity._global_store = self.store
        undo.clear()

        # Dashboard Test Client setup
        from dashboard.server import DashboardServer
        self.server = DashboardServer()
        from fastapi.testclient import TestClient
        self.client = TestClient(self.server.app, client=("127.0.0.1", 50000))
        self.auth_token = self.server.new_key(expiry_secs=60)
        # Login to get bearer token
        login_resp = self.client.post("/login", json={"pin": self.auth_token})
        self.token = login_resp.json().get("token")
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        productivity._global_store = self._orig_store
        undo.clear()
        try:
            shutil.rmtree(self.test_dir, ignore_errors=True)
        except Exception:
            pass

    def test_dashboard_unauthorized(self):
        from fastapi.testclient import TestClient
        unauth_client = TestClient(self.server.app, client=("127.0.0.1", 50001))
        resp = unauth_client.get("/api/productivity/today")
        self.assertEqual(resp.status_code, 401)

    def test_dashboard_today_tasks_and_checkoff(self):
        # Add a task due today
        add_res = productivity.run({"action": "add_task", "title": "Dashboard Test Task", "due": "today 6pm"})
        self.assertIn("status=done", add_res)
        data = self.store.load()
        task_id = list(data["tasks"].keys())[0]

        # Add a habit
        add_h_res = productivity.run({"action": "add_habit", "name": "Dashboard Habit"})
        self.assertIn("status=done", add_h_res)
        data = self.store.load()
        habit_id = list(data["habits"].keys())[0]

        # 1. GET /api/productivity/today
        resp = self.client.get("/api/productivity/today", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        json_data = resp.json()
        self.assertTrue(json_data["ok"])
        self.assertEqual(len(json_data["tasks"]), 1)
        self.assertEqual(json_data["tasks"][0]["id"], task_id)
        self.assertEqual(len(json_data["habits"]), 1)
        self.assertEqual(json_data["habits"][0]["id"], habit_id)

        # 2. Check off task via dashboard POST endpoint
        comp_resp = self.client.post(f"/api/productivity/tasks/{task_id}/complete", headers=self.headers)
        self.assertEqual(comp_resp.status_code, 200)
        self.assertTrue(comp_resp.json()["ok"])

        # Verify task is now marked done
        task = self.store.load()["tasks"][task_id]
        self.assertEqual(task["status"], "done")

        # 3. Check off habit via dashboard POST endpoint
        log_resp = self.client.post(f"/api/productivity/habits/{habit_id}/log", headers=self.headers)
        self.assertEqual(log_resp.status_code, 200)
        self.assertTrue(log_resp.json()["ok"])

        # Verify habit has log entry for today
        habit = self.store.load()["habits"][habit_id]
        self.assertEqual(len(habit["log"]), 1)
        self.assertEqual(habit["streak"], 1)

    def test_plugin_removable_resilience(self):
        """Verify dashboard endpoint handles gracefully if productivity plugin/store errors."""
        from unittest.mock import patch

        with patch("plugins.productivity.get_store", side_effect=ImportError("Simulated missing plugin")):
            resp = self.client.get("/api/productivity/today", headers=self.headers)
            self.assertEqual(resp.status_code, 500)
            # Fails gracefully with JSON error rather than taking down process
            self.assertFalse(resp.json()["ok"])

    def test_hud_tasks_tab_build_and_refresh(self):
        """Verify HUD tasks tab builds and refreshes on Qt thread without error."""
        from PyQt6.QtWidgets import QApplication, QWidget, QTextEdit
        app = QApplication.instance() or QApplication([])
        from gui.window_audio import WindowAudioMixin

        class DummyHUDWindow(WindowAudioMixin, QWidget):
            def __init__(self):
                super().__init__()
                self._expanded = True
                self._log = QTextEdit()
                self._drop_zone = QWidget()
                self._file_hint = QWidget()

        hud = DummyHUDWindow()
        tab = hud._build_tasks_tab()
        self.assertIsNotNone(tab)
        hud._refresh_tasks_tab()
        self.assertIsNotNone(hud._tasks_layout)


if __name__ == "__main__":
    unittest.main()
