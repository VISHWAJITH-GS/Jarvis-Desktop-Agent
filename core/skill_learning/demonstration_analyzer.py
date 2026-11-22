"""
core/skill_learning/demonstration_analyzer.py — Gemini AI Demonstration & Intent Analyzer.

Interprets raw observation event traces and natural language skill descriptions:
- Uses the REAL ActionRegistry tool catalog (names, descriptions, schemas), not hardcoded tools.
- Emits strict SkillSpec JSON (name, parameters with defaults & examples, preconditions, steps, postconditions).
- Prefers high-level atomic tools (page_to_file, whatsapp_send, browser_control, file_controller) over raw clicks.
- Respects browser independence (config_manager.get_browser_engine(), default Chrome).
- Supports "Learn from description" mode without screen demonstration.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from core import gemini as _gemini
from core.action_loader import get_default_registry
from core.skill_learning.models import (
    ParameterDefinition,
    StepDefinition,
    UIEvent,
    VerificationCondition,
)
from core.skill_learning.skill_security import sanitize_text_and_credentials
from memory.config_manager import get_browser_engine


class DemonstrationAnalyzer:
    """Analyzes demonstrated user interactions and descriptions using Gemini with real action catalogs."""

    def __init__(self, action_registry=None):
        self.action_registry = action_registry or get_default_registry()

    def _format_action_catalog(self) -> str:
        """Format registered tools, descriptions, and parameter schemas for the model prompt."""
        lines = []
        action_names = (
            self.action_registry.list_actions()
            if hasattr(self.action_registry, "list_actions")
            else sorted(self.action_registry.names())
        )
        for name in action_names:
            decl = self.action_registry.get_declaration(name) or {}
            desc = decl.get("description", "").strip()
            params = decl.get("parameters", {})
            props = params.get("properties", {}) if isinstance(params, dict) else {}
            prop_strs = []
            for p_name, p_info in props.items():
                p_type = p_info.get("type", "string") if isinstance(p_info, dict) else "string"
                p_desc = p_info.get("description", "") if isinstance(p_info, dict) else ""
                prop_strs.append(f"{p_name}: {p_type} ({p_desc})")
            lines.append(f"- Tool '{name}': {desc}\n  Parameters: {', '.join(prop_strs) or 'None'}")
        return "\n".join(lines)

    def analyze_demonstration(
        self,
        goal: str,
        events: list[UIEvent],
        trace_summary: str,
    ) -> dict:
        """Analyze demonstration trace and return structured SkillSpec workflow analysis."""
        if not events:
            return self._empty_analysis(goal)

        sanitized_goal = sanitize_text_and_credentials(goal)
        sanitized_trace = sanitize_text_and_credentials(trace_summary)
        browser_choice = get_browser_engine().capitalize()
        catalog_str = self._format_action_catalog()

        print("[Analyzer] Sending sanitized trace with real tool catalog to Gemini.")

        prompt = f"""You are MARK LVI's Senior AI Skill Architect.
Analyze the following recorded user demonstration of a Windows desktop workflow and convert it into a reusable declarative SkillSpec.

USER GOAL / INTENT: "{sanitized_goal}"

CONFIGURED BROWSER: "{browser_choice}" (Use this browser or dynamic browser tools; NEVER hardcode 'Chrome' if configured otherwise).

AVAILABLE TOOLS CATALOG (You MUST choose tools ONLY from this real catalog):
{catalog_str}

DEMONSTRATION EVENT TRACE:
{sanitized_trace}

INSTRUCTIONS:
1. Identify primary application and workflow intent.
2. PREFER HIGH-LEVEL ATOMIC TOOLS over raw UI clicks and keystrokes:
   - If the user copied text from a web page/chat and saved it to a file: use 'page_to_file' (or 'copy_page_text_to_clipboard' + 'file_controller'), NOT 15 clicks and a paste.
   - If the user sent a WhatsApp message: use 'whatsapp_send', NOT clicking search, typing contact, clicking chat, typing text, clicking send.
   - Use 'browser_control' for web navigation/actions, 'file_controller' for reading/writing files, and 'open_app' for launching applications.
   - Use 'windows_uia' only when no high-level tool exists in the catalog.
