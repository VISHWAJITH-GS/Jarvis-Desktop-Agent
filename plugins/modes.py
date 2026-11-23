"""
plugins/modes.py — Unified Operating Modes Manager for MARK LVI / JARVIS.

Coordinates:
- Focus Mode (Distraction & adult domain blocking, polite process monitoring, browser tab closer)
- English Placement Practice Mode (Campus placement mock interviews, feedback engine, mistake flashcards)

Configured via memory/config_manager.py and stored in config/api_keys.json.
"""

from __future__ import annotations

import logging
from typing import Optional

from memory import config_manager
from plugins import _english_mode, _focus_mode

logger = logging.getLogger(__name__)

PLUGIN = {
    "name": "modes",
    "description": (
        "Controls system operating modes: Focus Mode (blocking distractions and adult sites for deep work) "
        "and English Placement Practice Mode (campus placement interview coaching, STAR method, speaking practice). "
        "Use when user asks to turn focus mode on or off, start English practice, change interview scenario, "
        "or check mode status."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "The mode action to perform: 'on', 'off', 'toggle', 'status', 'list', "
                    "'set_scenario', 'set_feedback_style', 'show_mistakes_quiz', 'log_mistake'."
                ),
                "enum": [
                    "on",
                    "off",
                    "toggle",
                    "status",
                    "list",
                    "set_scenario",
                    "set_feedback_style",
                    "show_mistakes_quiz",
                    "log_mistake",
                ],
            },
            "mode": {
                "type": "STRING",
                "description": "Target mode: 'focus', 'english_practice', or 'all'.",
                "enum": ["focus", "english_practice", "all"],
            },
            "scenario": {
                "type": "STRING",
                "description": (
                    "Scenario for English practice: 'self_intro', 'hr_round', 'project_explain', "
                    "'code_walkthrough', 'group_discussion', 'presentation'."
                ),
                "enum": [
                    "self_intro",
                    "hr_round",
                    "project_explain",
                    "code_walkthrough",
                    "group_discussion",
                    "presentation",
                ],
            },
            "feedback_style": {
                "type": "STRING",
                "description": "Feedback timing for English practice: 'after_answer', 'end_of_session', 'on_request'.",
                "enum": ["after_answer", "end_of_session", "on_request"],
            },
            "wrong_phrase": {
                "type": "STRING",
                "description": "The incorrect or unpolished phrase spoken by the user during English practice.",
            },
            "corrected_phrase": {
                "type": "STRING",
                "description": "The polished, professional English replacement phrase.",
            },
            "reason": {
                "type": "STRING",
                "description": "One-line explanation of the grammar, vocabulary, or structural improvement.",
            },
        },
        "required": ["action"],
    },
    "permissions": [
        {
            "id": "system:modes_toggle",
            "label": "Toggle System Modes",
            "description": "Switch system operating modes (Focus Mode, English Practice Mode)",
            "tier": "NORMAL",
            "default_policy": "ALLOW",
        }
    ],
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """
    Execute mode action. Dispatches to _focus_mode or _english_mode.
    Returns a concise natural-language response spoken back to the user.
    """
    action = str(parameters.get("action") or "status").strip().lower()
    mode = str(parameters.get("mode") or "").strip().lower()
    scenario = parameters.get("scenario")
    feedback_style = parameters.get("feedback_style")

    try:
        # Default mode resolution if not specified
        if not mode:
            if scenario or feedback_style:
                mode = "english_practice"
            elif action in ("set_scenario", "set_feedback_style", "show_mistakes_quiz"):
                mode = "english_practice"
            else:
                active = config_manager.get_active_mode()
                mode = active if active else "focus"

        if action == "list":
            cfg = config_manager.get_modes_config()
            modes = cfg.get("modes", {})
            active = cfg.get("active_mode")
            lines = ["Available modes:"]
            for m_id, m_data in modes.items():
                name = m_data.get("name", m_id)
                desc = m_data.get("description", "")
                is_act = " (ACTIVE)" if m_id == active else ""
                lines.append(f"• {name}{is_act}: {desc}")
            return "\n".join(lines)

        elif action == "status":
            act_mode = config_manager.get_active_mode()
            focus_act = _focus_mode.is_focus_mode_active()
            eng_status = _english_mode.get_english_mode_status()

            status_lines = []
            if focus_act or act_mode == "focus":
                status_lines.append("Focus Mode is currently ACTIVE.")
            else:
                status_lines.append("Focus Mode is off.")

            if eng_status.get("active") or act_mode == "english_practice":
                scen = eng_status.get("scenario_title", eng_status.get("scenario"))
                status_lines.append(f"English Practice Mode is ACTIVE (Scenario: {scen}).")
            else:
                status_lines.append("English Practice Mode is off.")

            return " ".join(status_lines)

        elif action == "show_mistakes_quiz":
            return _english_mode.show_mistake_quiz(player=player)

        elif action == "log_mistake":
            wrong = str(parameters.get("wrong_phrase") or "").strip()
            corr = str(parameters.get("corrected_phrase") or "").strip()
            reason = str(parameters.get("reason") or "").strip()
            scen = str(parameters.get("scenario") or config_manager.get_mode_settings("english_practice").get("scenario", "general")).strip()
            if not wrong or not corr:
                return "Please provide both 'wrong_phrase' and 'corrected_phrase' to log a mistake."
            entry = _english_mode.log_mistake(wrong, corr, reason, scenario=scen)
            return f"Logged mistake: '{wrong}' -> '{corr}'."


        elif action == "set_scenario":
            if not scenario:
                return "Please specify a scenario (self_intro, hr_round, project_explain, code_walkthrough, group_discussion, or presentation)."
            config_manager.update_mode_settings("english_practice", {"scenario": scenario})
            if config_manager.get_active_mode() == "english_practice":
                # Reload mid-session overlay
                _english_mode.activate_english_mode(scenario=scenario, player=player)
            return f"English practice scenario set to '{scenario}'."

        elif action == "set_feedback_style":
            if not feedback_style:
                return "Please specify a feedback style (after_answer, end_of_session, or on_request)."
            config_manager.update_mode_settings("english_practice", {"feedback_style": feedback_style})
            if config_manager.get_active_mode() == "english_practice":
                _english_mode.activate_english_mode(feedback_style=feedback_style, player=player)
            return f"English practice feedback style set to '{feedback_style}'."

        elif action == "on":
            if mode == "focus":
                ok, msg = _focus_mode.activate_focus_mode(player=player)
                return msg
            elif mode == "english_practice":
                scen = scenario or config_manager.get_mode_settings("english_practice").get("scenario", "self_intro")
                fb = feedback_style or config_manager.get_mode_settings("english_practice").get("feedback_style", "after_answer")
                ok, msg = _english_mode.activate_english_mode(scenario=scen, feedback_style=fb, player=player)
                return msg
            else:
                return f"Unknown mode '{mode}'. Choose 'focus' or 'english_practice'."

        elif action == "off":
            if mode == "focus":
                ok, msg = _focus_mode.deactivate_focus_mode(player=player)
                return msg
            elif mode == "english_practice":
                ok, msg = _english_mode.deactivate_english_mode(player=player)
                return msg
            elif mode == "all":
                msg1 = _focus_mode.deactivate_focus_mode(player=player)[1]
                msg2 = _english_mode.deactivate_english_mode(player=player)[1]
                return f"{msg1} {msg2}"
            else:
                return f"Unknown mode '{mode}'."

        elif action == "toggle":
            if mode == "focus":
                if _focus_mode.is_focus_mode_active():
                    return _focus_mode.deactivate_focus_mode(player=player)[1]
                else:
                    return _focus_mode.activate_focus_mode(player=player)[1]
            elif mode == "english_practice":
                if config_manager.get_active_mode() == "english_practice":
                    return _english_mode.deactivate_english_mode(player=player)[1]
                else:
                    scen = scenario or config_manager.get_mode_settings("english_practice").get("scenario", "self_intro")
                    return _english_mode.activate_english_mode(scenario=scen, player=player)[1]
            else:
                return f"Unknown mode '{mode}'."

        else:
            return f"Unknown action '{action}' for modes plugin."

    except Exception as e:
        logger.error(f"[ModesPlugin] Error executing {action}: {e}", exc_info=True)
        return f"Modes action '{action}' encountered an error: {e}"
