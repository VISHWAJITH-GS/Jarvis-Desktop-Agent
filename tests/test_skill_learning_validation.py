"""
tests/test_skill_learning_validation.py — Verification of Part 3 (Validation and Dry Run).

Tests:
1. Rejection of unknown arguments and unregistered tools against ActionRegistry.
2. Placeholder validation ({param} must be defined in skill parameters).
3. In-code risk tier computation (MAX tier across steps; rejection of shell/code execution).
4. Dry-run simulation with parameter substitution and side-effect-free checks.
5. Exactly one repair attempt for failing skills (no looping).
"""

import unittest
from unittest.mock import MagicMock, patch

from core.action_loader import get_default_registry
from core.risk_tiers import RiskTier
from core.skill_learning.models import (
    ParameterDefinition,
    SkillDefinition,
    StepDefinition,
    VerificationCondition,
)
from core.skill_learning.skill_validator import SkillValidator


class TestActionRegistryAndArgumentValidation(unittest.TestCase):
    def setUp(self):
        self.validator = SkillValidator()

    def test_unregistered_tool_rejected(self):
        skill = SkillDefinition(
            id="invalid_tool_skill",
            name="Invalid Tool Skill",
            description="Uses nonexistent tool",
            steps=[
                StepDefinition(
                    step_id=1,
                    thought="Run fake tool",
                    tool="completely_fake_nonexistent_tool_123",
                    parameters={},
                )
            ],
        )
        is_valid, errors, warnings = self.validator.validate_schema(skill)
        self.assertFalse(is_valid)
        self.assertTrue(any("unregistered tool" in e for e in errors))

    def test_unexpected_keyword_argument_rejected(self):
        # open_app takes "app_name", not "bogus_extra_arg"
        skill = SkillDefinition(
            id="bad_arg_skill",
            name="Bad Arg Skill",
            description="Passes unexpected argument to open_app",
            steps=[
                StepDefinition(
                    step_id=1,
                    thought="Open app with bad arg",
                    tool="open_app",
                    parameters={"app_name": "notepad", "bogus_extra_arg": 12345},
                )
            ],
        )
        is_valid, errors, warnings = self.validator.validate_schema(skill)
        self.assertFalse(is_valid)
        self.assertTrue(any("unexpected keyword argument 'bogus_extra_arg'" in e for e in errors))


class TestPlaceholderValidation(unittest.TestCase):
    def setUp(self):
        self.validator = SkillValidator()

    def test_undefined_placeholder_rejected(self):
        skill = SkillDefinition(
            id="undefined_placeholder_skill",
            name="Undefined Placeholder",
            description="Uses undefined placeholder in step",
            parameters=[ParameterDefinition(name="chat_name", type="string")],
            steps=[
                StepDefinition(
                    step_id=1,
                    thought="Send to chat with undefined message",
                    tool="open_app",
                    parameters={"app_name": "{undeclared_variable}"},
                )
            ],
        )
        is_valid, errors, warnings = self.validator.validate_schema(skill)
        self.assertFalse(is_valid)
        self.assertTrue(any("undeclared_variable" in e for e in errors))

    def test_defined_placeholder_accepted(self):
        skill = SkillDefinition(
            id="valid_placeholder_skill",
            name="Valid Placeholder",
            description="Uses defined placeholder in step",
            parameters=[ParameterDefinition(name="target_app", type="string", example="notepad")],
            steps=[
                StepDefinition(
                    step_id=1,
                    thought="Open defined app",
                    tool="open_app",
                    parameters={"app_name": "{target_app}"},
                )
            ],
        )
        is_valid, errors, warnings = self.validator.validate_schema(skill)
        self.assertTrue(is_valid, f"Expected valid, got errors: {errors}")


