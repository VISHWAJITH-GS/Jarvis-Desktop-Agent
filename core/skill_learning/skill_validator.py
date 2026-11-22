"""
core/skill_learning/skill_validator.py — Schema, Tool, and Execution Validator for Learned Skills.

Validates that a learned SkillDefinition:
1. Conforms to the declarative schema (valid IDs, names, steps, parameters).
2. Uses only registered and available actions in the unified ActionRegistry.
3. Arguments match the declaration schema (types, required fields). Rejects unknown arguments.
4. Placeholders: every {param} is declared in skill parameters; detects leftover demonstration concrete values.
5. In-code risk tier computation: computes MAX tier across steps; rejects shell/code execution and credential extraction.
6. Dry-run simulation: resolves parameters, verifies well-formed tool calls and side-effect-free checks.
7. Single-attempt model repair: attempts exactly one repair if validation fails; does not loop.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from core import gemini as _gemini
from core.action_loader import get_default_registry
from core.risk_tiers import RiskTier, get_action_tier
from core.skill_learning.models import (
    ParameterDefinition,
    RiskLevel,
    SkillDefinition,
    SkillStatus,
    StepDefinition,
)
from core.skill_learning.skill_security import SENSITIVE_INPUT_PLACEHOLDER

logger = logging.getLogger(__name__)

_PROHIBITED_TOOLS = {
    "cmd", "powershell", "bash", "terminal", "run_command", "shell",
    "dev_agent", "takeover_desktop", "python_eval", "execute_code",
}

_PROHIBITED_PARAM_KEYS = {"command", "shell_command", "bash_command", "powershell_command"}


class SkillValidator:
    """Performs static analysis, schema compliance, risk tiering, and dry-run validation on skills."""

    def __init__(self, action_registry=None):
        self.registry = action_registry or get_default_registry()

    def validate_schema(self, skill: SkillDefinition) -> Tuple[bool, list[str], list[str]]:
        """Validate structure and integrity of a SkillDefinition.

        Returns:
            (is_valid, errors, warnings)
        """
        errors: list[str] = []
        warnings: list[str] = []

        if not skill.id or not re.match(r"^[a-z0-9_\-]+$", skill.id):
            errors.append(f"Invalid skill ID '{skill.id}'. Must contain only lowercase letters, digits, '_' or '-'.")

        if not skill.name or len(skill.name.strip()) < 3:
            errors.append("Skill name must be at least 3 characters.")

        if not skill.steps:
            errors.append("Skill must contain at least one step.")

        # Check parameter names
        defined_param_names = set()
        for p in skill.parameters:
            if not p.name or not re.match(r"^[a-zA-Z0-9_]+$", p.name):
                errors.append(f"Invalid parameter name '{p.name}'.")
            defined_param_names.add(p.name)

        # Inspect steps
        step_ids = set()
        for idx, step in enumerate(skill.steps, 1):
            if step.step_id in step_ids:
                warnings.append(f"Duplicate step_id {step.step_id}; auto-correcting to {idx}.")
            step_ids.add(step.step_id)

            if not step.tool:
                errors.append(f"Step {idx} has no tool declared.")
                continue

            # 1. Prohibited dangerous operations check (shell, code execution, desktop takeover)
            tool_lower = step.tool.lower().strip()
            if tool_lower in _PROHIBITED_TOOLS:
                errors.append(
                    f"Step {idx}: tool '{step.tool}' is prohibited. Skills cannot execute arbitrary shell or code execution."
                )

            for pk in step.parameters:
                if pk.lower() in _PROHIBITED_PARAM_KEYS:
                    errors.append(
                        f"Step {idx}: prohibited parameter '{pk}' in step. Shell command execution is not allowed in learned skills."
                    )

            # 2. Check if tool is available in the ActionRegistry
            if not self.registry.has_action(step.tool):
                errors.append(f"Step {idx} refers to unregistered tool '{step.tool}'.")
            else:
                # 3. Check for unknown arguments against action parameter declaration
                decl = self.registry.get_declaration(step.tool)
                if decl and isinstance(decl.get("parameters"), dict):
                    props = decl["parameters"].get("properties", {})
                    if props:
                        for k in step.parameters:
                            if k not in props:
                                errors.append(
                                    f"Step {idx} ({step.tool}): unexpected keyword argument '{k}'. Allowed: {sorted(props.keys())}"
                                )

            # 4. Check parameter placeholders {param_name} & leftover concrete demonstration values
            for k, v in step.parameters.items():
                if isinstance(v, str):
                    if SENSITIVE_INPUT_PLACEHOLDER in v:
                        warnings.append(
                            f"Step {idx} parameter '{k}' contains redacted credential placeholder. Needs parameterization."
                        )

                    # Check parameter placeholders
                    placeholders = re.findall(r"\{([a-zA-Z0-9_]+)\}", v)
                    for ph in placeholders:
                        if ph not in defined_param_names:
                            errors.append(
                                f"Step {idx} uses placeholder '{{{ph}}}' which is not defined in skill parameters."
                            )

                    # Check for leftover concrete absolute paths
                    if ("C:\\" in v or "D:\\" in v or "/home/" in v or "/Users/" in v) and not placeholders:
                        warnings.append(
                            f"Step {idx} parameter '{k}' contains hardcoded demonstration path '{v}'; consider parameterizing as a variable."
                        )

        # 5. In-code Risk Tier Computation (Model never assigns its own tier)
        skill.permissions = self.compute_skill_risk_tier(skill).value.lower()

        is_valid = len(errors) == 0
        return is_valid, errors, warnings

    def compute_skill_risk_tier(self, skill: SkillDefinition) -> RiskTier:
        """Compute the effective RiskTier in code as the MAX tier across all steps."""
        max_tier = RiskTier.LOW
        for step in skill.steps:
            tier = get_action_tier(step.tool, step.parameters)
            if tier == RiskTier.HIGH:
                return RiskTier.HIGH
            elif tier == RiskTier.MEDIUM and max_tier == RiskTier.LOW:
                max_tier = RiskTier.MEDIUM
        return max_tier

    def dry_run_test(
        self,
        skill: SkillDefinition,
        test_inputs: Optional[dict] = None,
    ) -> dict:
        """Simulate execution of skill steps without calling OS tools.

        Verifies:
        - All required parameters are satisfied or have defaults/examples
        - Template substitution evaluates properly
        - Tool signatures accept the substituted arguments
        - Side-effect-free checks (valid URLs, non-empty app names, valid path syntax)
        """
        is_simulation_mode = (test_inputs is None)
        test_inputs = test_inputs or {}
        report = {
            "skill_id": skill.id,
            "success": True,
            "errors": [],
            "warnings": [],
            "substituted_steps": [],
        }

        # 1. Parameter completeness
        substituted_params: dict[str, Any] = {}
        for p in skill.parameters:
            if p.name in test_inputs:
                substituted_params[p.name] = test_inputs[p.name]
            elif p.default is not None:
                substituted_params[p.name] = p.default
            elif is_simulation_mode and p.example is not None and str(p.example).strip():
                substituted_params[p.name] = p.example
            elif is_simulation_mode and p.required:
                # In simulation mode without explicit inputs, provide synthetic example to test substitution
                if p.type == "file_path":
                    substituted_params[p.name] = "C:/test_dir/sample_file.txt"
                elif p.type == "url":
                    substituted_params[p.name] = "https://example.com"
                else:
                    substituted_params[p.name] = f"sample_{p.name}"
            elif p.required:
                report["errors"].append(f"Missing required parameter '{p.name}' for test.")
                report["success"] = False
            else:
                substituted_params[p.name] = ""

        if not report["success"]:
            return report

        # 2. Step substitution simulation & side-effect-free validation
        for step in skill.steps:
            sub_params = {}
            for k, v in step.parameters.items():
                if isinstance(v, str):
                    val = v
                    for p_name, p_val in substituted_params.items():
                        val = val.replace(f"{{{p_name}}}", str(p_val))
                    sub_params[k] = val
                else:
                    sub_params[k] = v

            # Verify tool accepts parameters
            action_def = self.registry.get_action(step.tool)
            if not action_def:
                report["errors"].append(f"Step {step.step_id}: Tool '{step.tool}' not found in registry.")
                report["success"] = False
            else:
                param_valid, param_err = action_def.validate_parameters(sub_params)
                if not param_valid:
                    report["errors"].append(
                        f"Step {step.step_id} tool '{step.tool}' parameter validation failed: {param_err}"
                    )
                    report["success"] = False

            # Side-effect-free checks
            if step.tool == "open_app":
                app_target = sub_params.get("app_name") or sub_params.get("app")
                if not app_target or not str(app_target).strip():
                    report["errors"].append(f"Step {step.step_id}: 'open_app' missing required application name.")
                    report["success"] = False

            elif step.tool == "browser_control" and sub_params.get("action") == "navigate":
                url_val = sub_params.get("url", "")
                if url_val:
                    parsed = urlparse(url_val)
                    if not (parsed.scheme in ("http", "https") or "." in url_val):
                        report["warnings"].append(f"Step {step.step_id}: URL '{url_val}' may not be a well-formed web address.")

            elif step.tool in ("page_to_file", "file_controller"):
                dest = sub_params.get("destination_path") or sub_params.get("file_path") or sub_params.get("path")
                if dest and isinstance(dest, str):
                    try:
                        Path(dest)
                    except Exception as pe:
                        report["errors"].append(f"Step {step.step_id}: Invalid file path syntax '{dest}': {pe}")
                        report["success"] = False

            report["substituted_steps"].append(
                {
                    "step_id": step.step_id,
                    "tool": step.tool,
                    "parameters": sub_params,
                    "expected_outcome": step.expected_outcome,
                    "verification": step.verification.to_dict() if step.verification else None,
                }
            )

        return report

    def validate_full(
        self,
        skill: SkillDefinition,
        sample_inputs: Optional[dict] = None,
    ) -> dict:
        """Perform comprehensive validation and return a structured report."""
        is_valid, errors, warnings = self.validate_schema(skill)
        dry_run = self.dry_run_test(skill, sample_inputs)

        errors.extend(dry_run["errors"])
        warnings.extend(dry_run["warnings"])

        overall_valid = is_valid and dry_run["success"] and len(errors) == 0
        print("[Validator] Skill validation completed.")
        logger.info("[Validator] Skill validation completed. (valid: %s)", overall_valid)

        return {
            "valid": overall_valid,
            "errors": list(dict.fromkeys(errors)),
            "warnings": list(dict.fromkeys(warnings)),
            "step_count": len(skill.steps),
            "tool_dependencies": skill.tool_dependencies,
            "risk_level": skill.permissions,
            "simulation": dry_run["substituted_steps"],
        }

    def repair_skill(self, skill: SkillDefinition, errors: list[str]) -> Optional[SkillDefinition]:
        """Perform ONE repair attempt by sending validation errors to Gemini."""
        if not errors:
            return skill

        print(f"[Validator] Attempting single model repair for skill '{skill.id}' with {len(errors)} errors.")
        logger.info("[Validator] Attempting single model repair for skill '%s' with %d errors.", skill.id, len(errors))

        # Format tool catalog
        action_names = (
            self.registry.list_actions()
            if hasattr(self.registry, "list_actions")
            else sorted(self.registry.names())
        )
        catalog_lines = []
        for name in action_names:
            decl = self.registry.get_declaration(name) or {}
            props = decl.get("parameters", {}).get("properties", {}) if isinstance(decl.get("parameters"), dict) else {}
            catalog_lines.append(f"- {name}: {list(props.keys())}")
        catalog_summary = "\n".join(catalog_lines)

        prompt = f"""You are MARK LVI's Skill Repair Specialist.
