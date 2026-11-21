"""
core/tasks — Durable Background Autonomy Engine for JARVIS.

Design reference: Mark LVII (github.com/FatihMakes/Mark-LVII, CC BY-NC 4.0).
Provides SQLite-backed atomic persistence, multi-round reasoning, quota recovery,
budget tracking, human confirmation gating, and prompt injection defense.
"""

from __future__ import annotations

from typing import Optional

from core.tasks.budgets import TaskBudget
from core.tasks.control import (
    cancel_task,
    get_default_store,
    resolve_confirmation,
    set_default_store,
    steer_task,
)
from core.tasks.engine import TaskEngine
from core.tasks.notify import (
    NotifyCallback,
    get_notify_callback,
    notify_task_event,
    set_notify_callback,
)
from core.tasks.recovery import (
    CLASSIFICATION_BLOCKED,
    CLASSIFICATION_FIXABLE,
    CLASSIFICATION_TRANSIENT,
    CLASSIFICATION_UNKNOWN,
    build_failure_report,
    classify_failure,
    execute_step_with_recovery,
    verify_state_before_retry,
)
from core.tasks.security import (
    DEFAULT_UNATTENDED_ALLOWLIST,
    execute_task_tool,
    is_tool_allowed,
    validate_workspace_path,
    wrap_untrusted,
)
from core.tasks.store import TaskRecord, TaskStore

__all__ = [
    "TaskEngine",
    "TaskStore",
    "TaskRecord",
    "TaskBudget",
    "steer_task",
    "cancel_task",
    "resolve_confirmation",
    "set_notify_callback",
    "get_notify_callback",
    "notify_task_event",
    "execute_task_tool",
    "is_tool_allowed",
    "validate_workspace_path",
    "wrap_untrusted",
    "DEFAULT_UNATTENDED_ALLOWLIST",
    "get_task_engine",
    "classify_failure",
    "execute_step_with_recovery",
    "build_failure_report",
    "verify_state_before_retry",
    "CLASSIFICATION_TRANSIENT",
    "CLASSIFICATION_FIXABLE",
    "CLASSIFICATION_BLOCKED",
    "CLASSIFICATION_UNKNOWN",
]

_engine_instance: Optional[TaskEngine] = None


def get_task_engine() -> TaskEngine:
    """Lazy singleton accessor for the background task engine."""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = TaskEngine()
    return _engine_instance
