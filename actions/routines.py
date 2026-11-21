"""
actions/routines.py — Voice-controlled routine, schedule, and quiet hours management tools.

Exposes:
- create_routine: schedule once, daily, weekly, periodic, startup, or idle actions
- list_routines: list active and scheduled routines
- edit_routine: modify an existing routine's schedule or action
- delete_routine: remove or cancel a routine
- configure_quiet_hours: adjust quiet hours window and speech suppression
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from core.routines import (
    RoutineRecord,
    get_quiet_hours_config,
    get_routine_engine,
    save_quiet_hours_config,
)


def create_routine(parameters: dict, player=None) -> str:
    """Create a new routine or scheduled action."""
    title = (parameters.get("title") or "").strip()
    if not title:
        return "Please specify a title or name for the routine."

    schedule_type = (parameters.get("schedule_type") or "once").lower().strip()
    action_type = (parameters.get("action_type") or "speak").lower().strip()
    schedule_cfg = parameters.get("schedule_config") or {}
    action_cfg = parameters.get("action_config") or {}
    is_exact_time = bool(parameters.get("is_exact_time", False))

    if action_type == "speak" and not action_cfg.get("message"):
        action_cfg["message"] = parameters.get("message") or title

    # Calculate next_run_at
    next_run_at = None
    now = datetime.now()
    if schedule_type == "once":
        due = schedule_cfg.get("due_time") or parameters.get("due_time")
        if due:
            next_run_at = due
            is_exact_time = True
        else:
            next_run_at = (now + timedelta(minutes=5)).isoformat()
    elif schedule_type == "every_n_minutes":
        interval = int(schedule_cfg.get("interval_minutes", 15))
        next_run_at = (now + timedelta(minutes=interval)).isoformat()
    elif schedule_type == "daily":
        t_str = schedule_cfg.get("time", "08:00")
        parts = [int(p) for p in t_str.split(":")]
        target = now.replace(hour=parts[0], minute=parts[1], second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        next_run_at = target.isoformat()
    elif schedule_type == "weekly":
        next_run_at = (now + timedelta(days=7)).isoformat()
    elif schedule_type == "monthly":
        next_run_at = (now + timedelta(days=30)).isoformat()

    routine_id = f"rt_{uuid.uuid4().hex[:8]}"
    record = RoutineRecord(
        id=routine_id,
        title=title,
        schedule_type=schedule_type,
        schedule_config=schedule_cfg,
        action_type=action_type,
        action_config=action_cfg,
        is_exact_time=is_exact_time,
        state="active",
        next_run_at=next_run_at,
    )

    engine = get_routine_engine()
    engine.start()
    engine.store.save_routine(record)

    if player and hasattr(player, "write_log"):
        player.write_log(f"SYS: Created routine '{title}' ({routine_id})")

    return f"Created {schedule_type} routine '{title}' with ID {routine_id}."


def list_routines(parameters: dict = None, player=None) -> str:
    """List all scheduled routines."""
    status_filter = (parameters or {}).get("status")
    if status_filter == "all":
        status_filter = None

    engine = get_routine_engine()
    routines = engine.store.list_routines(state=status_filter, limit=20)
    if not routines:
        return "No scheduled routines found."

    lines = []
    for r in routines:
        nr = f" Next run: {r.next_run_at}" if r.next_run_at else ""
        lines.append(f"• [{r.schedule_type.upper()}] {r.title} ({r.id}) — {r.action_type}{nr}")

    summary_text = "\n".join(lines)
    if player and hasattr(player, "show_content"):
        player.show_content("Scheduled Routines", summary_text)

    active_cnt = sum(1 for r in routines if r.state == "active")
    return f"You have {active_cnt} active routine{'s' if active_cnt != 1 else ''}:\n{summary_text}"


def edit_routine(parameters: dict, player=None) -> str:
    """Edit an existing routine."""
    routine_id = (parameters or {}).get("routine_id", "").strip()
    if not routine_id:
        return "Please specify a routine_id to edit."

    engine = get_routine_engine()
    routine = engine.store.get_routine(routine_id)
    if not routine:
        return f"Routine '{routine_id}' not found."

    if "title" in parameters:
        routine.title = str(parameters["title"])
    if "schedule_type" in parameters:
        routine.schedule_type = str(parameters["schedule_type"])
    if "schedule_config" in parameters:
        routine.schedule_config.update(parameters["schedule_config"])
    if "action_config" in parameters:
        routine.action_config.update(parameters["action_config"])
    if "state" in parameters:
        routine.state = str(parameters["state"])

    engine.store.save_routine(routine)
    if player and hasattr(player, "write_log"):
        player.write_log(f"SYS: Updated routine '{routine.title}' ({routine_id})")

    return f"Updated routine '{routine.title}'."


def delete_routine(parameters: dict, player=None) -> str:
    """Remove a routine."""
    routine_id = (parameters or {}).get("routine_id", "").strip()
    if not routine_id:
        return "Please specify a routine_id to delete."

    engine = get_routine_engine()
    success = engine.store.delete_routine(routine_id)
    if success:
        if player and hasattr(player, "write_log"):
            player.write_log(f"SYS: Deleted routine {routine_id}")
        return f"Deleted routine {routine_id}."
    return f"Could not delete routine {routine_id}. It may not exist."


def configure_quiet_hours(parameters: dict, player=None) -> str:
    """Configure quiet hours window and speech suppression."""
    cfg = get_quiet_hours_config()
    enabled = bool(parameters.get("enabled", cfg.enabled))
    start_time = str(parameters.get("start_time", cfg.start_time))
    end_time = str(parameters.get("end_time", cfg.end_time))

    save_quiet_hours_config(enabled, start_time, end_time)
    status_str = f"from {start_time} to {end_time}" if enabled else "disabled"
    msg = f"Quiet hours are now {status_str}."
    if player and hasattr(player, "write_log"):
        player.write_log(f"SYS: {msg}")
    return msg


TOOLS = [
    {
        "name": "create_routine",
        "description": "Schedule a recurring or delayed action (once, daily, weekly, every N minutes, at startup, after idle).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING", "description": "Title or summary of the routine"},
                "schedule_type": {
                    "type": "STRING",
                    "description": "once | daily | weekly | monthly | every_n_minutes | at_startup | after_idle",
                },
                "action_type": {"type": "STRING", "description": "speak | tool | task"},
                "message": {"type": "STRING", "description": "Message to speak if action_type is speak"},
                "schedule_config": {
                    "type": "OBJECT",
                    "description": "Optional schedule details (due_time, time, interval_minutes, idle_seconds)",
                },
                "action_config": {
                    "type": "OBJECT",
                    "description": "Optional action parameters (message, tool_name, parameters, task_prompt)",
                },
            },
            "required": ["title"],
        },
        "handler": create_routine,
        "permission": "normal",
    },
    {
        "name": "list_routines",
        "description": "List all active, scheduled, or completed routines.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"status": {"type": "STRING", "description": "Filter: 'active', 'completed', 'all'"}},
        },
        "handler": list_routines,
        "permission": "read_only",
    },
    {
        "name": "edit_routine",
        "description": "Modify the schedule, action, or status of an existing routine.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "routine_id": {"type": "STRING", "description": "ID of the routine to edit"},
                "title": {"type": "STRING", "description": "New title"},
                "state": {"type": "STRING", "description": "active | paused | completed | cancelled"},
            },
            "required": ["routine_id"],
        },
        "handler": edit_routine,
        "permission": "normal",
    },
    {
        "name": "delete_routine",
        "description": "Delete a scheduled routine by ID.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"routine_id": {"type": "STRING", "description": "ID of the routine to delete"}},
            "required": ["routine_id"],
        },
        "handler": delete_routine,
        "permission": "normal",
    },
    {
        "name": "configure_quiet_hours",
        "description": "Configure quiet hours speech suppression window (e.g. 22:00 to 07:00).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "enabled": {"type": "BOOLEAN", "description": "Whether quiet hours are active"},
                "start_time": {"type": "STRING", "description": "Start time in HH:MM (e.g. 22:00)"},
                "end_time": {"type": "STRING", "description": "End time in HH:MM (e.g. 07:00)"},
            },
        },
        "handler": configure_quiet_hours,
        "permission": "normal",
    },
]
