"""
tests/test_tasks_recovery.py — Unit tests for background task failure recovery,
side-effect protection, classification, and honest verification.
"""

import unittest
from unittest.mock import MagicMock, patch

from core import outcome
from core.tasks.recovery import (
    CLASSIFICATION_BLOCKED,
    CLASSIFICATION_FIXABLE,
    CLASSIFICATION_TRANSIENT,
    CLASSIFICATION_UNKNOWN,
    build_failure_report,
    classify_failure,
    compute_backoff,
    execute_step_with_recovery,
    has_side_effects,
    verify_state_before_retry,
)


class TestTasksRecovery(unittest.TestCase):
    def test_01_classification(self):
        """Verify accurate failure classification for transient, fixable, blocked, and unknown."""
        # Transient
        res_timeout = outcome.failed("web_search", "Request timed out after 10000ms")
        self.assertEqual(classify_failure("web_search", {}, res_timeout), CLASSIFICATION_TRANSIENT)

        res_quota = outcome.failed("gemini", "429 Quota resource_exhausted")
        self.assertEqual(classify_failure("gemini", {}, res_quota), CLASSIFICATION_TRANSIENT)

        # Fixable
        res_param = outcome.failed("file_reader", "missing required argument: 'path'")
        self.assertEqual(classify_failure("file_reader", {}, res_param), CLASSIFICATION_FIXABLE)

        res_nofile = outcome.failed("read_file", "No such file or directory: notes.txt")
        self.assertEqual(classify_failure("read_file", {}, res_nofile), CLASSIFICATION_FIXABLE)

        # Blocked
        res_perm = outcome.failed("delete_file", "Permission denied: access is restricted")
        self.assertEqual(classify_failure("delete_file", {}, res_perm), CLASSIFICATION_BLOCKED)

        res_conf = outcome.ActionResult.needs_confirmation(
            action="whatsapp_send",
            message="Needs user approval",
        )
        self.assertEqual(classify_failure("whatsapp_send", {}, res_conf), CLASSIFICATION_BLOCKED)

        # Unknown
        res_unk = outcome.failed("custom_tool", "Bizarre hardware glitch #882")
        self.assertEqual(classify_failure("custom_tool", {}, res_unk), CLASSIFICATION_UNKNOWN)

    def test_02_side_effects_detection_and_safety(self):
        """Verify side-effect tools are identified and never blindly retried without verification."""
        self.assertTrue(has_side_effects("send_message", {}))
        self.assertTrue(has_side_effects("whatsapp_send", {}))
        self.assertTrue(has_side_effects("workspace_write_file", {}))
        self.assertTrue(has_side_effects("delete_file", {}))
        self.assertFalse(has_side_effects("web_search", {}))
        self.assertFalse(has_side_effects("read_file", {}))

        # Attempting retry on unverified message send MUST be refused
        can_retry, reason = verify_state_before_retry("send_message", {"recipient": "Alice"})
        self.assertFalse(can_retry)
        self.assertIn("blind retry aborted", reason.lower())

    def test_03_transient_retry_succeeds(self):
        """Verify transient failure retries with backoff and succeeds on 2nd attempt."""
        call_count = 0
        logged_attempts = []

        def mock_execute(t_name, params):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return outcome.failed(t_name, "Connection reset by peer: econnreset")
            return outcome.done(t_name, result="Search results found", verified=True)

        res = execute_step_with_recovery(
            task_id="t1",
            task_title="Research topic",
            tool_name="web_search",
            parameters={"query": "Quantum computing"},
            execute_fn=mock_execute,
            on_attempt_logged=lambda att: logged_attempts.append(att),
            sleep_fn=lambda s: None,  # instantaneous for tests
        )

        self.assertEqual(res.status, outcome.STATUS_DONE)
        self.assertTrue(res.verified)
        self.assertEqual(call_count, 2)
        self.assertEqual(len(logged_attempts), 2)
        self.assertEqual(logged_attempts[0]["classification"], CLASSIFICATION_TRANSIENT)
        self.assertEqual(logged_attempts[1]["status"], outcome.STATUS_DONE)

    def test_04_transient_exhaustion_reports_plain_failure(self):
        """Verify 3 transient failures exhaust retries and produce plain-language report."""
        call_count = 0
        logged_attempts = []

        def mock_execute(t_name, params):
            nonlocal call_count
            call_count += 1
            return outcome.failed(t_name, "503 Service Temporarily Unavailable")

        res = execute_step_with_recovery(
            task_id="t2",
            task_title="Fetch weather",
            tool_name="weather_report",
            parameters={"city": "Tokyo"},
            execute_fn=mock_execute,
            on_attempt_logged=lambda att: logged_attempts.append(att),
            sleep_fn=lambda s: None,
        )

        self.assertEqual(res.status, outcome.STATUS_FAILED)
        self.assertFalse(res.verified)
        self.assertEqual(call_count, 3)
        self.assertEqual(len(logged_attempts), 3)

        report = res.data.get("recovery_report")
        self.assertIsNotNone(report)
        self.assertEqual(report["classification"], CLASSIFICATION_TRANSIENT)
        self.assertIn("Service Temporarily Unavailable", report["root_cause"])
        self.assertIn("Check your internet connection", report["user_action"])
        self.assertIn("Attempt 1", report["plain_report"])

    def test_05_fixable_alternative_path(self):
        """Verify fixable error switches to alternative route (e.g. query simplification) and succeeds."""
        calls = []

        def mock_execute(t_name, params):
            calls.append((t_name, dict(params)))
            if len(calls) == 1:
                return outcome.failed(t_name, "query too long or syntax error")
            return outcome.done(t_name, result="Results", verified=True)

        res = execute_step_with_recovery(
            task_id="t3",
            task_title="Search paper",
            tool_name="web_search",
            parameters={"query": "very long query that exceeds reasonable parser limits here"},
            execute_fn=mock_execute,
            sleep_fn=lambda s: None,
        )

        self.assertEqual(res.status, outcome.STATUS_DONE)
        self.assertTrue(res.verified)
        self.assertEqual(len(calls), 2)
        # Second call query was simplified
        self.assertNotEqual(calls[0][1]["query"], calls[1][1]["query"])

    def test_06_blind_retry_on_side_effects_is_blocked(self):
        """Verify step with side effects refuses blind retry when status is unverified."""
        call_count = 0

        def mock_execute(t_name, params):
            nonlocal call_count
            call_count += 1
            # First attempt returns unverified/transient error
            return outcome.failed(t_name, "Network timeout during message dispatch")

        res = execute_step_with_recovery(
            task_id="t4",
            task_title="Send alert",
            tool_name="whatsapp_send",
            parameters={"contact": "Alice", "message": "Alert!"},
            execute_fn=mock_execute,
            sleep_fn=lambda s: None,
        )

        self.assertEqual(res.status, outcome.STATUS_FAILED)
        self.assertFalse(res.verified)
        # MUST ONLY execute once: second blind retry is blocked!
        self.assertEqual(call_count, 1)
        self.assertIn("Refused blind retry", res.error)

    def test_07_confirmation_requirement_is_never_bypassed(self):
        """Verify recovery immediately returns when confirmation is requested without retrying."""
        calls = 0

        def mock_execute(t_name, params):
            nonlocal calls
            calls += 1
            return outcome.ActionResult.needs_confirmation(
                action=t_name,
                message="User confirmation required for system action.",
            )

        res = execute_step_with_recovery(
            task_id="t5",
            task_title="System reboot",
            tool_name="windows_control",
            parameters={"action": "restart"},
            execute_fn=mock_execute,
        )

        self.assertEqual(res.status, outcome.STATUS_NEEDS_CONFIRMATION)
        self.assertEqual(calls, 1)

    def test_08_exponential_backoff_calculation(self):
        """Verify compute_backoff scales exponentially with jitter."""
        d1 = compute_backoff(1, base=1.0, jitter=False)
        d2 = compute_backoff(2, base=1.0, jitter=False)
        d3 = compute_backoff(3, base=1.0, jitter=False)
        self.assertEqual(d1, 1.0)
        self.assertEqual(d2, 2.0)
        self.assertEqual(d3, 4.0)

        # With jitter
        d1_jitter = compute_backoff(1, base=1.0, jitter=True)
        self.assertGreaterEqual(d1_jitter, 1.0)
        self.assertLessEqual(d1_jitter, 1.25)


if __name__ == "__main__":
    unittest.main()
