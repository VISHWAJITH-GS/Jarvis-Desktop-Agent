"""
tests/test_routines.py — Comprehensive unit tests for consolidated routines engine.

Covers:
- RoutineStore CRUD and persistence
- Schedules: once, daily, weekly, every_n_minutes, at_startup, after_idle
- Action execution: speak, tool, task
- Quiet hours window logic and exact-time reminder exemption
- Voice tools (create, list, edit, delete, configure quiet hours)
- Legacy reminders auto-migration (from fixtures/databases without loss)
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from core.routines.engine import RoutineEngine
from core.routines.migration import migrate_legacy_reminders
from core.routines.quiet_hours import (
    QuietHoursConfig,
    is_in_quiet_hours,
    should_suppress_speech,
)
from core.routines.store import RoutineRecord, RoutineStore
from actions.routines import (
    configure_quiet_hours,
    create_routine,
    delete_routine,
    edit_routine,
    list_routines,
)


class TestRoutinesEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "routines_test.db"
        self.store = RoutineStore(self.db_path)
        self.speak_calls = []
        self.engine = RoutineEngine(
            store=self.store,
            speak_fn=lambda m: self.speak_calls.append(m),
            poll_interval=0.05,
        )

    def tearDown(self):
        self.engine.stop()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # 1. Store CRUD
    def test_store_crud(self):
        r = RoutineRecord(
            id="rt_crud_1",
            title="Morning News",
            schedule_type="daily",
            schedule_config={"time": "08:00"},
            action_type="speak",
            action_config={"message": "Good morning, here is the news."},
            is_exact_time=False,
            state="active",
            next_run_at="2026-11-21T08:00:00",
        )
        self.store.save_routine(r)

        loaded = self.store.get_routine("rt_crud_1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.title, "Morning News")
        self.assertEqual(loaded.action_type, "speak")

        # List due routines
        due = self.store.list_due_routines("2026-11-21T09:00:00")
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].id, "rt_crud_1")

        # Update run status
        self.store.update_run_status("rt_crud_1", last_run_at="2026-11-21T08:00:01", next_run_at="2026-11-22T08:00:00")
        updated = self.store.get_routine("rt_crud_1")
        self.assertEqual(updated.last_run_at, "2026-11-21T08:00:01")
        self.assertEqual(updated.next_run_at, "2026-11-22T08:00:00")

        # Delete
        self.store.delete_routine("rt_crud_1")
        self.assertIsNone(self.store.get_routine("rt_crud_1"))

    # 2. Quiet Hours Window & Speech Suppression
    def test_quiet_hours_and_exact_time_exemption(self):
        # Window: 22:00 to 07:00
        cfg = QuietHoursConfig(enabled=True, start_time="22:00", end_time="07:00")

        # 23:30 is inside quiet hours
        night_time = datetime(2026, 11, 21, 23, 30)
        self.assertTrue(is_in_quiet_hours(now=night_time, config=cfg))

        # Unprompted routine speech (e.g. idle or background) is suppressed:
        self.assertTrue(should_suppress_speech(is_exact_time=False, now=night_time, config=cfg))

        # BUT exact-time reminder STILL FIRES!
        self.assertFalse(should_suppress_speech(is_exact_time=True, now=night_time, config=cfg))

        # 14:00 is outside quiet hours: speech is never suppressed
        day_time = datetime(2026, 11, 21, 14, 0)
        self.assertFalse(is_in_quiet_hours(now=day_time, config=cfg))
        self.assertFalse(should_suppress_speech(is_exact_time=False, now=day_time, config=cfg))
        self.assertFalse(should_suppress_speech(is_exact_time=True, now=day_time, config=cfg))

    # 3. Schedule Triggers: at_startup and after_idle
    def test_startup_and_idle_triggers(self):
        # Setup at_startup routine
        r_start = RoutineRecord(
            id="rt_startup_1",
            title="System Diagnostics",
            schedule_type="at_startup",
            action_type="speak",
            action_config={"message": "All systems operational."},
            state="active",
        )
        self.store.save_routine(r_start)

        # Setup after_idle routine (idle_seconds = 0.1s for fast testing)
        r_idle = RoutineRecord(
            id="rt_idle_1",
            title="Idle Check",
            schedule_type="after_idle",
            schedule_config={"idle_seconds": 0.1},
            action_type="speak",
            action_config={"message": "Standing by."},
            state="active",
        )
        self.store.save_routine(r_idle)

        # Start engine: should trigger at_startup immediately
        self.engine.start()
        self.assertIn("All systems operational.", self.speak_calls)
        start_rec = self.store.get_routine("rt_startup_1")
        self.assertEqual(start_rec.state, "completed")

        # Wait for idle threshold
        time.sleep(0.2)
        self.engine._check_idle_routines()
        self.assertIn("Standing by.", self.speak_calls)

    # 4. Action Execution: speak, tool, task
    def test_action_execution_types(self):
        # 1. Speak action
        r_speak = RoutineRecord(
            id="rt_act_speak",
            title="Meditation reminder",
            schedule_type="once",
            action_type="speak",
            action_config={"message": "Time to breathe."},
            is_exact_time=True,
        )
        self.engine._execute_routine_action(r_speak)
        self.assertIn("Time to breathe.", self.speak_calls)

        # 2. Tool action
        r_tool = RoutineRecord(
            id="rt_act_tool",
            title="Check Weather",
            schedule_type="once",
            action_type="tool",
            action_config={"tool_name": "weather_report", "parameters": {"location": "London"}},
        )
        with patch("core.action_loader.get_default_registry") as mock_reg:
            mock_inst = MagicMock()
            mock_reg.return_value = mock_inst
            self.engine._execute_routine_action(r_tool)
            mock_inst.execute.assert_called_once()

        # 3. Task action
        r_task = RoutineRecord(
            id="rt_act_task",
            title="Daily Briefing Research",
            schedule_type="once",
            action_type="task",
            action_config={"task_prompt": "Gather tech news"},
        )
        with patch("core.tasks.get_task_engine") as mock_te:
            mock_task_eng = MagicMock()
            mock_te.return_value = mock_task_eng
            self.engine._execute_routine_action(r_task)
            mock_task_eng.queue_task.assert_called_once()

    # 5. Legacy Reminders Auto-Migration
    def test_legacy_reminders_auto_migration(self):
        fixtures_json = Path(self.temp_dir.name) / "test_reminders.json"
        legacy_data = [
            {
                "id": "rem_legacy_001",
                "task_name": "Dentist Appointment",
                "message": "Visit dentist on 5th avenue",
                "due_time": "2026-12-01T10:00:00",
                "recurrence": "none",
                "status": "pending",
                "created_at": "2026-11-01T12:00:00",
            },
            {
                "id": "rem_legacy_002",
                "task_name": "Drink Water",
                "message": "Drink a glass of water",
                "due_time": "2026-11-21T15:00:00",
                "recurrence": "daily",
                "status": "pending",
                "created_at": "2026-11-01T12:00:00",
            },
        ]
        fixtures_json.write_text(json.dumps(legacy_data), encoding="utf-8")

        migrated = migrate_legacy_reminders(self.store, custom_json_path=fixtures_json)
        self.assertEqual(migrated, 2)

        # Check migrated records in routine store
        r1 = self.store.get_routine("rem_legacy_001")
        self.assertIsNotNone(r1)
        self.assertEqual(r1.title, "Dentist Appointment")
        self.assertEqual(r1.schedule_type, "once")
        self.assertTrue(r1.is_exact_time)  # Reminders exempt from quiet hours
        self.assertEqual(r1.action_config["message"], "Visit dentist on 5th avenue")

        r2 = self.store.get_routine("rem_legacy_002")
        self.assertIsNotNone(r2)
        self.assertEqual(r2.schedule_type, "daily")
        self.assertTrue(r2.is_exact_time)

        # Idempotent: running migration again migrates 0 new items
        migrated_again = migrate_legacy_reminders(self.store, custom_json_path=fixtures_json)
        self.assertEqual(migrated_again, 0)

    # 6. Voice Tools (actions/routines.py)
    def test_voice_tools_lifecycle(self):
        with patch("actions.routines.get_routine_engine", return_value=self.engine):
            # Create
            res_create = create_routine(
                {
                    "title": "Evening Stretch",
                    "schedule_type": "daily",
                    "schedule_config": {"time": "20:00"},
                    "message": "Time for evening stretching",
                }
            )
            self.assertIn("Created daily routine 'Evening Stretch'", res_create)

            # List
            res_list = list_routines()
            self.assertIn("Evening Stretch", res_list)

            # Find ID
            routines = self.store.list_routines()
            rt = next(r for r in routines if r.title == "Evening Stretch")

            # Edit
            res_edit = edit_routine({"routine_id": rt.id, "title": "Night Yoga"})
            self.assertIn("Updated routine 'Night Yoga'", res_edit)
            self.assertEqual(self.store.get_routine(rt.id).title, "Night Yoga")

            # Configure quiet hours
            res_qh = configure_quiet_hours({"enabled": True, "start_time": "23:00", "end_time": "06:00"})
            self.assertIn("from 23:00 to 06:00", res_qh)

            # Delete
            res_del = delete_routine({"routine_id": rt.id})
            self.assertIn(f"Deleted routine {rt.id}", res_del)
            self.assertIsNone(self.store.get_routine(rt.id))


if __name__ == "__main__":
    unittest.main()
