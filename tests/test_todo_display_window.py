"""
tests/test_todo_display_window.py

Comprehensive tests for TasksDisplayWindow / TodoListDisplayWindow:
1. Google Tasks-inspired layout with separate 'Pending' and 'Completed' sections.
2. Clear description, due dates (overdue, today, upcoming), priority badges, recurrence tags, and notes.
3. Interactive checkboxes:
   - Clicking '○' on pending task checks it off and completes it.
   - Clicking '✓' on completed task reopens it to pending.
4. Quick-add controls: title, due date, priority selector, and '+ ADD TASK' button.
5. Task deletion controls: individual delete button on every task card.
6. Collapsible completed section with expand/collapse toggle.
7. Search & scope filter tabs (ALL, TODAY, UPCOMING, HIGH).
8. Thread-safe facade methods on JarvisUI (show_tasks, show_todo_list).
9. Productivity plugin integration for show_tasks and auto-refresh.
"""

from datetime import datetime, timedelta
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QLineEdit

# Ensure a single QApplication instance exists for GUI tests
app = QApplication.instance() or QApplication([])

from gui.display_windows import TasksDisplayWindow, TodoListDisplayWindow
from gui.facade import JarvisUI
from plugins import productivity
from plugins.productivity import ProductivityStore


class TestTodoDisplayWindow(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "productivity.json")
        self.store = ProductivityStore(self.db_path)

        # Patch get_store in plugins.productivity and in gui.display_windows
        self.patcher_store = patch("plugins.productivity.get_store", return_value=self.store)
        self.patcher_store.start()

        # Mock MainWindow and write_log
        self.mock_main_win = MagicMock()
        self.mock_main_win.write_log = MagicMock()

        self.win = TasksDisplayWindow(main_win=self.mock_main_win)

    def tearDown(self):
        try:
            self.win.close()
        except Exception:
            pass
        self.patcher_store.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_window_instantiation_and_structure(self):
        """Verify window creation, alias, Google Tasks aesthetic, and essential UI elements."""
        self.assertIs(TasksDisplayWindow, TodoListDisplayWindow)
        self.assertIsInstance(self.win, TasksDisplayWindow)
        self.assertIn("Google Tasks", self.win.windowTitle())

        # Check essential UI controls exist
        self.assertTrue(hasattr(self.win, "_count_lbl"))
        self.assertTrue(hasattr(self.win, "_in_title"))
        self.assertTrue(hasattr(self.win, "_in_due"))
        self.assertTrue(hasattr(self.win, "_priority_combo"))
        self.assertTrue(hasattr(self.win, "_add_btn"))
        self.assertTrue(hasattr(self.win, "_search_in"))
        self.assertTrue(hasattr(self.win, "_tab_btns"))
        self.assertTrue(hasattr(self.win, "_pending_hdr"))
        self.assertTrue(hasattr(self.win, "_pending_lay"))
        self.assertTrue(hasattr(self.win, "_completed_toggle_btn"))
        self.assertTrue(hasattr(self.win, "_completed_lay"))

        # Check scope tabs
        for tab in ("all", "today", "upcoming", "high"):
            self.assertIn(tab, self.win._tab_btns)

    def test_02_task_cards_rendering_pending_and_completed(self):
        """Tasks are separated into Pending and Completed sections with descriptions and due dates."""
        now = datetime.now()
        yesterday_iso = (now - timedelta(days=1)).replace(microsecond=0).isoformat()
        today_iso = now.replace(hour=20, minute=0, second=0, microsecond=0).isoformat()
        tomorrow_iso = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0).isoformat()
        completed_iso = (now - timedelta(hours=2)).replace(microsecond=0).isoformat()

        # Seed data
        data = {
            "tasks": {
                "t1": {
                    "id": "t1",
                    "title": "Fix server vulnerability",
                    "notes": "Patch OpenSSL and restart nginx",
                    "scope": "once",
                    "due": yesterday_iso,
                    "priority": "high",
                    "status": "pending",
                    "recurrence": None,
                    "created_at": yesterday_iso,
                    "completed_at": None,
                    "reminder_times": [],
                },
                "t2": {
                    "id": "t2",
                    "title": "Prepare team standup notes",
                    "notes": "Discuss sprint deliverables",
                    "scope": "daily",
                    "due": today_iso,
                    "priority": "normal",
                    "status": "pending",
                    "recurrence": "daily",
                    "created_at": today_iso,
                    "completed_at": None,
                    "reminder_times": [],
                },
                "t3": {
                    "id": "t3",
                    "title": "Review weekly budget",
                    "notes": "",
                    "scope": "weekly",
                    "due": tomorrow_iso,
                    "priority": "low",
                    "status": "pending",
                    "recurrence": "weekly",
                    "created_at": tomorrow_iso,
                    "completed_at": None,
                    "reminder_times": [],
                },
                "t4": {
                    "id": "t4",
                    "title": "Submit expense report",
                    "notes": "Q3 travel receipts",
                    "scope": "once",
                    "due": yesterday_iso,
                    "priority": "normal",
                    "status": "done",
                    "recurrence": None,
                    "created_at": yesterday_iso,
                    "completed_at": completed_iso,
                    "reminder_times": [],
                },
            },
            "habits": {},
            "daily_history": {},
        }
        self.store.save_and_verify(data)

        # Load and render
        self.win.load_tasks()

        # Header counts
        self.assertEqual(self.win._cached_pending_count, 3)
        self.assertEqual(self.win._cached_completed_count, 1)
        self.assertIn("3 PENDING", self.win._count_lbl.text())
        self.assertIn("1 DONE", self.win._count_lbl.text())
        self.assertIn("PENDING (3)", self.win._pending_hdr.text())
        self.assertIn("COMPLETED (1)", self.win._completed_toggle_btn.text())

        # Check pending layout has 3 cards
        self.assertEqual(self.win._pending_lay.count(), 3)
        # Check completed layout has 1 card
        self.assertEqual(self.win._completed_lay.count(), 1)

        # Inspect pending card 0 (High priority overdue)
        card_0 = self.win._pending_lay.itemAt(0).widget()
        labels_0 = [w.text() for w in card_0.findChildren(QLabel)]
        buttons_0 = [w.text() for w in card_0.findChildren(QPushButton)]
        self.assertIn("Fix server vulnerability", labels_0)
        self.assertIn("Patch OpenSSL and restart nginx", labels_0)
        self.assertTrue(any("Overdue" in lbl for lbl in labels_0))
        self.assertIn("[HIGH]", labels_0)
        self.assertIn("○", buttons_0)  # Pending checkbox button

        # Inspect completed card
        comp_card = self.win._completed_lay.itemAt(0).widget()
        comp_labels = [w.text() for w in comp_card.findChildren(QLabel)]
        comp_buttons = [w.text() for w in comp_card.findChildren(QPushButton)]
        self.assertIn("Submit expense report", comp_labels)
        self.assertIn("✓", comp_buttons)  # Completed checkbox button

    def test_03_interactive_checkbox_complete_and_reopen(self):
        """Clicking checkbox completes pending task; clicking checkmark reopens it."""
        data = {
            "tasks": {
                "task-abc": {
                    "id": "task-abc",
                    "title": "Buy groceries",
                    "notes": "Milk, eggs, coffee",
                    "scope": "once",
                    "due": None,
                    "priority": "normal",
                    "status": "pending",
                    "recurrence": None,
                    "created_at": datetime.now().isoformat(),
                    "completed_at": None,
                }
            },
            "habits": {},
            "daily_history": {},
        }
        self.store.save_and_verify(data)
        self.win.load_tasks()

        self.assertEqual(self.win._cached_pending_count, 1)
        self.assertEqual(self.win._cached_completed_count, 0)

        # 1. Complete task via _on_complete
        self.win._on_complete("task-abc", "Buy groceries")

        # Verify task is now marked done in store and reflected in UI
        reloaded = self.store.load()
        self.assertEqual(reloaded["tasks"]["task-abc"]["status"], "done")
        self.assertIsNotNone(reloaded["tasks"]["task-abc"]["completed_at"])
        self.assertEqual(self.win._cached_pending_count, 0)
        self.assertEqual(self.win._cached_completed_count, 1)

        # 2. Reopen task via _on_reopen
        self.win._on_reopen("task-abc", "Buy groceries")

        reloaded = self.store.load()
        self.assertEqual(reloaded["tasks"]["task-abc"]["status"], "pending")
        self.assertIsNone(reloaded["tasks"]["task-abc"]["completed_at"])
        self.assertEqual(self.win._cached_pending_count, 1)
        self.assertEqual(self.win._cached_completed_count, 0)

    def test_04_quick_add_controls(self):
        """Entering task details in quick-add bar creates a new task in store and updates view."""
        self.win._in_title.setText("Prepare presentation slides")
        self.win._in_due.setText("tomorrow 3pm")
        self.win._priority_combo.setCurrentText("High")

        self.win._on_quick_add()

        # Inputs should be cleared
        self.assertEqual(self.win._in_title.text(), "")
        self.assertEqual(self.win._in_due.text(), "")

        # Store should now contain the new task
        reloaded = self.store.load()
        tasks = list(reloaded["tasks"].values())
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "Prepare presentation slides")
        self.assertEqual(tasks[0]["priority"], "high")
        self.assertIsNotNone(tasks[0]["due"])
        self.assertEqual(tasks[0]["status"], "pending")

        # UI should render 1 pending card
        self.assertEqual(self.win._cached_pending_count, 1)
        self.assertEqual(self.win._pending_lay.count(), 1)

    def test_05_delete_task_control(self):
        """Clicking delete button removes the task from the store and UI."""
        data = {
            "tasks": {
                "t-del": {
                    "id": "t-del",
                    "title": "Temporary obsolete task",
                    "scope": "once",
                    "status": "pending",
                }
            },
            "habits": {},
            "daily_history": {},
        }
        self.store.save_and_verify(data)
        self.win.load_tasks()
        self.assertEqual(self.win._cached_pending_count, 1)

        # Delete task
        self.win._on_delete("t-del", "Temporary obsolete task")

        reloaded = self.store.load()
        self.assertNotIn("t-del", reloaded["tasks"])
        self.assertEqual(self.win._cached_pending_count, 0)

    def test_06_collapsible_completed_section(self):
        """Toggling completed section hides/shows the completed container and updates the arrow."""
        self.win.show()
        self.assertTrue(self.win._completed_expanded)
        self.assertFalse(self.win._completed_container.isHidden())
        self.assertIn("▼", self.win._completed_toggle_btn.text())

        # Collapse
        self.win._toggle_completed()
        self.assertFalse(self.win._completed_expanded)
        self.assertTrue(self.win._completed_container.isHidden())
        self.assertIn("▶", self.win._completed_toggle_btn.text())

        # Expand again
        self.win._toggle_completed()
        self.assertTrue(self.win._completed_expanded)
        self.assertFalse(self.win._completed_container.isHidden())
        self.assertIn("▼", self.win._completed_toggle_btn.text())

    def test_07_filtering_and_scope_tabs(self):
        """Scope tabs (ALL, TODAY, UPCOMING, HIGH) and keyword search filter tasks correctly."""
        now = datetime.now()
        today_iso = now.replace(hour=18, minute=0, second=0).isoformat()
        next_week_iso = (now + timedelta(days=7)).replace(hour=12, minute=0, second=0).isoformat()

        data = {
            "tasks": {
                "t1": {
                    "id": "t1",
                    "title": "Email project client",
                    "notes": "urgent billing question",
                    "due": today_iso,
                    "priority": "high",
                    "status": "pending",
                },
                "t2": {
                    "id": "t2",
                    "title": "Clean desktop workspace",
                    "notes": "routine maintenance",
                    "due": next_week_iso,
                    "priority": "low",
                    "status": "pending",
                },
            },
            "habits": {},
            "daily_history": {},
        }
        self.store.save_and_verify(data)

        # Filter: ALL
        self.win._set_scope("all")
        self.assertEqual(self.win._cached_pending_count, 2)

        # Filter: TODAY
        self.win._set_scope("today")
        self.assertEqual(self.win._cached_pending_count, 1)

        # Filter: HIGH
        self.win._set_scope("high")
        self.assertEqual(self.win._cached_pending_count, 1)

        # Search keyword "billing"
        self.win._set_scope("all")
        self.win._on_search_changed("billing")
        self.assertEqual(self.win._cached_pending_count, 1)

        # Search non-matching keyword
        self.win._on_search_changed("nonexistent xyz")
        self.assertEqual(self.win._cached_pending_count, 0)

    def test_08_window_popup_minimize_restore(self):
        """Window popup, minimize to dock, and restore from dock transitions correctly."""
        self.win.popup("all")
        self.assertTrue(self.win.isVisible())
        self.assertFalse(self.win._collapsed)

        self.win.minimize_to_dock()
        self.assertTrue(self.win._collapsed)
        self.assertFalse(self.win.isVisible())
        self.assertEqual(self.win._min_btn.text().strip(), "RESTORE")

        self.win.restore_from_dock()
        self.assertFalse(self.win._collapsed)
        self.assertTrue(self.win.isVisible())
        self.assertEqual(self.win._min_btn.text().strip(), "MIN")

    def test_09_thread_safe_facade_and_plugin_action(self):
        """JarvisUI facade methods emit the Qt signal and productivity plugin dispatches show_tasks."""
        mock_ui = MagicMock()
        mock_ui._is_alive.return_value = True
        mock_ui._win = MagicMock()
        # Bind show_tasks so calling show_todo_list invokes JarvisUI.show_tasks logic
        mock_ui.show_tasks = lambda scope="all": JarvisUI.show_tasks(mock_ui, scope)

        # Call thread-safe methods
        JarvisUI.show_tasks(mock_ui, "today")
        mock_ui._win._tasks_sig.emit.assert_called_with("today")

        JarvisUI.show_todo_list(mock_ui, "high")
        mock_ui._win._tasks_sig.emit.assert_called_with("high")

        # Test productivity plugin action show_tasks
        plugin_player = MagicMock()
        plugin_player.show_tasks = MagicMock()
        out = productivity.run({"action": "show_tasks", "scope": "upcoming"}, player=plugin_player)
        self.assertIn("status=done", out)
        plugin_player.show_tasks.assert_called_with("upcoming")

        # Test productivity plugin action show_todo_list alias
        out_alias = productivity.run({"action": "show_todo_list"}, player=plugin_player)
        self.assertIn("status=done", out_alias)
        plugin_player.show_tasks.assert_called_with("all")


if __name__ == "__main__":
    unittest.main()
