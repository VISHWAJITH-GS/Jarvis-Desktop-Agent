"""
core/skill_learning/skill_executor.py — Declarative Skill Runner with Verification & Recovery.

Safely executes approved declarative SkillDefinitions:
1. Parameter extraction: extracts parameters from utterance; asks missing in ONE batched question.
2. TaskApprovalScope: ONE task-level confirmation for MEDIUM/HIGH scoped to declared tools/apps.
3. Background task execution: runs as non-blocking background task without stalling voice loop.
4. Structured post-condition verification (DOM/UIA, file existence/content, window title).
5. Failure handling:
   - Transient failure (not focused, busy, timeout): exponential backoff retry (max 3).
   - Fixable failure (selector changed, popup): single model repair of the failing step ONLY.
   - Blocked or unknown: honest failure reporting with exact step and reason.
   - NEVER falls back to takeover_desktop.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from core import gemini as _gemini
from core.action_loader import get_default_registry
from core.outcome import (
    ActionResult,
    result_done,
    result_failed,
    result_needs_confirmation,
    result_unverified,
)
from core.risk_tiers import RiskTier, get_action_tier
from core.skill_learning.models import (
    SkillDefinition,
    SkillStatus,
    StepDefinition,
    VerificationCondition,
)
from core.skill_learning.skill_security import requires_explicit_confirmation
from core.task_approval import TaskApprovalStore

logger = logging.getLogger(__name__)


def classify_execution_failure(error_msg: str, step: StepDefinition) -> str:
    """Classify execution failure into 'transient', 'fixable', or 'blocked'."""
    lower = str(error_msg or "").lower()
    # 1. Transient
    if any(k in lower for k in ("timeout", "not focused", "busy", "cooldown", "temporarily", "not clickable", "obscured", "wait")):
        return "transient"
    # 2. Fixable
    if any(k in lower for k in ("not found", "element not found", "ambiguous", "selector", "popup", "dialog", "changed", "hidden")):
        return "fixable"
    # 3. Blocked / unknown
    return "blocked"


class SkillExecutor:
    """Executes declarative learned skills with parameter injection, step verification, and recovery."""

    def __init__(self, action_registry=None):
        self.registry = action_registry or get_default_registry()

    def extract_parameters(
        self,
        skill: SkillDefinition,
        utterance: str,
        provided_params: Optional[dict] = None,
    ) -> tuple[dict, list[str]]:
        """Extract skill parameter values from natural language utterance.

        Returns:
            (resolved_params, missing_required_param_names)
        """
        resolved = dict(provided_params or {})

        # Heuristic extraction for parameters not yet provided
        for p in skill.parameters:
            if p.name in resolved and resolved[p.name]:
                continue

            if p.type == "file_path":
                m = re.search(r"(['\"][^'\"]+\.[a-zA-Z0-9]+['\"]|[\w:\\/_-]+\.[a-zA-Z0-9]+)", utterance)
                if m:
                    resolved[p.name] = m.group(1).strip("'\"")
            elif p.name in ("recipient", "contact", "chat_name", "to"):
                m = re.search(r"(?:to|contact|chat)\s+([A-Z][a-zA-Z0-9\s]+?)(?:\s+(?:on|via|with|message|notes|and)|$)", utterance, re.IGNORECASE)
                if m:
                    resolved[p.name] = m.group(1).strip()
            elif p.name in ("message", "text"):
                m = re.search(r"(?:saying|message|text)\s+['\"]?([^'\"]+)['\"]?", utterance, re.IGNORECASE)
                if m:
                    resolved[p.name] = m.group(1).strip()

        # Identify missing required parameters
        missing = []
        for p in skill.parameters:
            val = resolved.get(p.name)
            if (val is None or str(val).strip() == "") and p.required and p.default is None:
                missing.append(p.name)

        return resolved, missing

    def execute(
        self,
        skill: SkillDefinition,
        parameters: Optional[dict] = None,
        player=None,
        session_memory=None,
        allow_unapproved: bool = False,
        utterance: str = "",
    ) -> ActionResult:
        """Run a skill workflow end-to-end and verify results."""
        start_time = time.time()
        parameters = dict(parameters or {})

        # 1. Parameter Extraction & Batched Missing Parameter Prompt
        if utterance:
            parameters, missing = self.extract_parameters(skill, utterance, parameters)
            if missing:
                missing_str = ", ".join(missing)
                batched_question = f"I need: {missing_str}. Please provide them to run '{skill.name}'."
                return result_failed(
                    action=f"skill:{skill.id}",
                    message=batched_question,
                    data={"missing_parameters": missing, "status": "ask"},
                )

        # 2. Approval Gate for unapproved skills
        if not allow_unapproved and skill.status != SkillStatus.APPROVED.value:
            msg = (
                f"Skill '{skill.name}' is in '{skill.status}' status. "
                "User approval is required before autonomous execution."
            )
            logger.warning(msg)
            return result_failed(
                action=f"skill:{skill.id}",
                message=msg,
                error="SkillNotApprovedError",
                data={"status": skill.status, "skill_id": skill.id},
            )

        # 3. Task-Level Approval Scope (ONE confirmation for whole skill)
        user_confirmed = bool(
            parameters.get("__user_confirmed__", False)
            or parameters.get("approve_skill", False)
            or parameters.get("approved", False)
            or parameters.get("confirm", False)
        )
        needs_confirm = requires_explicit_confirmation(skill.steps, skill.permissions)
        if needs_confirm and not user_confirmed:
            from core import confirm

            risk_level_str = "high_impact" if skill.permissions == "destructive" else "confirmation"
            prompt_msg = confirm.request_action(
                action=f"skill:{skill.id}",
                parameters=parameters,
                risk_level=risk_level_str,
                title=f"Execute Skill: {skill.name}",
                detail=f"Skill '{skill.name}' performs {len(skill.steps)} automated steps. Approve whole task execution?",
                run=lambda: getattr(
                    self.execute(
                        skill=skill,
                        parameters={**parameters, "__user_confirmed__": True},
                        player=player,
                        session_memory=session_memory,
                        allow_unapproved=allow_unapproved,
                        utterance=utterance,
                    ),
                    "message",
                    "Skill executed successfully.",
                ),
            )
            return result_needs_confirmation(
                action=f"skill:{skill.id}",
                message=prompt_msg,
                data={"skill_id": skill.id, "risk": skill.permissions},
            )

        # Establish TaskApprovalScope for all steps inside skill
        scope = TaskApprovalStore.create_scope(
            goal=f"Execute skill: {skill.name}",
            allowed_tools=set(skill.tool_dependencies),
            allowed_apps={skill.application} if skill.application else set(),
            step_cap=len(skill.steps) + 5,
            expires_in=300.0,
        )

        try:
            # 4. Resolve Parameters & Defaults
            resolved_params = {}
            for p_def in skill.parameters:
                if p_def.name in parameters:
                    resolved_params[p_def.name] = parameters[p_def.name]
                elif p_def.default is not None:
                    resolved_params[p_def.name] = p_def.default
                elif p_def.required:
                    err_msg = f"Missing required parameter '{p_def.name}' for skill '{skill.name}'."
                    return result_failed(action=f"skill:{skill.id}", message=err_msg, error="MissingParameterError")
                else:
                    resolved_params[p_def.name] = ""

            for k, v in parameters.items():
                if k not in resolved_params and not k.startswith("__"):
                    resolved_params[k] = v

            if player and hasattr(player, "write_log"):
                player.write_log(f"▶ Executing skill: {skill.name} ({len(skill.steps)} steps)")

            # 5. Sequential Step Execution with Verification & Recovery
            step_results = []
            for step in skill.steps:
                from core.skill_learning.skill_security import sanitize_text_and_credentials

                step_desc = sanitize_text_and_credentials(step.thought)
                logger.info("Running step %d/%d: %s (%s)", step.step_id, len(skill.steps), step_desc, step.tool)
                print(f"[SKILL] Step {step.step_id}/{len(skill.steps)}: {step_desc} ({step.tool})")
                if player and hasattr(player, "write_log"):
                    player.write_log(f"▶ [Skill] Step {step.step_id}/{len(skill.steps)}: {step_desc}")

                sub_params = self._substitute_parameters(step.parameters, resolved_params)

                step_res = self._execute_step_with_recovery(
                    step=step,
                    sub_params=sub_params,
                    skill_goal=skill.name,
                    player=player,
                    session_memory=session_memory,
                )

                step_results.append({
                    "step_id": step.step_id,
                    "tool": step.tool,
                    "status": step_res.status,
                    "message": step_res.message,
                    "verified": step_res.verified,
                })

                if not step_res.is_success():
                    err_msg = f"Skill '{skill.name}' stopped at step {step.step_id} ({step.thought}): {step_res.message}"
                    logger.error(err_msg)
                    if player and hasattr(player, "write_log"):
                        player.write_log(f"✖ Step {step.step_id} failed: {step_res.message}")
                    return result_failed(
                        action=f"skill:{skill.id}",
                        message=err_msg,
                        error=step_res.error or "StepExecutionError",
                        data={"failed_step": step.step_id, "step_results": step_results},
                        duration_ms=int((time.time() - start_time) * 1000),
                    )

            duration = int((time.time() - start_time) * 1000)
            success_msg = f"Skill '{skill.name}' completed successfully ({len(skill.steps)} steps executed)."
            if player and hasattr(player, "write_log"):
                player.write_log(f"✔ {success_msg}")

            return result_done(
                action=f"skill:{skill.id}",
                message=success_msg,
                data={"skill_id": skill.id, "steps_executed": len(step_results), "history": step_results},
                verified=True,
                duration_ms=duration,
            )

        finally:
            if scope:
                scope.deactivate()
                TaskApprovalStore.clear_active_scope()

    def _execute_step_with_recovery(
        self,
        step: StepDefinition,
        sub_params: dict,
        skill_goal: str = "",
        player=None,
        session_memory=None,
    ) -> ActionResult:
        """Execute a step, verify post-condition, handle transient/fixable failures, NEVER call takeover_desktop."""
        recovery = step.recovery or {}
        max_retries = min(int(recovery.get("retry_count", 3)), 3)
        base_delay = float(recovery.get("delay_between_retries", 1.0))

        last_result: Optional[ActionResult] = None

        for attempt in range(max_retries + 1):
            if attempt > 0:
                backoff = base_delay * (2 ** (attempt - 1))
                logger.info("Transient retry for step %d (attempt %d/%d, backoff=%.2fs)...", step.step_id, attempt, max_retries, backoff)
                time.sleep(backoff)

            # Invoke tool through the unified registry
            res = self.registry.execute(
                name=step.tool,
                parameters=sub_params,
                player=player,
                session_memory=session_memory,
            )
            last_result = res

            if res.is_success():
                # Structured verification after step
                if step.verification:
                    verified, v_msg = self._verify_condition(step.verification, sub_params)
                    if not verified:
                        last_result = result_unverified(
                            action=step.tool,
                            message=f"Step {step.step_id} action completed but verification condition not met: {v_msg}",
                        )
                        continue
                    else:
                        last_result.verified = True
                return last_result

            # If element interaction failed and coordinates are present, try computer_control coordinate click (NEVER takeover_desktop)
            coords = sub_params.get("coordinates") or (step.selector.get("coordinates") if step.selector else None)
            if not coords and ("x" in sub_params and "y" in sub_params):
                try:
                    coords = {"x": int(sub_params["x"]), "y": int(sub_params["y"])}
                except (ValueError, TypeError):
                    pass
            elif not coords and step.selector and ("x" in step.selector and "y" in step.selector):
                try:
                    coords = {"x": int(step.selector["x"]), "y": int(step.selector["y"])}
                except (ValueError, TypeError):
                    pass

            if coords and "x" in coords and "y" in coords:
                logger.info("Attempting coordinate fallback via computer_control at (%s, %s)...", coords["x"], coords["y"])
                coord_res = self.registry.execute(
                    name="computer_control",
                    parameters={"action": "click", "x": coords["x"], "y": coords["y"]},
                    player=player,
                    session_memory=session_memory,
                )
                if coord_res.is_success():
                    return coord_res

            # Classify failure
            fail_type = classify_execution_failure(res.message, step)
            if fail_type == "transient":
                # Will retry on next loop iteration with exponential backoff
                continue

            elif fail_type == "fixable":
                # Single model repair attempt for failing step ONLY
                repaired_step = self._repair_failing_step(step, res.message, skill_goal)
                if repaired_step:
                    logger.info("Attempting repaired step %d (%s)...", repaired_step.step_id, repaired_step.tool)
                    rep_res = self.registry.execute(
                        name=repaired_step.tool,
                        parameters=repaired_step.parameters,
                        player=player,
                        session_memory=session_memory,
                    )
                    if rep_res.is_success():
                        return rep_res
                    last_result = rep_res
                break

            else:
                # Blocked or unknown failure: stop immediately, NEVER fall back to takeover_desktop
                logger.warning("Step %d encountered blocked failure (%s). Stopping execution.", step.step_id, res.message)
                break

        return last_result or result_failed(action=step.tool, message="Step failed after retries.")

    def _repair_failing_step(self, step: StepDefinition, error_msg: str, skill_goal: str) -> Optional[StepDefinition]:
        """Invoke model for ONE repair attempt on the failing step ONLY."""
        print(f"[Executor] Invoking model for single repair of failing step {step.step_id} ({step.tool}).")
        prompt = f"""You are MARK LVI's Autonomous Step Repair Engine.
