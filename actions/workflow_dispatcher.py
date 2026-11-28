"""
actions/workflow_dispatcher.py — Status & Control Tools for the 3-Key Architecture.

Exposes management tools on GEMINI_KEY_MAIN to inspect and control background work:
1. dispatch_background_workflow(goal, steps)
2. get_worker_status()
3. cancel_background_task(task_id)
4. resolve_worker_approval(task_id, approved)
5. watch_contact_messages(contact_name, app_filter)
6. watch_antigravity_task()
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.dispatcher import BackgroundWorkflowDispatcher
from core.watchers import WatcherService


def dispatch_background_workflow(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Dispatches a multi-step workflow goal and decomposed steps to a background worker."""
    p = dict(parameters or {})
    p.update(kwargs)

    goal = str(p.get("goal") or "").strip()
    raw_steps = p.get("steps") or []
    if isinstance(raw_steps, str):
        try:
            raw_steps = json.loads(raw_steps)
        except Exception:
            raw_steps = [s.strip() for s in raw_steps.split("\n") if s.strip()]

    if not goal:
        return "A 'goal' string is required to dispatch a background workflow."
    if not raw_steps:
        return "A list of 'steps' is required to dispatch a background workflow."

    dispatcher = BackgroundWorkflowDispatcher.get_instance(player=player)
    res = dispatcher.dispatch_background_workflow(goal=goal, steps=raw_steps)
    return json.dumps(res)


def get_worker_status(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Returns the real-time status of Worker-1, Worker-2, active tasks, and queue depth."""
    dispatcher = BackgroundWorkflowDispatcher.get_instance(player=player)
    res = dispatcher.get_worker_status()
    return json.dumps(res)


def cancel_background_task(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Gracefully cancels a running or queued background task by its task_id."""
    p = dict(parameters or {})
    p.update(kwargs)
    task_id = str(p.get("task_id") or "").strip()
    if not task_id:
        return "A 'task_id' is required to cancel a background task."

    dispatcher = BackgroundWorkflowDispatcher.get_instance(player=player)
    res = dispatcher.cancel_background_task(task_id=task_id)
    return json.dumps(res)


def resolve_worker_approval(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Approves or rejects a sensitive background step in WAITING_APPROVAL state."""
    p = dict(parameters or {})
    p.update(kwargs)
    task_id = str(p.get("task_id") or "").strip()
    if not task_id:
        return "A 'task_id' is required to resolve worker approval."

    approved = bool(p.get("approved", True))
    dispatcher = BackgroundWorkflowDispatcher.get_instance(player=player)
    res = dispatcher.resolve_worker_approval(task_id=task_id, approved=approved)
    return json.dumps(res)


def watch_contact_messages(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Monitors system toast notifications for incoming messages from contact_name."""
    p = dict(parameters or {})
    p.update(kwargs)
    contact_name = str(p.get("contact_name") or p.get("contact") or "").strip()
    if not contact_name:
        return "A 'contact_name' is required to watch messages."

    app_filter = p.get("app_filter")
    if isinstance(app_filter, str):
        app_filter = [a.strip() for a in app_filter.split(",") if a.strip()]

    watcher_svc = WatcherService.get_instance(player=player)
    res = watcher_svc.watch_contact_messages(contact_name=contact_name, app_filter=app_filter)
    return json.dumps(res)


def watch_antigravity_task(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Monitors Google Antigravity processes and logs for task completion or review approval."""
    watcher_svc = WatcherService.get_instance(player=player)
    res = watcher_svc.watch_antigravity_task()
    return json.dumps(res)


# ── Action Declarations (TOOLS) ───────────────────────────────────────────────

TOOLS = [
    {
        "name": "dispatch_background_workflow",
        "description": (
            "Dispatches a multi-step workflow goal and decomposed steps to a background worker "
            "(Worker-1 or Worker-2) for zero-focus non-blocking execution. Call this for any 2+ "
            "dependent actions (e.g. 'open Gmail, read email, analyze, write reply'). "
            "Returns immediately in <300ms without blocking voice conversation."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {
                    "type": "STRING",
                    "description": "High-level goal for the background workflow",
                },
                "steps": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Ordered list of individual steps to execute in sequence",
                },
            },
            "required": ["goal", "steps"],
        },
        "handler": dispatch_background_workflow,
    },
    {
        "name": "get_worker_status",
        "description": (
            "Returns the real-time status of Background Worker-1, Worker-2, active tasks, "
            "step progress, and queue depth."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
        "handler": get_worker_status,
    },
    {
        "name": "cancel_background_task",
        "description": "Gracefully cancels a running or queued background task by its task_id.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task_id": {
                    "type": "STRING",
                    "description": "The unique task_id of the workflow to cancel",
                }
            },
            "required": ["task_id"],
        },
        "handler": cancel_background_task,
    },
    {
        "name": "resolve_worker_approval",
        "description": (
            "Approves or rejects a sensitive background step currently in WAITING_APPROVAL state "
            "(such as sending an email, sending a WhatsApp message, or deleting files)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task_id": {
                    "type": "STRING",
                    "description": "The unique task_id of the paused workflow",
                },
                "approved": {
                    "type": "BOOLEAN",
                    "description": "True to approve and execute the sensitive step; False to reject and cancel",
                },
            },
            "required": ["task_id", "approved"],
        },
        "handler": resolve_worker_approval,
    },
    {
        "name": "watch_contact_messages",
        "description": (
            "Monitors system toast notifications for incoming messages from a specific contact "
            "(fuzzy match on WhatsApp, Edge, etc.) and injects proactive voice alerts."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "contact_name": {
                    "type": "STRING",
                    "description": "Name of contact to watch for incoming messages",
                },
                "app_filter": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Optional list of apps to filter, e.g. ['WhatsApp', 'Edge']",
                },
            },
            "required": ["contact_name"],
        },
        "handler": watch_contact_messages,
    },
    {
        "name": "watch_antigravity_task",
        "description": (
            "Monitors Google Antigravity CLI and IDE processes and logs for task completion or "
            "pauses waiting for review approval, proactively alerting the user verbally."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
        "handler": watch_antigravity_task,
    },
]
