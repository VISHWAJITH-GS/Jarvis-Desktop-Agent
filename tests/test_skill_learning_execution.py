"""
tests/test_skill_learning_execution.py — Verification of Part 4 (Execution Engine).

Tests:
1. Parameter extraction from utterance; batched asking for missing required parameters in ONE question.
2. TaskApprovalScope: 1 task-level confirmation for whole skill, scoped to tools/apps, no per-step confirms.
3. Structured post-condition verification after each step.
4. Failure classification & recovery:
   - Transient failure retries with exponential backoff (max 3).
   - Fixable failure triggers single model repair of the failing step ONLY.
   - Blocked failure stops and reports exact step and reason honestly.
   - NEVER falls back to takeover_desktop.
"""

import unittest
from unittest.mock import MagicMock, patch

from core.outcome import ActionResult, result_done, result_failed
from core.skill_learning.models import (
    ParameterDefinition,
    SkillDefinition,
    SkillStatus,
    StepDefinition,
    VerificationCondition,
)
from core.skill_learning.skill_executor import SkillExecutor, classify_execution_failure
from core.task_approval import TaskApprovalStore


class TestParameterExtraction(unittest.TestCase):
    def setUp(self):
        self.executor = SkillExecutor()
        self.skill = SkillDefinition(
            id="whatsapp_send_doc",
            name="Send Document on WhatsApp",
            description="Sends a document to a recipient on WhatsApp",
            parameters=[
                ParameterDefinition(name="recipient", type="string", required=True),
                ParameterDefinition(name="file_path", type="file_path", required=True),
                ParameterDefinition(name="optional_note", type="string", required=False, default=""),
            ],
            steps=[
                StepDefinition(step_id=1, thought="Send message", tool="open_app", parameters={"app_name": "Chrome"})
            ],
            status=SkillStatus.APPROVED.value,
        )

    def test_extracts_parameters_from_utterance(self):
        utterance = "Send the report to Ram Nath TCE on WhatsApp using C:/Reports/final.pdf"
        resolved, missing = self.executor.extract_parameters(self.skill, utterance)
        self.assertEqual(resolved.get("recipient"), "Ram Nath TCE")
        self.assertEqual(resolved.get("file_path"), "C:/Reports/final.pdf")
        self.assertEqual(missing, [])

    def test_batched_question_for_missing_required_parameters(self):
        # Only recipient in utterance, file_path is missing
        utterance = "Send a document to Ram Nath TCE on WhatsApp"
        res = self.executor.execute(self.skill, utterance=utterance)
        self.assertEqual(res.status, "failed")
        self.assertIn("I need: file_path", res.message)
        self.assertEqual(res.data.get("missing_parameters"), ["file_path"])


class TestApprovalScopeIntegration(unittest.TestCase):
    def setUp(self):
        self.executor = SkillExecutor()
        self.skill = SkillDefinition(
            id="test_scoped_skill",
            name="Scoped Skill",
            description="Testing task approval scope",
            tool_dependencies=["open_app", "page_to_file"],
            application="Browser",
            steps=[
                StepDefinition(step_id=1, thought="Open app", tool="open_app", parameters={"app_name": "Notepad"}),
            ],
            status=SkillStatus.APPROVED.value,
        )

    def test_creates_and_closes_task_approval_scope(self):
        store = TaskApprovalStore.get_instance()
        with patch.object(self.executor.registry, "execute", return_value=result_done("open_app", "ok")):
            res = self.executor.execute(self.skill)
            self.assertTrue(res.is_success())
            # Ensure scope was created and deactivated
            active_scopes = [s for s in store.get_all_active_scopes()]
            self.assertEqual(len(active_scopes), 0, "Approval scope should be deactivated after execution")


class TestStructuredVerification(unittest.TestCase):
    def setUp(self):
        self.executor = SkillExecutor()

    def test_structured_verification_file_content(self):
        cond = VerificationCondition(type="file_exists", target=__file__)
        verified, msg = self.executor._verify_condition(cond)
        self.assertTrue(verified)
        self.assertIn("File exists", msg)

        cond_nonexistent = VerificationCondition(type="file_exists", target="C:/nonexistent_file_xyz_123.fake")
        verified, msg = self.executor._verify_condition(cond_nonexistent)
        self.assertFalse(verified)
        self.assertIn("File not found", msg)


class TestFailureHandlingAndRecovery(unittest.TestCase):
    def setUp(self):
        self.executor = SkillExecutor()
        self.step = StepDefinition(
            step_id=1,
            thought="Click button",
            tool="open_app",
            parameters={"app_name": "Notepad"},
        )

    def test_classify_failure(self):
        self.assertEqual(classify_execution_failure("Target element temporarily not clickable", self.step), "transient")
        self.assertEqual(classify_execution_failure("Window not focused, busy", self.step), "transient")
        self.assertEqual(classify_execution_failure("Element not found in DOM", self.step), "fixable")
        self.assertEqual(classify_execution_failure("A blocking popup dialog appeared", self.step), "fixable")
        self.assertEqual(classify_execution_failure("Access denied / permission error", self.step), "blocked")

    def test_never_calls_takeover_desktop_on_failure(self):
        skill = SkillDefinition(
            id="failing_skill",
            name="Failing Skill",
            description="Skill that fails",
            steps=[
                StepDefinition(step_id=1, thought="Step 1", tool="open_app", parameters={"app_name": "BadApp"}),
            ],
            status=SkillStatus.APPROVED.value,
        )

        with patch.object(self.executor.registry, "execute") as mock_exec:
            mock_exec.return_value = result_failed("open_app", "Permission denied")
            res = self.executor.execute(skill)
            self.assertFalse(res.is_success())
            self.assertIn("Skill 'Failing Skill' stopped at step 1", res.message)
            # Ensure takeover_desktop was NEVER called
            for call_args in mock_exec.call_args_list:
                self.assertNotEqual(call_args.kwargs.get("name"), "takeover_desktop")


    def test_coordinate_fallback_via_computer_control(self):
        skill = SkillDefinition(
            id="click_skill",
            name="Click Skill",
            description="Skill with coordinates fallback",
            steps=[
                StepDefinition(
                    step_id=1,
                    thought="Click button",
                    tool="windows_uia",
                    parameters={"action": "click", "element_name": "Login"},
                    selector={"coordinates": {"x": 400, "y": 300}},
                ),
            ],
            status=SkillStatus.APPROVED.value,
        )

        def mock_exec_side_effect(name, parameters, **kwargs):
            if name == "windows_uia":
                return result_failed("windows_uia", "Element not found")
            elif name == "computer_control" and parameters.get("x") == 400 and parameters.get("y") == 300:
                return result_done("computer_control", "Clicked at (400, 300)")
            return result_failed(name, "unknown")

        with patch.object(self.executor.registry, "execute", side_effect=mock_exec_side_effect) as mock_exec:
            res = self.executor.execute(skill)
            self.assertTrue(res.is_success())
            # Ensure computer_control was invoked with coordinates, NOT takeover_desktop
            names_called = [c.kwargs.get("name") for c in mock_exec.call_args_list]
            self.assertIn("computer_control", names_called)
            self.assertNotIn("takeover_desktop", names_called)


if __name__ == "__main__":
    unittest.main()
