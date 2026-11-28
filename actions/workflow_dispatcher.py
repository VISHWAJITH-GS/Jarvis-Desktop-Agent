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
from core.watchers import WatcherManager, WatcherService
from actions.gesture_control import (
    enable_gesture_mode,
    disable_gesture_mode,
    get_gesture_status,
    enable_mouse_control,
    disable_mouse_control,
)


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

    one_shot = bool(p.get("one_shot", True))
    watcher_mgr = WatcherManager.get_instance(player=player)
    res = watcher_mgr.watch_contact_messages(contact_name=contact_name, app_filter=app_filter, one_shot=one_shot)
    return json.dumps(res)


def stop_watching_contact(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Stops monitoring system toast notifications for contact_name."""
    p = dict(parameters or {})
    p.update(kwargs)
    contact_name = str(p.get("contact_name") or p.get("contact") or "").strip()
    if not contact_name:
        return "A 'contact_name' is required to stop watching messages."

    watcher_mgr = WatcherManager.get_instance(player=player)
    res = watcher_mgr.stop_watching_contact(contact_name=contact_name)
    return json.dumps(res)


def watch_antigravity_task(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Monitors Google Antigravity processes, UI, and logs for task completion or review approval."""
    p = dict(parameters or {})
    p.update(kwargs)
    workspace_hint = str(p.get("workspace_hint") or "").strip()
    watcher_mgr = WatcherManager.get_instance(player=player)
    res = watcher_mgr.watch_antigravity_task(workspace_hint=workspace_hint)
    return json.dumps(res)


def stop_watching_antigravity(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """Stops monitoring Google Antigravity processes and logs."""
    watcher_mgr = WatcherManager.get_instance(player=player)
    res = watcher_mgr.stop_watching_antigravity()
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
                "one_shot": {
                    "type": "BOOLEAN",
                    "description": "True to remove watch rule automatically after triggering once (default True)",
                },
            },
            "required": ["contact_name"],
        },
        "handler": watch_contact_messages,
    },
    {
        "name": "stop_watching_contact",
        "description": "Stops monitoring toast notifications for a specific contact.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "contact_name": {
                    "type": "STRING",
                    "description": "Name of contact to stop watching",
                }
            },
            "required": ["contact_name"],
        },
        "handler": stop_watching_contact,
    },
    {
        "name": "watch_antigravity_task",
        "description": (
            "Monitors Google Antigravity CLI and IDE processes, UI state, and logs for task completion or "
            "pauses waiting for review approval, proactively alerting the user verbally."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "workspace_hint": {
                    "type": "STRING",
                    "description": "Optional workspace path hint to monitor active .gemini logs",
                }
            },
        },
        "handler": watch_antigravity_task,
    },
    {
        "name": "stop_watching_antigravity",
        "description": "Stops monitoring Google Antigravity agent execution and logs.",
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
        "handler": stop_watching_antigravity,
    },
    {
        "name": "enable_gesture_mode",
        "description": (
            "Activates on-demand local MediaPipe hand gesture control mode to control media, "
            "approve/reject worker tasks, and trigger voice listening from across the room."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
        "handler": enable_gesture_mode,
    },
    {
        "name": "disable_gesture_mode",
        "description": (
            "Deactivates hand gesture control mode and immediately closes the webcam to ensure 0% CPU usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
        "handler": disable_gesture_mode,
    },
    {
        "name": "get_gesture_status",
        "description": "Returns whether gesture control mode is active and the current state machine status.",
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
        "handler": get_gesture_status,
    },
    {
        "name": "enable_mouse_control",
        "description": "Enables real-time one-finger mouse cursor steering using index fingertip and pinch-to-click.",
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
        "handler": enable_mouse_control,
    },
    {
        "name": "disable_mouse_control",
        "description": "Disables real-time one-finger mouse cursor tracking.",
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
        "handler": disable_mouse_control,
    },
]
