"""
tests/test_permission_settings_comprehensive.py — Comprehensive tests for Permission Settings System.

Verifies:
1. Permission policy resolution: ASK, ALLOW, DENY.
2. Default policies by tier (READ_ONLY & NORMAL -> ALLOW; SENSITIVE & DESTRUCTIVE -> ASK).
3. Timed allow mode ("allow for N minutes") with auto-revert upon expiry.
4. Security rule: Model / untrusted code cannot change permissions (SecurityViolationError).
5. Permission decision audit logging (recording timestamp, action, decision).
6. Central dispatcher and ActionExecutionGuard policy enforcement (DENY blocks, ALLOW bypasses confirmation).
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from core import outcome
from core.permissions import (
    PermissionEntry,
    get_permission_registry,
    resolve_permission_policy,
    resolve_action_policy,
    set_permission_policy,
    set_timed_allow,
    get_timed_allows,
    clear_timed_allow,
    reset_permissions_to_defaults,
    log_permission_decision,
    get_recent_permission_decisions,
    clear_recent_permission_decisions,
    POLICY_ALLOW,
    POLICY_ASK,
    POLICY_DENY,
    READ_ONLY,
    NORMAL,
    SENSITIVE,
    DESTRUCTIVE,
    FORBIDDEN,
    SecurityViolationError,
    ActionExecutionGuard,
    set_untrusted_context,
)
from memory import config_manager


class TestPermissionSettingsComprehensive(unittest.TestCase):
    def setUp(self):
        set_untrusted_context(False)
        reset_permissions_to_defaults(from_ui=True)
        clear_recent_permission_decisions()

    def tearDown(self):
        set_untrusted_context(False)
        reset_permissions_to_defaults(from_ui=True)
        clear_recent_permission_decisions()

    def test_default_policies_by_tier(self):
        """Verify defaults: READ_ONLY & NORMAL default to ALLOW; SENSITIVE & DESTRUCTIVE default to ASK."""
        reg = get_permission_registry()

        # READ_ONLY & NORMAL
        entry_ro = reg.get("file:read")
        self.assertIsNotNone(entry_ro)
        self.assertEqual(entry_ro.tier, READ_ONLY)
        self.assertEqual(resolve_permission_policy("file:read"), POLICY_ALLOW)

        entry_norm = reg.get("network:wifi_toggle")
        self.assertIsNotNone(entry_norm)
        self.assertEqual(entry_norm.tier, NORMAL)
        self.assertEqual(resolve_permission_policy("network:wifi_toggle"), POLICY_ALLOW)

        # SENSITIVE & DESTRUCTIVE
        entry_sens = reg.get("file:create_write")
        self.assertIsNotNone(entry_sens)
        self.assertEqual(entry_sens.tier, SENSITIVE)
        self.assertEqual(resolve_permission_policy("file:create_write"), POLICY_ASK)

        entry_dest = reg.get("system:shutdown")
        self.assertIsNotNone(entry_dest)
        self.assertEqual(entry_dest.tier, DESTRUCTIVE)
        self.assertEqual(resolve_permission_policy("system:shutdown"), POLICY_ASK)

    def test_policy_resolution_override_ask_allow_deny(self):
        """Verify setting explicit overrides changes effective policy."""
        # 1. Override a sensitive action to ALLOW
        set_permission_policy("file:create_write", POLICY_ALLOW, from_ui=True)
        self.assertEqual(resolve_permission_policy("file:create_write"), POLICY_ALLOW)

        # 2. Override a normal action to ASK
        set_permission_policy("network:wifi_toggle", POLICY_ASK, from_ui=True)
        self.assertEqual(resolve_permission_policy("network:wifi_toggle"), POLICY_ASK)

        # 3. Override an action to DENY
        set_permission_policy("network:wifi_toggle", POLICY_DENY, from_ui=True)
        self.assertEqual(resolve_permission_policy("network:wifi_toggle"), POLICY_DENY)

        # 4. Reset to defaults reverts overrides
        reset_permissions_to_defaults(from_ui=True)
        self.assertEqual(resolve_permission_policy("file:create_write"), POLICY_ASK)
        self.assertEqual(resolve_permission_policy("network:wifi_toggle"), POLICY_ALLOW)

    def test_timed_allow_with_automatic_reversion(self):
        """Verify 'allow for N minutes' temporarily grants ALLOW and reverts after expiry."""
        perm_id = "process:close_app"
        # By default, SENSITIVE action resolves to ASK
        self.assertEqual(resolve_permission_policy(perm_id), POLICY_ASK)

        # Set timed allow for 1 minute
        set_timed_allow(perm_id, minutes=1.0, from_ui=True)
        timed = get_timed_allows()
        self.assertIn(perm_id, timed)
        self.assertGreater(timed[perm_id], time.time())
        # While active, effective policy is ALLOW
        self.assertEqual(resolve_permission_policy(perm_id), POLICY_ALLOW)

        # Simulate expiration
        with patch("time.time", return_value=time.time() + 120.0):
            # Once expired, policy automatically reverts to ASK
            self.assertEqual(resolve_permission_policy(perm_id), POLICY_ASK)

        clear_timed_allow(perm_id)

    def test_security_rule_model_cannot_change_permissions(self):
        """Security rule: Model / untrusted code must be rejected when attempting to change permissions."""
        # Attempt to change policy without from_ui flag
        with self.assertRaises(SecurityViolationError):
            set_permission_policy("system:shutdown", POLICY_ALLOW, from_ui=False)

        with self.assertRaises(SecurityViolationError):
            set_timed_allow("system:shutdown", minutes=10.0, from_ui=False)

        with self.assertRaises(SecurityViolationError):
            reset_permissions_to_defaults(from_ui=False)

        # Attempt to change from untrusted context even if from_ui=True
        set_untrusted_context(True)
        with self.assertRaises(SecurityViolationError):
            set_permission_policy("system:shutdown", POLICY_ALLOW, from_ui=True)

    def test_central_enforcement_blocks_denied_actions(self):
        """Verify ActionExecutionGuard immediately blocks actions configured to DENY."""
        set_permission_policy("network:wifi_toggle", POLICY_DENY, from_ui=True)

        executed = []
        dummy_handler = lambda **kw: executed.append("executed") or "Done"

        res = ActionExecutionGuard.guard_and_execute(
            action_name="windows_control",
            parameters={"action": "set_wifi_enabled", "value": "off"},
            handler=dummy_handler,
            ctx={},
        )
        self.assertEqual(res.status, outcome.STATUS_BLOCKED)
        self.assertFalse(res.success)
        self.assertIn("denied", str(res.message).lower())
        self.assertEqual(len(executed), 0)

    def test_central_enforcement_allows_overridden_actions(self):
        """Verify ActionExecutionGuard executes immediately when policy is set to ALLOW."""
        # SENSITIVE action by default asks for confirmation
        set_permission_policy("process:close_app", POLICY_ALLOW, from_ui=True)

        executed = []
        dummy_handler = lambda **kw: executed.append("executed") or "Closed Calculator"

        res = ActionExecutionGuard.guard_and_execute(
            action_name="computer_control",
            parameters={"action": "close_app", "app_name": "calculator"},
            handler=dummy_handler,
            ctx={},
        )
        self.assertEqual(res.status, outcome.STATUS_DONE)
        self.assertTrue(res.success)
        self.assertEqual(len(executed), 1)

    def test_permission_decision_logging(self):
        """Verify decisions are recorded in the recent decisions log."""
        clear_recent_permission_decisions()
        log_permission_decision("system:shutdown", "windows_control", "ASK", "Awaiting user confirmation")
        log_permission_decision("file:delete", "file_controller", "DENIED", "Policy set to DENY")
        log_permission_decision("file:read", "file_controller", "ALLOWED", "Read-only policy")

        decisions = get_recent_permission_decisions(limit=10)
        self.assertEqual(len(decisions), 3)
        self.assertEqual(decisions[0]["decision"], "ASK")
        self.assertEqual(decisions[1]["decision"], "DENIED")
        self.assertEqual(decisions[2]["decision"], "ALLOWED")


if __name__ == "__main__":
    unittest.main()
