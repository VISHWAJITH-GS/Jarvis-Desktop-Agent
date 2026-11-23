"""
core/prompt_builder.py — Tool declarations and system prompt formatting.
"""

from __future__ import annotations

import json
from pathlib import Path
from core.runtime_env import BASE_DIR, API_CONFIG_PATH, PROMPT_PATH

TOOL_DECLARATIONS = [
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the computer screen image and lets you describe or analyze it visually. "
            "MUST be called when the user explicitly asks what is on screen, analyze my screen, "
            "describe what is visible on screen, etc. "
            "NEVER call this tool to find things to click, open folders, or navigate UI — "
            "use windows_uia or computer_control by element name instead. "
            "After the screenshot is captured it is sent directly to you — describe what you see and answer the user's question. "
            "Webcam/camera capture is disabled and unavailable."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text": {"type": "STRING", "description": "The question or instruction about the captured screen image"}
            },
            "required": ["text"],
        },
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background news/topic monitors. "
            "When a topic is added, JARVIS checks the web for developments throughout the day "
            "and speaks alerts when new headlines appear. "
            "action: 'add', 'remove', or 'list'. topic: query string (e.g. 'SpaceX launch', 'Bitcoin')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "'add', 'remove', or 'list'"},
                "topic": {"type": "STRING", "description": "Topic to monitor (required for add/remove)"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "sleep_jarvis",
        "description": (
            "Puts JARVIS into a low-power background sleep mode. The microphone is closed and "
            "audio stops streaming to Gemini. JARVIS will only wake up when the user speaks "
            "the wake word ('Jarvis' or 'Hello Jarvis') or presses the keyboard shortcut (Ctrl+Alt+K). "
            "Use this when the user says 'go to sleep', 'sleep', 'take a rest', 'goodnight', etc."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely (requires high-impact voice + visual confirmation). "
            "Use when the user asks to exit, close, terminate, or power down JARVIS."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important fact, user preference, project detail, or piece of knowledge "
            "into persistent long-term memory so you remember it in future sessions. "
            "Categories: user_facts — name, location, family, work | "
            "preferences — communication style, tools, habits | "
            "projects — active projects, goals, things being built | "
            "environment — hardware, software versions, setup notes | "
            "decisions — choices made and the reasoning behind them."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": "One of: user_facts, preferences, projects, environment, decisions, notes",
                },
                "key": {"type": "STRING", "description": "Short identifier, e.g. 'favorite_language'"},
                "value": {"type": "STRING", "description": "The fact to remember"},
            },
            "required": ["category", "key", "value"],
        },
    },
    {
        "name": "recall_memory",
        "description": (
            "Search persistent long-term memory for facts, preferences, or notes saved in previous "
            "sessions. Call this when the user asks 'do you remember...', asks about past preferences, "
            "or refers to something you might have stored earlier."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {"query": {"type": "STRING", "description": "Keywords to search for in memory"}},
            "required": ["query"],
        },
    },
    {
        "name": "undo",
        "description": (
            "Revert the most recent changes made to files or settings by an action or coding task. "
            "This only covers your own actions — it is not the Ctrl+Z of whatever "
            "program is on screen. Call with action='list' to inspect the stack first, or "
            "action='undo' to revert the most recent entry."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "'undo' to revert the last action, 'list' to see what can be undone",
                }
            },
            "required": [],
        },
    },
]


def _describe_tools(declarations) -> str:
    lines = []
    for d in declarations or ():
        try:
            name = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
            desc = (d.get("description") if isinstance(d, dict) else getattr(d, "description", "")) or ""
        except Exception:
            continue
        if not name:
            continue
        desc = " ".join(str(desc).split())
        lines.append(f"- {name}: {desc[:150]}" if desc else f"- {name}")
    return "\n".join(lines)


def _describe_limits(has_vision: bool, has_mic: bool) -> str:
    out = [
        "- Anything not listed above is outside your reach. Say so in one clause "
        "and offer the nearest thing you can actually do — never mime an action "
        "you cannot take, and never report a result you did not get.",
        "- You act on this machine only. You cannot reach the user's other "
        "devices, accounts or hardware except through the tools listed above.",
        "- You remember what is in the memory block and what has been said this "
        "session. Anything else you were told before is gone unless it was saved.",
    ]
    try:
        from core import platform_support

        out.extend(platform_support.describe_for_model(platform_support.detect()))
    except Exception as _e:
        print(f"[Limits] platform capabilities unavailable: {_e}")

    if has_vision:
        out.append(
            "- Your sight is not continuous. You see nothing until you call a "
            "vision tool, and then only that single frame at that moment — you "
            "cannot watch, monitor or notice something changing on screen."
        )
    else:
        out.append("- You have no sight at all in this build.")
    if has_mic:
        out.append("- You hear nothing while the microphone is muted, and you cannot unmute it yourself.")
    return "\n".join(out)


def _render_prompt(template: str, values: dict) -> str:
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _get_api_key() -> str:
    try:
        from memory.config_manager import get_gemini_key

        k = get_gemini_key()
        if k:
            return k
    except Exception:
        pass
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _load_system_prompt() -> str:
    base = ""
    try:
        base = PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        base = (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

    # Check for active mode overlay (e.g. English Placement Practice Mode)
    try:
        from memory import config_manager
        active_mode = config_manager.get_active_mode()
        if active_mode == "english_practice":
            from plugins import _english_mode
            overlay = _english_mode.get_prompt_overlay()
            if overlay:
                base = base.rstrip() + "\n\n" + overlay
    except Exception:
        pass

    return base

