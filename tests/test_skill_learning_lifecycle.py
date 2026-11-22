"""
tests/test_skill_learning_lifecycle.py — Test Suite for Skill Learning Lifecycle, Storage, Deduplication, Quota, and Safety (Parts 5–8).
"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.outcome import ActionResult, result_done, result_failed
from core.risk_tiers import RiskTier, get_action_tier, is_destructive_protected
from core.skill_learning.cooldown_queue import CooldownQueue, is_quota_error
from core.skill_learning.learning_manager import LearningManager, get_default_learning_manager
from core.skill_learning.models import (
    ParameterDefinition,
    SkillDefinition,
    SkillStatus,
    StepDefinition,
)
from core.skill_learning.skill_storage import (
    delete_skill,
    edit_skill,
    find_near_duplicate,
    load_skill,
    rename_skill,
    restore_skill,
    save_skill,
)
from core.task_approval import TaskApprovalScope


class TestSkillStorageAndMetadata(unittest.TestCase):
    def setUp(self):
        self.skill = SkillDefinition(
            id="lifecycle_test_skill",
            name="Lifecycle Test Skill",
            description="Testing versioned storage and lifecycle fields",
            category="user",
            application="Notepad",
            source="demonstration",
            risk_tier="LOW",
            usage_count=5,
            success_count=4,
            failure_count=1,
            consecutive_failures=0,
            needs_relearning=False,
            steps=[StepDefinition(step_id=1, thought="Type", tool="windows_uia", parameters={"text": "hello"})],
            status=SkillStatus.APPROVED.value,
        )

    def tearDown(self):
        delete_skill(self.skill.id)

    def test_versioned_storage_and_success_rate(self):
        # 1. Verify computed success rate
        self.assertEqual(self.skill.success_rate, 0.8)

        # 2. Save skill and check persisted JSON
        p = save_skill(self.skill)
        self.assertTrue(p.exists())
        raw_data = json.loads(p.read_text(encoding="utf-8"))

        self.assertEqual(raw_data["source"], "demonstration")
        self.assertEqual(raw_data["risk_tier"], "LOW")
        self.assertEqual(raw_data["usage_count"], 5)
        self.assertEqual(raw_data["success_rate"], 0.8)
        self.assertFalse(raw_data["needs_relearning"])

        # 3. Reload and verify round-trip
        loaded = load_skill(self.skill.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.usage_count, 5)
        self.assertEqual(loaded.success_count, 4)
        self.assertEqual(loaded.success_rate, 0.8)


class TestDeduplication(unittest.TestCase):
    def setUp(self):
        self.existing = SkillDefinition(
            id="whatsapp_send_report",
            name="WhatsApp Send Report",
            description="Sends daily status report to team on WhatsApp",
            category="application",
            application="WhatsApp",
            intent_examples=["Send report to team", "WhatsApp daily report"],
            steps=[StepDefinition(step_id=1, thought="Send", tool="whatsapp_send", parameters={})],
            status=SkillStatus.APPROVED.value,
        )
        save_skill(self.existing)

    def tearDown(self):
        delete_skill(self.existing.id)

    def test_finds_near_duplicate_by_name(self):
        candidate = SkillDefinition(
            id="send_report_whatsapp",
            name="WhatsApp Send Report",  # Same name
            description="Another way to send reports on WhatsApp",
            category="application",
            application="WhatsApp",
            steps=[],
        )
        dup = find_near_duplicate(candidate)
        self.assertIsNotNone(dup)
        self.assertEqual(dup.id, self.existing.id)

    def test_finds_near_duplicate_by_intent(self):
        candidate = SkillDefinition(
            id="team_report_whatsapp",
            name="Transmit Team Document",
            description="Transmit document to team",
            category="application",
            application="WhatsApp",
            intent_examples=["Send report to team"],  # Exact intent match
            steps=[],
        )
        dup = find_near_duplicate(candidate)
        self.assertIsNotNone(dup)
        self.assertEqual(dup.id, self.existing.id)

    def test_distinct_skill_is_not_flagged_as_duplicate(self):
        candidate = SkillDefinition(
            id="discord_share_meme",
            name="Discord Share Meme",
            description="Post meme in Discord channel",
            category="application",
            application="Discord",
            steps=[],
        )
        dup = find_near_duplicate(candidate)
        self.assertIsNone(dup)


class TestVoiceCommandsAndLifecycle(unittest.TestCase):
    def setUp(self):
        self.manager = get_default_learning_manager()
        self.skill = SkillDefinition(
            id="voice_cmd_skill",
            name="Voice Command Skill",
            description="Testing voice lifecycle operations",
            category="user",
            application="Calculator",
            steps=[StepDefinition(step_id=1, thought="Calc", tool="open_app", parameters={"app_name": "calc"})],
            status=SkillStatus.APPROVED.value,
        )
        save_skill(self.skill)
        self.manager.registry.save_and_register(self.skill)

    def tearDown(self):
        delete_skill(self.skill.id)
        delete_skill("renamed_voice_skill")

    def test_show_skill(self):
        info = self.manager.show_skill("voice_cmd_skill")
        self.assertIsNotNone(info)
        self.assertEqual(info["name"], "Voice Command Skill")
        self.assertEqual(info["application"], "Calculator")

    def test_rename_skill(self):
        res = self.manager.rename_skill("voice_cmd_skill", "Renamed Voice Skill")
        self.assertTrue(res.is_success())
        loaded = load_skill("voice_cmd_skill")
        self.assertEqual(loaded.name, "Renamed Voice Skill")

    def test_edit_skill_description(self):
        res = self.manager.edit_skill("voice_cmd_skill", "description", "Updated description for test")
        self.assertTrue(res.is_success())
        loaded = load_skill("voice_cmd_skill")
        self.assertEqual(loaded.description, "Updated description for test")

    def test_disable_and_restore_skill(self):
        # 1. Disable
        dis_res = self.manager.disable_skill("voice_cmd_skill")
        self.assertTrue(dis_res.is_success())
        self.assertEqual(load_skill("voice_cmd_skill").status, SkillStatus.DISABLED.value)

        # 2. Restore
        res_res = self.manager.restore_skill("voice_cmd_skill")
        self.assertTrue(res_res.is_success())
        self.assertEqual(load_skill("voice_cmd_skill").status, SkillStatus.APPROVED.value)


class TestSelfImprovementThreeFailures(unittest.TestCase):
    def setUp(self):
        self.manager = get_default_learning_manager()
        self.skill = SkillDefinition(
            id="failing_lifecycle_skill",
            name="Failing Lifecycle Skill",
            description="Skill that fails consecutively",
            category="user",
            steps=[StepDefinition(step_id=1, thought="Fail", tool="open_app", parameters={"app_name": "GhostApp"})],
            status=SkillStatus.APPROVED.value,
        )
        save_skill(self.skill)
        self.manager.registry.save_and_register(self.skill)

    def tearDown(self):
        delete_skill(self.skill.id)

    def test_three_consecutive_failures_trigger_relearning(self):
        with patch.object(self.manager.executor.registry, "execute", return_value=result_failed("open_app", "App crashed")):
            # Failure 1
            self.manager.execute_skill("failing_lifecycle_skill")
            s = load_skill("failing_lifecycle_skill")
            self.assertEqual(s.consecutive_failures, 1)
            self.assertFalse(s.needs_relearning)

            # Failure 2
            self.manager.execute_skill("failing_lifecycle_skill")
            s = load_skill("failing_lifecycle_skill")
            self.assertEqual(s.consecutive_failures, 2)
            self.assertFalse(s.needs_relearning)

            # Failure 3 -> should trigger needs_relearning = True
            self.manager.execute_skill("failing_lifecycle_skill")
            s = load_skill("failing_lifecycle_skill")
            self.assertEqual(s.consecutive_failures, 3)
            self.assertTrue(s.needs_relearning)
            self.assertEqual(len(s.failure_history), 3)

            # Next unforced attempt should refuse and suggest re-learning
            attempt_res = self.manager.execute_skill("failing_lifecycle_skill")
            self.assertFalse(attempt_res.is_success())
            self.assertIn("needs re-learning", attempt_res.message)
            self.assertEqual(attempt_res.error, "NeedsRelearning")


class TestQuotaAndCooldownHandling(unittest.TestCase):
    def setUp(self):
        self.cooldown = CooldownQueue.get_instance()
        self.cooldown.clear()

    def tearDown(self):
        self.cooldown.clear()

    def test_is_quota_error(self):
        self.assertTrue(is_quota_error("429 Client Error: Too Many Requests"))
        self.assertTrue(is_quota_error("ResourceExhausted: Quota exceeded for quota metric"))
        self.assertTrue(is_quota_error("rate limit reached"))
        self.assertFalse(is_quota_error("ValueError: Invalid argument"))

    def test_cooldown_queue_lifecycle(self):
        self.assertFalse(self.cooldown.is_in_cooldown())
        self.cooldown.set_cooldown(10.0)
        self.assertTrue(self.cooldown.is_in_cooldown())
        self.assertGreater(self.cooldown.get_remaining_cooldown(), 0.0)

        # Enqueue item
        self.cooldown.enqueue("learn_from_description", {"name": "Test", "desc": "something"})
        self.assertTrue(self.cooldown.has_pending())
        item = self.cooldown.peek()
        self.assertEqual(item["type"], "learn_from_description")

        # Notification test
        player = MagicMock()
        msg = self.cooldown.notify_user_cooldown(player=player)
        self.assertIn("cooldown", msg.lower())
        player.write_log.assert_called_once()


class TestSafetyGuardrails(unittest.TestCase):
    def test_delete_skill_is_high_risk_and_protected(self):
        # 1. Classification is HIGH
        tier = get_action_tier("skill_learner", {"action": "delete_skill", "skill_id": "test"})
        self.assertEqual(tier, RiskTier.HIGH)

        # 2. Is in protected destructive operations
        self.assertTrue(is_destructive_protected("skill_learner", "delete_skill"))

        # 3. TaskApprovalScope CANNOT bypass delete_skill even if skill_learner is in allowed_tools
        scope = TaskApprovalScope(
            scope_id="test_scope",
            goal="Manage skills",
            allowed_tools={"skill_learner", "open_app"},
        )
        self.assertFalse(
            scope.matches_action("skill_learner", {"action": "delete_skill", "skill_id": "important_skill"}),
            "TaskApprovalScope must never match protected destructive action delete_skill"
        )


if __name__ == "__main__":
    unittest.main()
