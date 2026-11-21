"""
actions/tasks.py — Background autonomy tools exposed to the JARVIS assistant.

Exposes:
- start_task: launch a background task with goal, budget, and tool allowlist
- list_tasks: query active and finished background tasks
- task_status: inspect progress, rounds, findings, and confirmation state
- steer_task: supply mid-flight guidance notes for the next round
- stop_task: cancel or request early report finish
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

from core.tasks import (
    cancel_task,
    get_task_engine,
    steer_task as do_steer,
)
from core.tasks.control import get_default_store


def start_task(parameters: dict, player=None) -> str:
    """Launch a background autonomous task."""
    prompt = parameters.get("prompt") or parameters.get("goal") or ""
    if not prompt:
        return "Please specify a goal or prompt for the background task."

    title = parameters.get("title") or (prompt[:40] + ("..." if len(prompt) > 40 else ""))
    time_budget = float(parameters.get("time_budget_sec", 300.0))
    round_budget = int(parameters.get("round_budget", 10))
    allow_tools = parameters.get("allow_tools")

    task_id = f"task_{uuid.uuid4().hex[:8]}"
    engine = get_task_engine()
    engine.start()
    task = engine.queue_task(
        task_id=task_id,
        title=title,
        description=prompt,
        time_budget_sec=time_budget,
        round_budget=round_budget,
        allowlist=allow_tools,
    )

    if player and hasattr(player, "write_log"):
        player.write_log(f"SYS: Started background task '{title}' ({task_id})")

    return f"Started background task '{title}' with ID {task_id}."


def list_tasks(parameters: dict = None, player=None) -> str:
    """List current and recent background tasks."""
    state_filter = (parameters or {}).get("status")
    if state_filter == "all":
        state_filter = None

    store = get_default_store()
    tasks = store.list_tasks(state=state_filter, limit=10)
    if not tasks:
        return "No background tasks found."

    lines = []
    for t in tasks:
        lines.append(f"• [{t.state.upper()}] {t.title} (ID: {t.id}, Round: {t.round_count}/{t.round_budget})")

    summary_text = "\n".join(lines)
    if player and hasattr(player, "show_content"):
        player.show_content("Background Tasks", summary_text)

    active_count = sum(1 for t in tasks if t.state in ("running", "queued", "waiting_quota", "needs_confirmation"))
    return f"You have {active_count} active background task{'s' if active_count != 1 else ''}. Check the display panel for details."


def task_status(parameters: dict, player=None) -> str:
    """Get status and progress for a specific task."""
    task_id = (parameters or {}).get("task_id", "").strip()
    if not task_id:
        return "Please provide a task_id to check status."

    store = get_default_store()
    task = store.get_task(task_id)
    if not task:
        return f"Task '{task_id}' was not found."

    status_lines = [
        f"Task: {task.title} ({task.id})",
        f"State: {task.state}",
        f"Progress: Round {task.round_count}/{task.round_budget} ({task.elapsed_sec:.1f}s / {task.time_budget_sec:.1f}s)",
    ]

    if task.state == "needs_confirmation" and task.pending_confirmation:
        p_act = task.pending_confirmation.get("action", "unknown")
        status_lines.append(f"⚠️ Action '{p_act}' is waiting for confirmation.")

    if task.result:
        status_lines.append(f"\nFinal Result:\n{task.result}")
    elif task.error:
        status_lines.append(f"\nError: {task.error}")

    full_info = "\n".join(status_lines)
    if player and hasattr(player, "show_content"):
        player.show_content(f"Task: {task.title}", full_info)

    if task.state == "done":
        return f"Task '{task.title}' is completed. Check the screen for the full summary."
    elif task.state == "needs_confirmation":
        act = task.pending_confirmation.get("action") if task.pending_confirmation else "action"
        return f"Task '{task.title}' is paused waiting for your confirmation on {act}."
    else:
        return f"Task '{task.title}' is currently {task.state} at round {task.round_count}."


def steer_task(parameters: dict, player=None) -> str:
    """Inject steering notes into an in-flight background task."""
    task_id = (parameters or {}).get("task_id", "").strip()
    note = (parameters or {}).get("note", "").strip()
    if not task_id or not note:
        return "Please provide both task_id and note."

    success = do_steer(task_id, note)
    if success:
        if player and hasattr(player, "write_log"):
            player.write_log(f"SYS: Steered task {task_id}: {note}")
        return f"Delivered steering guidance to task {task_id}."
    return f"Could not steer task {task_id}. Make sure it is active and not already finished."


def stop_task(parameters: dict, player=None) -> str:
    """Stop or request graceful wrap-up of a background task."""
    task_id = (parameters or {}).get("task_id", "").strip()
    finish_early = bool((parameters or {}).get("finish", False))
    if not task_id:
        return "Please provide a task_id to stop."

    success = cancel_task(task_id, finish=finish_early)
    if success:
        if finish_early:
            msg = f"Task {task_id} instructed to wrap up and synthesize final report."
        else:
            msg = f"Task {task_id} cancelled."
        if player and hasattr(player, "write_log"):
            player.write_log(f"SYS: {msg}")
        return msg
    return f"Failed to stop task {task_id}. It may already be completed or cancelled."


TOOLS = [
    {
        "name": "start_task",
        "description": "Start a long-running background research or automation task with time and round budgets.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "prompt": {"type": "STRING", "description": "Goal or instructions for the background task"},
                "title": {"type": "STRING", "description": "Short human-readable title for the task"},
                "time_budget_sec": {
                    "type": "NUMBER",
                    "description": "Maximum seconds allowed before synthesizing report (default 300)",
                },
                "round_budget": {"type": "INTEGER", "description": "Maximum reasoning rounds allowed (default 10)"},
            },
            "required": ["prompt"],
        },
        "handler": start_task,
        "permission": "normal",
    },
    {
        "name": "list_tasks",
        "description": "List all active, queued, or recently completed background tasks.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"status": {"type": "STRING", "description": "Filter by status: 'active', 'done', 'all'"}},
        },
        "handler": list_tasks,
        "permission": "read_only",
    },
    {
        "name": "task_status",
        "description": "Check the progress, findings, and result of a specific background task by ID.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"task_id": {"type": "STRING", "description": "The unique ID of the background task"}},
            "required": ["task_id"],
        },
        "handler": task_status,
        "permission": "read_only",
    },
    {
        "name": "steer_task",
        "description": "Send a steering or correction note to guide an active background task.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task_id": {"type": "STRING", "description": "The unique ID of the task to steer"},
                "note": {"type": "STRING", "description": "Instructions or guidance to deliver in the next round"},
            },
            "required": ["task_id", "note"],
        },
        "handler": steer_task,
        "permission": "normal",
    },
    {
        "name": "stop_task",
        "description": "Cancel an active background task or tell it to finish early and produce its final report.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task_id": {"type": "STRING", "description": "The unique ID of the task to stop"},
                "finish": {
                    "type": "BOOLEAN",
                    "description": "If true, finish early and write final report; if false, cancel immediately",
                },
            },
            "required": ["task_id"],
        },
        "handler": stop_task,
        "permission": "normal",
    },
]
