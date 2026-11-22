"""
tests/test_skill_learning_part0.py — Tests for Skill Learning Part 0:
- ActionRegistry public API (has_action, get_declaration, get_parameters_schema, list_actions, get_action)
- SkillValidator schema validation without attribute errors
- Single shared discovery & discovery idempotence (screen_process and verify_outcome both active)
- Removal of approve_skill from model schema & ignoring model-passed approval arguments
- Risk tiering for skill_learner actions
"""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from actions.skill_learner import TOOL, skill_learner
from core.action_loader import ActionRegistry, discover_actions, get_default_registry
from core.risk_tiers import RiskTier, get_action_tier
from core.skill_learning.models import ParameterDefinition, SkillDefinition, StepDefinition
from core.skill_learning.skill_validator import SkillValidator


class TestActionRegistryPublicAPI(unittest.TestCase):
    def setUp(self):
        self.registry = get_default_registry()

    def test_has_action_and_alias(self):
        self.assertTrue(self.registry.has_action("open_app"))
        self.assertTrue(self.registry.has("open_app"))
        self.assertFalse(self.registry.has_action("non_existent_tool_xyz_123"))
        self.assertFalse(self.registry.has("non_existent_tool_xyz_123"))

    def test_get_declaration(self):
        decl = self.registry.get_declaration("open_app")
        self.assertIsNotNone(decl)
        self.assertIsInstance(decl, dict)
        self.assertEqual(decl.get("name"), "open_app")
        self.assertIn("parameters", decl)
        self.assertIsNone(self.registry.get_declaration("non_existent_tool_xyz_123"))

    def test_get_parameters_schema(self):
        schema = self.registry.get_parameters_schema("open_app")
        self.assertIsNotNone(schema)
        self.assertIsInstance(schema, dict)
        self.assertIn("properties", schema)
        self.assertIsNone(self.registry.get_parameters_schema("non_existent_tool_xyz_123"))

    def test_list_actions(self):
        actions = self.registry.list_actions()
        self.assertIsInstance(actions, list)
        self.assertIn("open_app", actions)
        self.assertIn("screen_process", actions)
        self.assertIn("verify_outcome", actions)

    def test_get_action(self):
        record = self.registry.get_action("open_app")
        self.assertIsNotNone(record)
        self.assertEqual(record.name, "open_app")
        self.assertIsNone(self.registry.get_action("non_existent_tool_xyz_123"))


class TestSkillValidatorAPI(unittest.TestCase):
    def test_validate_schema_uses_has_action(self):
        validator = SkillValidator()
        skill = SkillDefinition(
            id="test_skill_api",
            name="Test Skill API",
            description="Testing validator schema API",
            application="Windows",
            parameters=[ParameterDefinition(name="param1", type="string", required=True)],
            steps=[
                StepDefinition(
                    step_id=1,
                    thought="Run open app",
                    tool="open_app",
                    parameters={"app_name": "notepad"},
                )
            ],
        )
        is_valid, errors, warnings = validator.validate_schema(skill)
        self.assertTrue(is_valid, f"Validation failed: {errors}")
        self.assertEqual(errors, [])


class TestDiscoveryIdempotence(unittest.TestCase):
    def test_discovery_is_idempotent_and_tools_available(self):
        actions_dir = Path(__file__).resolve().parent.parent / "actions"
        reg1 = discover_actions(actions_dir=actions_dir)
        reg2 = discover_actions(actions_dir=actions_dir)

        # Both runs should yield identical registered action sets
        self.assertEqual(reg1.names(), reg2.names())
        self.assertIn("screen_process", reg1.names())
        self.assertIn("verify_outcome", reg1.names())
        self.assertIn("screen_process", reg2.names())
        self.assertIn("verify_outcome", reg2.names())

        # Test both handlers are callable
        screen_proc = reg1.get_action("screen_process")
        self.assertIsNotNone(screen_proc)
        self.assertTrue(callable(screen_proc.handler))

        verify_out = reg1.get_action("verify_outcome")
        self.assertIsNotNone(verify_out)
        self.assertTrue(callable(verify_out.handler))


class TestModelApprovalIgnored(unittest.TestCase):
    def test_approve_skill_not_in_tool_schema(self):
        props = TOOL.get("parameters", {}).get("properties", {})
        self.assertNotIn("approve_skill", props)
        self.assertNotIn("approved", props)
        self.assertNotIn("confirm", props)

    def test_model_passed_approval_arguments_ignored(self):
        with patch("actions.skill_learner.get_default_learning_manager") as mock_mgr_getter:
            mock_mgr = MagicMock()
            mock_mgr_getter.return_value = mock_mgr

            # Model tries to pass approve_skill in parameters
            skill_learner({
                "action": "execute_skill",
                "skill_id": "test_skill",
                "approve_skill": True,
                "approved": True,
                "confirm": True,
            })

            self.assertTrue(mock_mgr.execute_skill.called)
            call_kwargs = mock_mgr.execute_skill.call_args[1]
            params = call_kwargs.get("parameters", {})
            self.assertFalse(params.get("approve_skill", False))
            self.assertFalse(params.get("__user_confirmed__", False))


class TestSkillLearnerRiskTiers(unittest.TestCase):
    def test_low_tier_actions(self):
        low_actions = [
            "start_learning", "start",
            "stop_learning", "stop",
            "pause_learning", "pause",
            "resume_learning", "resume",
            "cancel_learning", "cancel",
            "review_demonstration", "review",
            "list_skills", "list",
            "test_skill", "test",
            "show_skill", "view_skill",
        ]
        for act in low_actions:
            tier = get_action_tier("skill_learner", {"action": act})
            self.assertEqual(tier, RiskTier.LOW, f"Action {act} was expected to be LOW")

    def test_medium_tier_actions(self):
        medium_actions = [
            "save_skill", "save",
            "approve_skill", "approve",
            "generate_skill", "create_skill",
            "edit_skill", "rename_skill",
            "execute_skill", "run_skill",
        ]
        for act in medium_actions:
            tier = get_action_tier("skill_learner", {"action": act})
            self.assertEqual(tier, RiskTier.MEDIUM, f"Action {act} was expected to be MEDIUM")

    def test_high_actions(self):
        high_actions = ["delete_skill", "delete", "remove_skill"]
        for act in high_actions:
            tier = get_action_tier("skill_learner", {"action": act})
            self.assertEqual(tier, RiskTier.HIGH, f"Action {act} was expected to be HIGH")


if __name__ == "__main__":
    unittest.main()