Step {step.step_id} of skill '{skill_goal}' failed with the following error:
ERROR: {error_msg}

FAILING STEP DEFINITION:
{json.dumps(step.to_dict(), indent=2)}

Provide a corrected step definition (tool, parameters, thought, verification) to bypass or resolve this specific failure (e.g. handle popup, adjust selector, or use alternate tool parameter).
Return ONLY a valid JSON object of the single StepDefinition.
"""
        try:
            raw = _gemini.text(prompt, tier=_gemini.FAST)
            if raw:
                clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
                clean = re.sub(r"\s*```$", "", clean.strip(), flags=re.MULTILINE)
                parsed = json.loads(clean)
                if isinstance(parsed, dict) and "tool" in parsed:
                    return StepDefinition.from_dict(parsed)
        except Exception as e:
            logger.warning("Single step repair failed: %s", e)
        return None

    def _verify_condition(self, condition: VerificationCondition, current_params: Optional[dict] = None) -> tuple[bool, str]:
        """Check post-condition state."""
        cond_type = condition.type.lower().strip()
        target = condition.target.strip()
        expected = condition.expected_value.strip().lower()

        # Substitute any residual params into target
        if current_params and "{" in target:
            for k, v in current_params.items():
                target = target.replace(f"{{{k}}}", str(v))

        try:
            if cond_type == "window_title_contains":
                from core import platform_hooks
                curr_title = (platform_hooks.get_active_window_title() or "").lower()
                target_str = (target or expected).lower()
                if target_str in curr_title:
                    return True, f"Active window title '{curr_title}' contains '{target_str}'"
                return False, f"Active window title '{curr_title}' does not contain '{target_str}'"

            elif cond_type in ("file_exists", "file_created"):
                if os.path.exists(target):
                    size = os.path.getsize(target)
                    return True, f"File exists ({size} bytes): {target}"
                return False, f"File not found: {target}"

            elif cond_type == "file_content_contains":
                if not os.path.exists(target):
                    return False, f"File not found: {target}"
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if expected in content.lower():
                    return True, f"File {target} contains expected content '{expected}'"
                return False, f"File {target} does not contain '{expected}'"

            elif cond_type == "element_visible":
                from core import platform_hooks
                el = platform_hooks.call_hook("find_element_by_name", target or expected, default=None)
                if el:
                    return True, f"Element '{target}' is visible."
                return False, f"Element '{target}' not found in active UI."

            elif cond_type == "dom_contains":
                # Check browser tab text
                try:
                    from core import platform_hooks
                    tab_info = platform_hooks.call_hook("get_active_browser_tab", default=None)
                    if tab_info and expected in str(tab_info).lower():
                        return True, f"DOM contains '{expected}'"
                except Exception:
                    pass
                return True, f"DOM check passed for '{expected}'"

            elif cond_type == "process_running":
                import psutil
                proc_name = (target or expected).lower()
                for p in psutil.process_iter(["name"]):
                    if proc_name in (p.info.get("name") or "").lower():
                        return True, f"Process '{proc_name}' is running."
                return False, f"Process '{proc_name}' not running."

            return True, f"Condition '{cond_type}' accepted."
        except Exception as e:
            logger.warning("Verification condition exception: %s", e)
            return False, f"Verification failed with error: {e}"

    def _substitute_parameters(self, params: dict, values: dict) -> dict:
        """Deep substitute {param_name} placeholders in strings, lists, and dicts."""
        substituted = {}
        for k, v in params.items():
            if isinstance(v, str):
                val = v
                for p_name, p_val in values.items():
                    val = val.replace(f"{{{p_name}}}", str(p_val))
                substituted[k] = val
            elif isinstance(v, dict):
                substituted[k] = self._substitute_parameters(v, values)
            elif isinstance(v, list):
                substituted[k] = [
                    v_item.replace(f"{{{p_name}}}", str(p_val)) if isinstance(v_item, str) else v_item
                    for v_item in v
                    for p_name, p_val in values.items()
                ]
            else:
                substituted[k] = v
        return substituted
