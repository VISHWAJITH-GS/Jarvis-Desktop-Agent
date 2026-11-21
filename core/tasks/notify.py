"""
core/tasks/notify.py — Pluggable caller-injected notification mechanism.

Design reference: Mark LVII (CC BY-NC 4.0).
The background task engine is completely decoupled from Qt and the voice session.
The application caller (e.g., main.py or tests) injects how completed/paused tasks are announced.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Callback signature: (task_id: str, title: str, state: str, summary: str) -> None
NotifyCallback = Callable[[str, str, str, str], None]

_notify_callback: Optional[NotifyCallback] = None


def set_notify_callback(fn: Optional[NotifyCallback]) -> None:
    """Register the global notification handler for background tasks."""
    global _notify_callback
    _notify_callback = fn


def get_notify_callback() -> Optional[NotifyCallback]:
    """Retrieve the current notification handler."""
    return _notify_callback


def notify_task_event(task_id: str, title: str, state: str, summary: str) -> None:
    """Dispatch a background task completion, confirmation request, or failure event."""
    global _notify_callback
    if _notify_callback is not None:
        try:
            _notify_callback(task_id, title, state, summary)
            return
        except Exception as e:
            logger.warning("[Tasks] Notification callback failed: %s", e)

    # Fallback log when no callback is registered
    print(f"[Tasks] [{state.upper()}] Task '{title}' ({task_id}): {summary}")