3. Extract dynamic reusable parameters:
   - file paths, prompts, contact names, URLs, folder paths
   - Provide example values extracted from the demonstration (e.g. chat_name: "Ram Nath TCE", filename: "report.pdf", folder: "Documents")
4. CRITICAL SECURITY: Never put actual passwords, PINs, auth tokens, or private credentials in the skill steps. Parameterize them with is_credential=true.
5. Define concrete expected_outcome and verification for each step (e.g. "file exists at {{path}}", "window title contains {{name}}").
6. Define postconditions: what true state change marks overall success.

OUTPUT FORMAT:
Return ONLY a valid JSON object matching this exact schema:
{{
  "skill_name": "Export Chat to File",
  "skill_id": "export_chat_to_file",
  "application": "Browser",
  "description": "Exports chat text from web page into a local text file.",
  "intent_examples": ["export this chat to a file", "save chat text to desktop"],
  "parameters": [
    {{"name": "file_path", "type": "file_path", "description": "Destination file path", "required": true, "example": "C:/Users/Report/chat.txt", "default": null}},
    {{"name": "format", "type": "string", "description": "Export format", "required": false, "example": "txt", "default": "txt"}}
  ],
  "preconditions": ["Browser is open with target conversation"],
  "steps": [
    {{
      "step_id": 1,
      "thought": "Extract page chat content and save to file atomically",
      "tool": "page_to_file",
      "parameters": {{"destination_path": "{{file_path}}", "format": "{{format}}"}},
      "expected_outcome": "file exists at {{file_path}}",
      "verification": {{"type": "file_exists", "target": "{{file_path}}"}}
    }}
  ],
  "postconditions": ["Chat text is saved to target file"]
}}
"""
        try:
            raw = _gemini.text(prompt, tier=_gemini.SMART)
            if raw:
                clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
                clean = re.sub(r"\s*```$", "", clean.strip(), flags=re.MULTILINE)
                parsed = json.loads(clean)
                if isinstance(parsed, dict) and "steps" in parsed:
                    print("[Analyzer] Workflow analysis completed.")
                    return parsed
        except Exception as e:
            print(f"[DemonstrationAnalyzer] Gemini analysis fallback due to: {e}")

        fallback = self._deterministic_fallback_analysis(sanitized_goal, events)
        print("[Analyzer] Workflow analysis completed.")
        return fallback

    def analyze_description(self, goal: str, description: str) -> dict:
        """Learn from description mode: generate SkillSpec directly from natural language instruction."""
        sanitized_goal = sanitize_text_and_credentials(goal)
        sanitized_desc = sanitize_text_and_credentials(description)
        browser_choice = get_browser_engine().capitalize()
        catalog_str = self._format_action_catalog()

        prompt = f"""You are MARK LVI's Senior AI Skill Architect.
The user wants to teach a new desktop skill entirely through natural language description, WITHOUT a visual demonstration.

USER GOAL: "{sanitized_goal}"
USER DESCRIPTION: "{sanitized_desc}"

CONFIGURED BROWSER: "{browser_choice}"

AVAILABLE TOOLS CATALOG (Choose tools ONLY from this catalog):
{catalog_str}

INSTRUCTIONS:
1. Translate the user's description into an optimal sequence of tool calls.
2. PREFER HIGH-LEVEL ATOMIC TOOLS:
   - For copying/saving chat or web page content to a file: use 'page_to_file'.
   - For WhatsApp messaging: use 'whatsapp_send'.
   - For web navigation: use 'browser_control'.
   - For file ops: use 'file_controller'.
   - For app launch: use 'open_app'.
3. Parameterize dynamic variables (e.g. {{file_path}}, {{chat_name}}, {{message}}, {{url}}) with types, defaults, and examples.
4. Set concrete expected_outcome for each step and postconditions.