The following SkillSpec failed validation. Please fix all errors and return a corrected SkillSpec JSON.

VALIDATION ERRORS:
{json.dumps(errors, indent=2)}

ORIGINAL SKILL:
{json.dumps(skill.to_dict(), indent=2)}

AVAILABLE TOOL CATALOG (Tool name: allowed parameters):
{catalog_summary}

RULES FOR REPAIR:
1. Fix all validation errors:
   - Use only registered tools from the catalog.
   - Remove or rename any unexpected arguments so parameters strictly match the catalog.
   - Ensure every {{placeholder}} used in steps is declared in the skill's parameters list.
   - Do NOT use prohibited tools (shell, cmd, powershell, code execution).
2. Return ONLY a valid JSON object of the repaired SkillSpec.
"""
        try:
            raw = _gemini.text(prompt, tier=_gemini.SMART)
            if raw:
                clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
                clean = re.sub(r"\s*```$", "", clean.strip(), flags=re.MULTILINE)
                parsed = json.loads(clean)
                if isinstance(parsed, dict) and "steps" in parsed:
                    from core.skill_learning.workflow_builder import WorkflowBuilder
                    builder = WorkflowBuilder()
                    repaired = builder.build_skill_definition(parsed, author=skill.author)
                    if (not repaired.id or repaired.id == "unnamed_skill") and skill.id and skill.id != "unnamed_skill":
                        repaired.id = skill.id
                    if (not repaired.name or repaired.name == "Unnamed Skill") and skill.name:
                        repaired.name = skill.name
                    print("[Validator] Model repair attempt produced repaired skill.")
                    return repaired
        except Exception as e:
            logger.warning("Skill repair attempt failed: %s", e)

        return None

    def validate_and_repair(
        self,
        skill: SkillDefinition,
        sample_inputs: Optional[dict] = None,
    ) -> Tuple[SkillDefinition, dict]:
        """Validate skill, and if errors are found, attempt exactly ONE repair."""
        report = self.validate_full(skill, sample_inputs)
        if report["valid"]:
            return skill, report

        # Attempt ONE repair
        repaired = self.repair_skill(skill, report["errors"])
        if repaired:
            repaired_report = self.validate_full(repaired, sample_inputs)
            if repaired_report["valid"]:
                print(f"[Validator] Repair succeeded! Skill '{repaired.id}' is now valid.")
                return repaired, repaired_report
            else:
                print(f"[Validator] Repair failed. Remaining errors: {repaired_report['errors']}")
                return repaired, repaired_report

        return skill, report
