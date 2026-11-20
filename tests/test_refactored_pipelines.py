"""
tests/test_refactored_pipelines.py — Comprehensive unit tests for refactored WhatsApp,
Plugin Permission Guarding, and Agent Orchestrator Recovery contracts.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core import outcome
from core.agent_orchestrator import AgentOrchestrator
from core.permissions import ActionExecutionGuard, ActionPermissionManager, ActionRiskLevel
from core.plugin_loader import PluginRecord, PluginRegistry
from plugins import _whatsapp_core as wa


class WhatsAppPipelineTestCase(unittest.TestCase):
    """Test WhatsApp verification honesty, contact resolution, and UIA guards."""

    def setUp(self):
        self.driver = wa.WhatsAppDriver()

    def test_resolve_contact_empty_candidates_returns_false(self):
        """When 0 contact candidates match, resolve_contact returns False and does NOT select."""
        with patch.object(self.driver, "open_whatsapp", return_value=True), \
             patch.object(self.driver, "_find_whatsapp_window", return_value=MagicMock()), \
             patch.object(wa, "pyautogui") as mock_pyautogui:
            with patch.object(self.driver, "uia") as mock_uia:
                # Search box found, but search result ListItems return empty
                mock_uia.find_elements.side_effect = [
                    {"status": "success", "matches": [MagicMock()]},  # search box
                    {"status": "success", "matches": []},              # candidates empty
                ]
                ok, msg, candidates = self.driver.resolve_contact("NonexistentContact")
                self.assertFalse(ok)
                self.assertEqual(len(candidates), 0)
                self.assertIn("not found", msg.lower())
                # Verify enter key was NEVER pressed (search bar clearing may use backspace/delete)
                self.assertNotIn(unittest.mock.call("enter"), mock_pyautogui.press.call_args_list)

    def test_send_message_fails_when_uia_verification_fails(self):
        """When UIA confirmation cannot verify the message sent, returns False (never verified=True)."""
        with patch.object(self.driver, "open_chat", return_value=(True, "Alex")), \
             patch.object(self.driver, "_find_whatsapp_window", return_value=MagicMock()), \
             patch.object(wa, "_paste_text"):
            with patch.object(self.driver, "uia") as mock_uia:
                mock_input = MagicMock()
                # Input field was NOT cleared after typing/enter (stuck or failed)
                mock_input.window_text.return_value = "Hello World"
                mock_uia.find_elements.side_effect = [
                    # 1. Input box found
                    {"status": "success", "matches": [{"_wrapper": mock_input}]},
                    # 2. History verification returns no matching sent bubble
                    {"status": "success", "matches": []},
                ]
                ok, err = self.driver.send_message_to("Alex", "Hello World")
                self.assertFalse(ok)
                self.assertIn("could not be confirmed", err.lower())

    def test_send_message_fails_safely_when_input_box_missing(self):
        """When the message input element cannot be found, return False and do not paste blind."""
        with patch.object(self.driver, "open_chat", return_value=(True, "Alex")), \
             patch.object(self.driver, "_find_whatsapp_window", return_value=MagicMock()), \
             patch.object(wa, "_paste_text") as mock_paste:
            with patch.object(self.driver, "uia") as mock_uia:
                # No Edit elements found for input
                mock_uia.find_elements.return_value = {"status": "success", "matches": []}
                ok, err = self.driver.send_message_to("Alex", "Hello World")
                self.assertFalse(ok)
                self.assertIn("input box could not be located", err.lower())
                mock_paste.assert_not_called()


class PluginPermissionGuardTestCase(unittest.TestCase):
    """Test PluginRegistry.run() strictly enforces ActionExecutionGuard."""

    def test_plugin_registry_run_routes_through_action_execution_guard(self):
        """Verify PluginRegistry.run invokes ActionExecutionGuard.guard_and_execute."""
        mock_run = MagicMock(return_value="Plugin Output")
        rec = PluginRecord(
            name="test_plugin",
            description="Test plugin",
            parameters={"type": "OBJECT", "properties": {"target": {"type": "STRING"}}, "required": ["target"]},
            run=mock_run,
            valid=True,
        )
        registry = PluginRegistry(plugins={"test_plugin": rec}, logger=lambda m: None)

        with patch("core.plugin_loader.get_plugin_enabled", return_value=True):
            with patch.object(ActionExecutionGuard, "guard_and_execute") as mock_guard:
                mock_guard.return_value = outcome.ActionResult(
                    status=outcome.STATUS_DONE,
                    action="test_plugin",
                    success=True,
                    verified=True,
                    message="Guarded plugin executed successfully",
                )
                res = registry.run("test_plugin", {"target": "sample"})
                self.assertTrue(mock_guard.called)
                call_args = mock_guard.call_args[1]
                self.assertEqual(call_args["action_name"], "test_plugin")
                self.assertEqual(call_args["parameters"], {"target": "sample"})
                self.assertIn("Guarded plugin executed successfully", res)

    def test_plugin_registry_rejects_invalid_schema_arguments(self):
        """Verify PluginRegistry.run validates schema before execution."""
        mock_run = MagicMock()
        rec = PluginRecord(
            name="strict_plugin",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "count": {"type": "INTEGER"},
                    "mode": {"type": "STRING", "enum": ["fast", "slow"]},
                },
                "required": ["count", "mode"],
            },
            run=mock_run,
            valid=True,
        )
        registry = PluginRegistry(plugins={"strict_plugin": rec}, logger=lambda m: None)

        with patch("core.plugin_loader.get_plugin_enabled", return_value=True):
            # Missing required field
            res1 = registry.run("strict_plugin", {"count": 5})
            self.assertIn("Invalid arguments", res1)
            self.assertIn("Missing required parameter 'mode'", res1)

            # Invalid enum value
            res2 = registry.run("strict_plugin", {"count": 5, "mode": "invalid_mode"})
            self.assertIn("Invalid arguments", res2)
            self.assertIn("not in allowed choices", res2)
            mock_run.assert_not_called()


class OrchestratorRecoveryTestCase(unittest.TestCase):
    """Test AgentOrchestrator updates step_results[-1].success=True on bounded recovery."""

    def test_orchestrator_updates_step_result_success_on_recovery(self):
        """When bounded recovery succeeds, step_results[-1].success and verified are True."""
        mock_registry = MagicMock()
        mock_registry.get_tool_declarations.return_value = []
        mock_registry.has.return_value = True

        # Step 1 fails on initial execution; subsequent steps succeed if prerequisite passes
        initial_fail = outcome.ActionResult(
            status=outcome.STATUS_FAILED,
            action="click_element",
            success=False,
            verified=False,
            message="Element not found via UIA",
        )
        def mock_exec(action, params, ctx=None):
            if action == "verify_state":
                return outcome.ActionResult(
                    status=outcome.STATUS_DONE,
                    action="verify_state",
                    success=True,
                    verified=True,
                    message="State verified",
                )
            return initial_fail

        mock_registry.execute.side_effect = mock_exec

        orchestrator = AgentOrchestrator(registry=mock_registry, log_fn=lambda m: None)

        # Plan with 2 steps where step 2 requires step 1 (index 0)
        test_plan = [
            {"thought": "Click target button", "action": "click_element", "parameters": {"target": "SubmitButton"}},
            {"thought": "Verify next step", "action": "verify_state", "parameters": {"expected_window": "Home"}, "requires": [0]},
        ]

        with patch.object(orchestrator, "observe_screen", return_value={"summary": "desktop", "window_title": "Home"}), \
             patch.object(orchestrator, "plan_task", return_value=test_plan):
            # Mock target_resolver and computer_use_engine for bounded recovery
            mock_res_target = MagicMock(desktop_x=500, desktop_y=400)
            mock_click_res = outcome.ActionResult(
                status=outcome.STATUS_DONE,
                action="click_coordinates",
                success=True,
                verified=True,
                message="Clicked at (500, 400)",
            )

            with patch("actions.target_resolver.get_target_resolver") as mock_get_tr, \
                 patch("actions.computer_use_engine.get_computer_use_engine") as mock_get_eng:
                mock_tr = MagicMock()
                mock_tr.resolve_target.return_value = (mock_res_target, None)
                mock_get_tr.return_value = mock_tr

                mock_eng = MagicMock()
                mock_eng.click_coordinates.return_value = mock_click_res
                mock_get_eng.return_value = mock_eng

                summary = orchestrator.run_autonomous_task("Click submit and verify", max_steps=5)

                self.assertIn("Recovered via visual grounding", summary)
                # Ensure the entire plan succeeded and did NOT halt at step 2
                self.assertNotIn("Plan halted", summary)
                self.assertIn("Step 2 (verify_state): ✓", summary)


if __name__ == "__main__":
    unittest.main()