class TestInCodeRiskTierComputationAndProhibitedTools(unittest.TestCase):
    def setUp(self):
        self.validator = SkillValidator()

    def test_rejects_shell_execution_and_prohibited_tools(self):
        for prohibited in ("run_command", "cmd", "powershell", "dev_agent", "takeover_desktop"):
            skill = SkillDefinition(
                id=f"prohibited_{prohibited}",
                name="Prohibited Skill",
                description="Attempts prohibited tool",
                steps=[
                    StepDefinition(
                        step_id=1,
                        thought="Run prohibited tool",
                        tool=prohibited,
                        parameters={},
                    )
                ],
            )
            is_valid, errors, warnings = self.validator.validate_schema(skill)
            self.assertFalse(is_valid, f"Prohibited tool {prohibited} should have been rejected")
            self.assertTrue(any("prohibited" in e for e in errors))

    def test_in_code_tier_computation_max_across_steps(self):
        # Step 1: LOW (open_app)
        # Step 2: MEDIUM (send_message)
        skill = SkillDefinition(
            id="mixed_tier_skill",
            name="Mixed Tier Skill",
            description="Contains low and medium tier steps",
            steps=[
                StepDefinition(step_id=1, thought="Open app", tool="open_app", parameters={"app_name": "Chrome"}),
                StepDefinition(step_id=2, thought="Send message", tool="send_message", parameters={"receiver": "John", "message_text": "Hello"}),
            ],
        )
        tier = self.validator.compute_skill_risk_tier(skill)
        self.assertEqual(tier, RiskTier.MEDIUM)

        # Verify model never assigns tier; validator sets skill.permissions
        self.validator.validate_schema(skill)
        self.assertEqual(skill.permissions, "medium")


class TestDryRunSimulation(unittest.TestCase):
    def setUp(self):
        self.validator = SkillValidator()

    def test_dry_run_resolves_templates_and_validates_side_effect_free(self):
        skill = SkillDefinition(
            id="dry_run_skill",
            name="Dry Run Skill",
            description="Testing dry run simulation",
            parameters=[
                ParameterDefinition(name="out_path", type="file_path", example="C:/Users/test/chat.txt", required=True),
            ],
            steps=[
                StepDefinition(
                    step_id=1,
                    thought="Export chat to file",
                    tool="page_to_file",
                    parameters={"destination_path": "{out_path}", "format": "txt"},
                    expected_outcome="file exists at {out_path}",
                    verification=VerificationCondition(type="file_exists", target="{out_path}"),
                )
            ],
        )
        report = self.validator.dry_run_test(skill, test_inputs={"out_path": "C:/Users/test/chat.txt"})
        self.assertTrue(report["success"], f"Dry run failed: {report['errors']}")
        self.assertEqual(len(report["substituted_steps"]), 1)
        sub_step = report["substituted_steps"][0]
        self.assertEqual(sub_step["parameters"]["destination_path"], "C:/Users/test/chat.txt")


class TestSingleRepairAttempt(unittest.TestCase):
    def setUp(self):
        self.validator = SkillValidator()

    def test_repair_attempt_invoked_once_on_failure(self):
        broken_skill = SkillDefinition(
            id="broken_skill",
            name="Broken Skill",
            description="Broken parameter",
            steps=[
                StepDefinition(
                    step_id=1,
                    thought="Open app",
                    tool="open_app",
                    parameters={"unknown_param": "foo"},
                )
            ],
        )
        fixed_skill = SkillDefinition(
            id="broken_skill",
            name="Broken Skill",
            description="Fixed parameter",
            steps=[
                StepDefinition(
                    step_id=1,
                    thought="Open app",
                    tool="open_app",
                    parameters={"app_name": "notepad"},
                )
            ],
        )

        with patch.object(self.validator, "repair_skill", return_value=fixed_skill) as mock_repair:
            result_skill, report = self.validator.validate_and_repair(broken_skill)
            mock_repair.assert_called_once()
            self.assertTrue(report["valid"])
            self.assertEqual(result_skill.steps[0].parameters, {"app_name": "notepad"})


if __name__ == "__main__":
    unittest.main()
