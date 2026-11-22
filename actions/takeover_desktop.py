"""
actions/takeover_desktop.py — Autonomous Windows Desktop Takeover Action for Mark-LVI

Exposes the central autonomous agent orchestrator as an auto-discovered action
so it is loaded by core/action_loader.py and registered into the LLM function schemas.
Metadata is kept lightweight so discovery never imports heavy GUI or agent dependencies.
"""

from __future__ import annotations


def takeover_desktop(parameters: dict, player=None) -> str:
    """Lazy handler delegating to core.agent_orchestrator on-demand, with direct routing for deterministic tasks."""
    params = dict(parameters or {})
    goal = str(params.get("goal") or "").strip()
    goal_lower = goal.lower()

    # Routing rule: simple deterministic page/chat extraction to file uses page_to_file directly
    if (
        any(w in goal_lower for w in ("chatgpt", "chat", "conversation", "webpage", "page"))
        and any(w in goal_lower for w in ("save", "copy", "export", "dump", "write"))
        and any(w in goal_lower for w in ("file", "notepad", "txt", "md", "desktop", "document"))
    ):
        try:
            from actions.page_to_file import page_to_file
            return page_to_file({"source": "active_tab", "goal": goal}, player=player)
        except Exception:
            pass

    from core import agent_orchestrator

    return agent_orchestrator.takeover_desktop(parameters, player=player)


TOOL = {
    "name": "takeover_desktop",
    "description": (
        "AUTONOMOUS WINDOWS DESKTOP CONTROL: Takes complete control of the Windows desktop "
        "to fulfill multi-step instructions (e.g. 'Connect my earbuds, open Spotify and play One Name', "
        "'Open Chrome and test my website', 'Click where my cursor is pointing'). "
        "Executes a continuous Observe-Understand-Plan-Resolve-Act-Wait-Reobserve-Verify loop "
        "using Windows UI Automation, multi-monitor perception, Playwright browser QA, and Gemini. "
        "Always call this tool when the user requests multi-step compound desktop operations."
    ),
    "brief": "Autonomous desktop takeover: observe, plan, act and verify multi-step Windows actions.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {
                "type": "STRING",
                "description": "The natural language instruction or goal for the desktop agent.",
            },
        },
        "required": ["goal"],
    },
    "handler": takeover_desktop,
}