OUTPUT FORMAT:
Return ONLY a valid JSON object matching the SkillSpec schema (skill_name, skill_id, application, description, intent_examples, parameters, preconditions, steps, postconditions).
"""
        try:
            raw = _gemini.text(prompt, tier=_gemini.SMART)
            if raw:
                clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
                clean = re.sub(r"\s*```$", "", clean.strip(), flags=re.MULTILINE)
                parsed = json.loads(clean)
                if isinstance(parsed, dict) and "steps" in parsed:
                    return parsed
        except Exception as e:
            print(f"[DemonstrationAnalyzer] Gemini description analysis fallback: {e}")

        return self._deterministic_description_analysis(sanitized_goal, sanitized_desc)

    def _deterministic_fallback_analysis(self, goal: str, events: list[UIEvent]) -> dict:
        """Deterministic workflow extraction when offline or in tests, preferring high-level tools."""
        browser_name = get_browser_engine().capitalize()
        goal_lower = goal.lower()
        slug = re.sub(r"[^a-z0-9_]+", "_", goal_lower.strip())[:40].strip("_") or "demonstrated_skill"

        # Check for High-Level Intent 1: Web chat / page to file
        is_page_to_file = any(kw in goal_lower for kw in ("save chat", "copy chat", "page to file", "export chat", "chat to file"))
        if is_page_to_file or any("chat" in (e.window_title or "").lower() for e in events if e.event_type == "clipboard"):
            return {
                "skill_name": goal.capitalize(),
                "skill_id": slug,
                "application": browser_name,
                "description": f"Exports web chat content into a file: {goal}",
                "intent_examples": [goal, f"Run {goal}"],
                "parameters": [
                    {
                        "name": "destination_path",
                        "type": "file_path",
                        "description": "Path to output file",
                        "required": True,
                        "example": "C:/Users/Desktop/chat.txt",
                        "default": None,
                    }
                ],
                "preconditions": [f"{browser_name} is open with the target conversation"],
                "steps": [
                    {
                        "step_id": 1,
                        "thought": "Extract active chat and save atomically to file",
                        "tool": "page_to_file",
                        "parameters": {"destination_path": "{destination_path}", "format": "txt"},
                        "expected_outcome": "file exists at {destination_path}",
                        "verification": {"type": "file_exists", "target": "{destination_path}"},
                    }
                ],
                "postconditions": ["Chat text is saved to target file"],
            }

        # Check for High-Level Intent 2: WhatsApp send
        if "whatsapp" in goal_lower or any("whatsapp" in (e.app_name or "").lower() for e in events):
            return {
                "skill_name": goal.capitalize(),
                "skill_id": slug,
                "application": "WhatsApp",
                "description": f"Sends a WhatsApp message: {goal}",
                "intent_examples": [goal],
                "parameters": [
                    {"name": "recipient", "type": "string", "description": "Contact or chat name", "required": True, "example": "Ram Nath TCE"},
                    {"name": "message", "type": "string", "description": "Message content", "required": True, "example": "Meeting at 3 PM"},
                ],
                "preconditions": ["WhatsApp Web is logged in"],
                "steps": [
                    {
                        "step_id": 1,
                        "thought": "Send WhatsApp message directly via high-level tool",
                        "tool": "whatsapp_send",
                        "parameters": {"recipient": "{recipient}", "message": "{message}"},
                        "expected_outcome": "message sent to {recipient}",
                        "verification": {"type": "custom_rule", "target": "whatsapp_send_verified"},
                    }
                ],
                "postconditions": ["Message delivered to {recipient}"],
            }

        # General event-based synthesis
        apps = {e.app_name for e in events if e.app_name and e.app_name not in ("Unknown", "Desktop", "JarvisVoice", "Browser")}
        raw_app = list(apps)[0] if apps else browser_name
        primary_app = browser_name if raw_app.lower() in ("browser", "chrome", "edge", "msedge") else raw_app
        steps = []
        parameters = []
        step_id = 1

        for e in events:
            if e.event_type == "navigation" and e.url:
                steps.append({
                    "step_id": step_id,
                    "thought": f"Navigate to URL {e.url}",
                    "tool": "browser_control",
                    "parameters": {"action": "navigate", "url": e.url},
                    "expected_outcome": f"browser URL is {e.url}",
                    "verification": {"type": "window_title_contains", "target": primary_app},
                })
                step_id += 1
            elif e.event_type == "window_activate":
                app_target = browser_name if primary_app.lower() in ("chrome", "msedge", "edge", "browser") else e.app_name
                steps.append({
                    "step_id": step_id,
                    "thought": f"Switch to window '{e.window_title}'",
                    "tool": "open_app",
                    "parameters": {"app_name": app_target},
                    "expected_outcome": f"active window is {app_target}",
                    "verification": {"type": "window_title_contains", "target": app_target},
                })
                step_id += 1
            elif e.event_type in ("click", "double_click"):
                elem_name = e.element_name or e.selector.get("name", "Button")
                steps.append({
                    "step_id": step_id,
                    "thought": f"Click '{elem_name}' in {e.app_name}",
                    "tool": "windows_uia",
                    "parameters": {"action": "click", "name": elem_name, "control_type": e.control_type or "Button"},
                    "expected_outcome": f"element '{elem_name}' clicked",
                    "verification": {"type": "element_visible", "target": elem_name},
                })
                step_id += 1
            elif e.event_type == "type_text" and e.input_value:
                param_name = "password" if e.is_secret else "input_text"
                parameters.append({
                    "name": param_name,
                    "type": "string",
                    "description": "Secure password" if e.is_secret else "Text input",
                    "required": True,
                    "is_credential": e.is_secret,
                    "example": "[SECRET]" if e.is_secret else "Sample text",
                })
                steps.append({
                    "step_id": step_id,
                    "thought": f"Type {{{param_name}}} into {e.element_name}",
                    "tool": "windows_uia",
                    "parameters": {"action": "type_text", "text": f"{{{param_name}}}", "name": e.element_name},
                    "expected_outcome": f"text entered into {e.element_name}",
                    "verification": {"type": "element_visible", "target": e.element_name},
                    "is_sensitive": e.is_secret,
                })
                step_id += 1

        return {
            "skill_name": goal.capitalize(),
            "skill_id": slug,
            "application": primary_app,
            "description": f"Workflow learned from demonstration: {goal}",
            "intent_examples": [goal, f"Run {goal}", f"Execute {goal}"],
            "parameters": parameters,
            "preconditions": [f"{primary_app} is installed and available"],
            "steps": steps,
            "postconditions": [f"{goal} completed"],
        }

    def _deterministic_description_analysis(self, goal: str, description: str) -> dict:
        """Deterministic description-based SkillSpec synthesis for offline mode."""
        slug = re.sub(r"[^a-z0-9_]+", "_", goal.lower().strip())[:40].strip("_") or "described_skill"
        desc_lower = description.lower()
        browser_name = get_browser_engine().capitalize()

        if "chat" in desc_lower and ("file" in desc_lower or "save" in desc_lower or "copy" in desc_lower):
            return {
                "skill_name": goal.capitalize(),
                "skill_id": slug,
                "application": browser_name,
                "description": description,
                "intent_examples": [goal, f"Run {goal}"],
                "parameters": [
                    {
                        "name": "destination_path",
                        "type": "file_path",
                        "description": "Destination file path for chat export",
                        "required": True,
                        "example": "C:/Users/Desktop/chat.txt",
                        "default": None,
                    }
                ],
                "preconditions": [f"{browser_name} is open with target chat"],
                "steps": [
                    {
                        "step_id": 1,
                        "thought": "Extract active chat and save atomically to file",
                        "tool": "page_to_file",
                        "parameters": {"destination_path": "{destination_path}", "format": "txt"},
                        "expected_outcome": "file exists at {destination_path}",
                        "verification": {"type": "file_exists", "target": "{destination_path}"},
                    }
                ],
                "postconditions": ["Chat text is saved to target file"],
            }

        return {
            "skill_name": goal.capitalize(),
            "skill_id": slug,
            "application": "Desktop",
            "description": description,
            "intent_examples": [goal],
            "parameters": [
                {"name": "param", "type": "string", "description": "Target parameter", "required": False, "default": "", "example": "test"}
            ],
            "preconditions": [],
            "steps": [
                {
                    "step_id": 1,
                    "thought": f"Execute action for {goal}",
                    "tool": "open_app",
                    "parameters": {"app_name": "Notepad"},
                    "expected_outcome": "Notepad is open",
                    "verification": {"type": "window_title_contains", "target": "Notepad"},
                }
            ],
            "postconditions": [f"{goal} finished"],
        }

    def _empty_analysis(self, goal: str) -> dict:
        return {
            "skill_name": goal,
            "skill_id": "empty_skill",
            "application": "Desktop",
            "description": "No events recorded.",
            "intent_examples": [goal],
            "parameters": [],
            "preconditions": [],
            "steps": [],
            "postconditions": [],
        }
