"""
core/routines/engine.py — Scheduling and execution engine for consolidated routines.

Executes:
- once, daily, weekly, monthly, every N minutes
- at_startup (triggered immediately on engine startup)
- after_idle (triggered when system idle time exceeds threshold)

Actions:
- speak (subject to quiet hours suppression unless is_exact_time=True)
- tool (invokes ActionRegistry tool)
- task (launches background TaskEngine task)
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from core.routines.migration import migrate_legacy_reminders
from core.routines.quiet_hours import should_suppress_speech
from core.routines.store import RoutineRecord, RoutineStore

logger = logging.getLogger(__name__)


class RoutineEngine:
    """Consolidated scheduler and executor for background routines and reminders."""

    def __init__(
        self,
        store: Optional[RoutineStore] = None,
        speak_fn: Optional[Callable[[str], None]] = None,
        player=None,
        poll_interval: float = 1.0,
    ):
        self.store = store or RoutineStore()
        self.speak_fn = speak_fn
        self.player = player
        self.poll_interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_activity_time: float = time.monotonic()
        self._idle_triggered: set[str] = set()

    def record_user_activity(self) -> None:
        """Reset the idle clock when the user speaks or interacts."""
        self._last_activity_time = time.monotonic()
        self._idle_triggered.clear()

    def start(self) -> None:
        """Start routine scheduler and run startup routines."""
        with self._lock:
            if self._running:
                return
            self._running = True

            # 1. Migrate legacy reminders on first run
            try:
                migrate_legacy_reminders(self.store)
            except Exception as e:
                logger.warning("[Routines] Migration check failed: %s", e)

            # 2. Trigger at_startup routines
            self._trigger_startup_routines()

            # 3. Start scheduler background thread
            self._thread = threading.Thread(
                target=self._loop,
                daemon=True,
                name="JarvisRoutineEngine",
            )
            self._thread.start()
            logger.info("[Routines] Routine engine started.")

    def stop(self) -> None:
        """Stop scheduler."""
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _trigger_startup_routines(self) -> None:
        """Run all routines marked 'at_startup'."""
        routines = self.store.list_routines(state="active")
        for r in routines:
            if r.schedule_type == "at_startup":
                self._execute_routine_action(r)
                now_str = datetime.now().isoformat()
                self.store.update_run_status(r.id, last_run_at=now_str, next_run_at=None, state="completed")

    def _loop(self) -> None:
        while self._running:
            try:
                now = datetime.now()
                now_iso = now.isoformat()

                # Check scheduled due routines
                due = self.store.list_due_routines(now_iso)
                for r in due:
                    self._process_due_routine(r, now)

                # Check idle routines
                self._check_idle_routines()

            except Exception as e:
                logger.error("[Routines] Error in routine scheduler loop: %s", e)

            time.sleep(self.poll_interval)

    def _check_idle_routines(self) -> None:
        """Check routines scheduled with 'after_idle'."""
        idle_dur = time.monotonic() - self._last_activity_time
        routines = self.store.list_routines(state="active")
        for r in routines:
            if r.schedule_type == "after_idle":
                threshold = float(r.schedule_config.get("idle_seconds", 300.0))
                if idle_dur >= threshold and r.id not in self._idle_triggered:
                    self._execute_routine_action(r)
                    self._idle_triggered.add(r.id)
                    now_str = datetime.now().isoformat()
                    self.store.update_run_status(r.id, last_run_at=now_str, next_run_at=None)

    def _process_due_routine(self, r: RoutineRecord, now: datetime) -> None:
        """Execute a due routine and compute its next occurrence."""
        self._execute_routine_action(r)
        now_str = now.isoformat()

        # Compute next occurrence
        next_run = None
        next_state = "active"

        if r.schedule_type == "once":
            next_state = "completed"
        elif r.schedule_type == "every_n_minutes":
            mins = int(r.schedule_config.get("interval_minutes", 15))
            next_run = (now + timedelta(minutes=mins)).isoformat()
        elif r.schedule_type == "daily":
            time_str = r.schedule_config.get("time", "08:00")
            parts = [int(p) for p in time_str.split(":")]
            target = now.replace(hour=parts[0], minute=parts[1], second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            next_run = target.isoformat()
        elif r.schedule_type == "weekly":
            next_run = (now + timedelta(days=7)).isoformat()
        elif r.schedule_type == "monthly":
            next_run = (now + timedelta(days=30)).isoformat()

        self.store.update_run_status(r.id, last_run_at=now_str, next_run_at=next_run, state=next_state)

    def _execute_routine_action(self, r: RoutineRecord) -> None:
        """Perform the routine's designated action (speak, tool, or background task)."""
        logger.info("[Routines] Firing routine '%s' (Action: %s)", r.title, r.action_type)

        if r.action_type == "speak":
            msg = r.action_config.get("message", r.title)
            # Check quiet hours suppression
            if should_suppress_speech(is_exact_time=r.is_exact_time):
                logger.info("[Routines] Speech suppressed during quiet hours for routine '%s'.", r.title)
                # Show on UI silently if available
                if self.player and hasattr(self.player, "write_log"):
                    self.player.write_log(f"⏰ [Quiet Hours] {msg}")
                return

            # Announce via speech / UI
            if self.player:
                if hasattr(self.player, "write_log"):
                    self.player.write_log(f"⏰ {msg}")
                if hasattr(self.player, "show_content"):
                    self.player.show_content("Routine Alert", msg)

            if self.speak_fn:
                try:
                    self.speak_fn(msg)
                except Exception as e:
                    logger.warning("[Routines] Speech output failed: %s", e)

        elif r.action_type == "tool":
            tool_name = r.action_config.get("tool_name")
            params = r.action_config.get("parameters", {})
            if tool_name:
                try:
                    from core.action_loader import get_default_registry

                    reg = get_default_registry()
                    reg.execute(tool_name, params, ctx={"player": self.player})
                except Exception as e:
                    logger.error("[Routines] Tool execution failed for '%s': %s", tool_name, e)

        elif r.action_type == "task":
            prompt = r.action_config.get("task_prompt", r.title)
            try:
                from core.tasks import get_task_engine

                engine = get_task_engine()
                engine.start()
                engine.queue_task(
                    task_id=f"routine_{r.id}",
                    title=r.title,
                    description=prompt,
                )
            except Exception as e:
                logger.error("[Routines] Failed to launch background task for routine '%s': %s", r.title, e)
