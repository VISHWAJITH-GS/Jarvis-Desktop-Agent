"""
core/routines — Consolidated Routines, Schedules, and Quiet Hours Engine for JARVIS.

Consolidates reminders, periodic timers, startup triggers, and idle hooks into
a unified scheduling engine with quiet hours and voice editing.
"""
from __future__ import annotations

from typing import Optional

from core.routines.engine import RoutineEngine
from core.routines.migration import migrate_legacy_reminders
from core.routines.quiet_hours import (
    QuietHoursConfig,
    get_quiet_hours_config,
    is_in_quiet_hours,
    save_quiet_hours_config,
    should_suppress_speech,
)
from core.routines.store import RoutineRecord, RoutineStore

__all__ = [
    "RoutineEngine",
    "RoutineRecord",
    "RoutineStore",
    "QuietHoursConfig",
    "get_quiet_hours_config",
    "save_quiet_hours_config",
    "is_in_quiet_hours",
    "should_suppress_speech",
    "migrate_legacy_reminders",
    "get_routine_engine",
]

_engine_instance: Optional[RoutineEngine] = None


def get_routine_engine() -> RoutineEngine:
    """Lazy singleton accessor for the consolidated routine engine."""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = RoutineEngine()
    return _engine_instance
