"""
core/tasks/control.py — External control surface: steer, cancel, and confirmation resolution.

Design reference: Mark LVII (CC BY-NC 4.0).
Provides thread-safe steering and interruption controls for in-flight tasks.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.tasks.notify import notify_task_event
from core.tasks.store import TaskRecord, TaskStore

_default_store: Optional[TaskStore] = None


def get_default_store() -> TaskStore:
    global _default_store
    if _default_store is None:
        _default_store = TaskStore()
    return _default_store


def set_default_store(store: TaskStore) -> None:
    global _default_store
    _default_store = store


def steer_task(task_id: str, note: str, store: Optional[TaskStore] = None) -> bool:
    """Deliver a mid-flight guidance note at the beginning of the task's next round."""
    s = store or get_default_store()
    task = s.get_task(task_id)
    if not task:
        return False
    if task.state in ("done", "failed", "cancelled"):
        return False
    return s.add_steer_note(task_id, note)


def cancel_task(task_id: str, finish: bool = False, store: Optional[TaskStore] = None) -> bool:
    """
    Cancel or gracefully finish an in-flight background task.
    If finish=True, requests the next round to offer no tools and write the final report.
    """
    s = store or get_default_store()
    task = s.get_task(task_id)
    if not task:
        return False
    if task.state in ("done", "failed", "cancelled"):
        return False

    if finish:
        # Mark finish early in checkpoint metadata
        cp = dict(task.checkpoint or {})
        cp["finish_early_requested"] = True
        return s.atomic_checkpoint(
            task_id=task_id,
            state=task.state,
            round_count=task.round_count,
            elapsed_sec=task.elapsed_sec,
            checkpoint=cp,
        )
    else:
        # Immediate cancellation
        success = s.update_state(task_id, "cancelled", error="Cancelled by user")
        if success:
            notify_task_event(task.id, task.title, "cancelled", "Task was cancelled by user.")
        return success


def resolve_confirmation(task_id: str, confirmed: bool, store: Optional[TaskStore] = None) -> bool:
    """Resolve a pending confirmation for a task paused in 'needs_confirmation'."""
    s = store or get_default_store()
    task = s.get_task(task_id)
    if not task or task.state != "needs_confirmation":
        return False

    if confirmed:
        # Set task back to queued to resume tool execution
        return s.update_state(task_id, "queued")
    else:
        # User rejected the confirmation: mark cancelled or resume with failure noted
        return s.update_state(task_id, "cancelled", error="Required action confirmation was rejected by user.")
