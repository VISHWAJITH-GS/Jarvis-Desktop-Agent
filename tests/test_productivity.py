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


if __name__ == "__main__":
    unittest.main()
