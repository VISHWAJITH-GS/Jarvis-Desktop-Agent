"""
core/skill_learning/learning_manager.py — Demonstration & Learning Lifecycle Manager.

Orchestrates the entire learning-by-demonstration loop:
- Starting / pausing / resuming / stopping learning sessions
- Coordinating live UI observation and event recording
- Abstracting demonstrations into reusable declarative skills using Gemini
- Validating and dry-testing generated workflows
- Enforcing user approval gates before autonomous activation
- Managing skill execution and persistence
"""

from __future__ import annotations

from datetime import datetime
import logging
import time
from typing import Any, Dict, List, Optional
import uuid

from core.outcome import ActionResult, result_done, result_failed
from core.skill_learning.cooldown_queue import CooldownQueue, is_quota_error
from core.skill_learning.models import LearningSession, SkillDefinition, SkillStatus
from core.skill_learning.observation_engine import ObservationEngine
from core.skill_learning.skill_executor import SkillExecutor
from core.skill_learning.skill_generator import SkillGenerator
from core.skill_learning.skill_registry import get_default_skill_registry
from core.skill_learning.skill_storage import (
    delete_skill as storage_delete_skill,
    edit_skill as storage_edit_skill,
    find_near_duplicate,
    load_skill,
    rename_skill as storage_rename_skill,
    restore_skill as storage_restore_skill,
    save_skill,
    update_skill_status,
)
from core.skill_learning.skill_validator import SkillValidator

logger = logging.getLogger(__name__)


class LearningManager:
    """Central controller for learning-by-demonstration sessions."""

    _instance: Optional[LearningManager] = None

    def __init__(
        self,
        observation_engine: Optional[ObservationEngine] = None,
        generator: Optional[SkillGenerator] = None,
        validator: Optional[SkillValidator] = None,
        executor: Optional[SkillExecutor] = None,
        action_registry=None,
    ):
        from core.action_loader import get_default_registry
        self.action_registry = action_registry or get_default_registry()
        self.observation_engine = observation_engine or ObservationEngine()
        self.generator = generator or SkillGenerator()
        self.validator = validator or SkillValidator(action_registry=self.action_registry)
        self.executor = executor or SkillExecutor(action_registry=self.action_registry)
        self.registry = get_default_skill_registry()

        self.current_session: Optional[LearningSession] = None

    @classmethod
    def get_instance(cls, action_registry=None) -> LearningManager:
        if cls._instance is None:
            cls._instance = cls(action_registry=action_registry)
        elif action_registry is not None and cls._instance.action_registry != action_registry:
            cls._instance.action_registry = action_registry
            cls._instance.validator.registry = action_registry
            cls._instance.executor.registry = action_registry
        return cls._instance

    def is_learning_active(self) -> bool:
        """Check if a learning session is currently recording or paused."""
        return self.current_session is not None and self.current_session.status in ("recording", "paused")

    def start_learning(self, goal: str, player=None) -> str:
        """Initiate a new demonstration recording session."""
        if self.is_learning_active():
            return f"A learning session is already active for: '{self.current_session.goal}'. Please stop or cancel it first."

        session_id = f"learn_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        self.current_session = LearningSession(
            session_id=session_id,
            goal=goal,
            status="recording",
            started_at=time.time(),
        )

        # Start live observation hooks
        self.observation_engine.start_recording()

        msg = (
            f"Learning mode started for: '{goal}'. "
            "Please perform the workflow on your desktop. Say 'Stop learning' when finished."
        )
        if player and hasattr(player, "show_content"):
            player.show_content(
                "SKILL LEARNING ACTIVE",
                f"Observing demonstration for: '{goal}'\n\n"
                "• Jarvis is purely observant (no execution, no confirmations)\n"
                "• Capturing clicks, shortcuts, typing, navigation, file dialogs\n"
                "• Passwords & secrets are redacted\n"
                "• Spoken narration is aligned as intent clues\n\n"
                "Perform actions on screen. Say 'Stop learning' when finished."
            )
        if player and hasattr(player, "write_log"):
            player.write_log(f"🔴 [LEARNING MODE] {msg}")

        print("[Learning] Session started.")
        logger.info("[Learning] Session started. (session_id=%s, goal=%s)", session_id, goal)
        return msg

    def record_narration(self, text: str, timestamp: Optional[float] = None) -> Optional[UIEvent]:
        """Record spoken user narration aligned by timestamp to demonstration events."""
        if not self.is_learning_active():
            return None
        evt = self.observation_engine.recorder.record_narration(text, timestamp)
        if evt and self.current_session:
            self.current_session.events.append(evt)
        return evt

    def pause_learning(self, player=None) -> str:
        """Temporarily pause event capture."""
        if not self.current_session or self.current_session.status != "recording":
            return "No active recording session to pause."

        self.observation_engine.pause()
        self.current_session.status = "paused"
        msg = "Learning session paused. Event recording is temporarily suspended."
        if player and hasattr(player, "write_log"):
            player.write_log(f"⏸ [LEARNING MODE] {msg}")
        return msg

    def resume_learning(self, player=None) -> str:
        """Resume recording after a pause."""
        if not self.current_session or self.current_session.status != "paused":
            return "No paused recording session to resume."

        self.observation_engine.resume()
        self.current_session.status = "recording"
        msg = "Learning session resumed. Event recording active."
        if player and hasattr(player, "write_log"):
            player.write_log(f"▶ [LEARNING MODE] {msg}")
        return msg

    def stop_learning(self, player=None) -> dict:
        """Stop observation, retrieve recorded events, and automatically generate and approve skill."""
        if not self.current_session:
            return {"success": False, "message": "No active learning session to stop."}

        # Stop recording and collect sanitized events
        recorded_events = self.observation_engine.stop_recording()
        if recorded_events or not self.current_session.events:
            self.current_session.events = recorded_events
        events = self.current_session.events
        self.current_session.status = "analyzing"

        print(f"[Learning] Captured {len(events)} events.")
        logger.info("[Learning] Captured %d events.", len(events))

        msg = f"Demonstration stopped. Captured {len(events)} user interaction events."
        if player and hasattr(player, "write_log"):
            player.write_log(f"⏹ [LEARNING MODE] {msg} Generating skill...")

        logger.info("Session %s stopped with %d events", self.current_session.session_id, len(events))

        gen_skill_id = None
        if events:
            try:
                gen_res = self.generate_skill(player=player)
                if gen_res.get("success"):
                    gen_skill_id = gen_res.get("skill_id")
                    # Auto-approve newly demonstrated skill
                    self.approve_skill(gen_skill_id, player=player)
                    msg += f" Successfully generated and approved skill '{gen_res.get('name')}' (ID: '{gen_skill_id}') with {len(gen_res.get('steps', []))} steps. Ready to test or execute!"
            except Exception as e:
                logger.warning("Auto skill generation failed on stop_learning: %s", e)

        return {
            "success": True,
            "session_id": self.current_session.session_id if self.current_session else "",
            "goal": self.current_session.goal if self.current_session else "",
            "event_count": len(events),
            "generated_skill_id": gen_skill_id,
            "message": msg,
        }

    def cancel_learning(self, player=None) -> str:
        """Discard the current demonstration session completely."""
        if not self.current_session:
            return "No active learning session to cancel."

        self.observation_engine.cancel()
        goal = self.current_session.goal
        self.current_session = None

        msg = f"Learning session for '{goal}' has been cancelled and discarded."
        if player and hasattr(player, "write_log"):
            player.write_log(f"✖ [LEARNING MODE] {msg}")
        return msg

    def review_demonstration(self) -> dict:
        """Inspect the events captured during the demonstration."""
        if not self.current_session:
            return {"error": "No active session to review."}

        events = self.current_session.events
        trace = self.observation_engine.get_trace_summary(events)
        return {
            "session_id": self.current_session.session_id,
            "goal": self.current_session.goal,
            "status": self.current_session.status,
            "event_count": len(events),
            "trace_summary": trace,
            "generated_skill": self.current_session.generated_skill.to_dict()
            if self.current_session.generated_skill
            else None,
        }

    def generate_skill(self, player=None) -> dict:
        """Analyze the demonstration using Gemini and generate a declarative SkillDefinition."""
        if not self.current_session:
            return {"success": False, "message": "No recorded demonstration available to generate skill from."}

        if not self.current_session.events:
            return {"success": False, "message": "No events were recorded during the demonstration."}

        cooldown = CooldownQueue.get_instance()
        if cooldown.is_in_cooldown():
            cooldown.enqueue("generate_skill", {"session_id": self.current_session.session_id})
            msg = cooldown.notify_user_cooldown(player=player)
            return {"success": False, "queued": True, "cooldown_seconds": cooldown.get_remaining_cooldown(), "message": msg}

        if player and hasattr(player, "write_log"):
            player.write_log("⚙ Analyzing demonstration trace with Gemini AI...")

        try:
            skill = self.generator.generate_from_session(self.current_session)
            skill.source = "demonstration"

            # Check for near-duplicates
            near_dup = find_near_duplicate(skill)
            dup_info = None
            if near_dup:
                dup_info = {"duplicate_detected": True, "duplicate_of": near_dup.id, "existing_name": near_dup.name}
                logger.info("Near-duplicate skill detected: '%s' matches existing '%s'", skill.name, near_dup.name)

            # Validate schema and tools with single repair attempt
            skill, val_report = self.validator.validate_and_repair(skill)
            self.current_session.validation_report = val_report
            self.current_session.generated_skill = skill

            if not val_report["valid"]:
                return {
                    "success": False,
                    "skill_id": skill.id,
                    "message": f"Skill validation failed after repair attempt: {'; '.join(val_report['errors'])}. Would you like to re-learn this workflow?",
                    "validation": val_report,
                }

            # Save draft skill to disk
            save_skill(skill)
            self.registry.save_and_register(skill)

            print("[Learning] Awaiting user approval.")
            logger.info("[Learning] Awaiting user approval. (skill_id=%s)", skill.id)

            self.current_session.status = "review_ready"

            msg = (
                f"Generated draft skill '{skill.name}' (ID: {skill.id}) with {len(skill.steps)} steps. "
                "Skill is ready for testing and approval."
            )
            if dup_info:
                msg += f" (Note: similar to existing skill '{dup_info['existing_name']}')"

            if player and hasattr(player, "write_log"):
                player.write_log(f"✔ {msg}")

            res_dict = {
                "success": True,
                "skill_id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "steps": [s.to_dict() for s in skill.steps],
                "parameters": [p.to_dict() for p in skill.parameters],
                "validation": val_report,
                "message": msg,
            }
            if dup_info:
                res_dict["duplicate_info"] = dup_info
            return res_dict
        except Exception as e:
            if is_quota_error(e):
                cooldown.set_cooldown(60.0)
                cooldown.enqueue("generate_skill", {"session_id": self.current_session.session_id if self.current_session else ""})
                msg = cooldown.notify_user_cooldown(player=player)
                return {"success": False, "queued": True, "cooldown_seconds": 60.0, "message": msg}
            logger.exception("Error generating skill from demonstration")
            return {"success": False, "message": f"Failed to generate skill: {e}"}

    def learn_from_description(self, name: str, description: str, player=None) -> dict:
        """Create a validated SkillDefinition directly from natural language description without demonstration."""
        cooldown = CooldownQueue.get_instance()
        if cooldown.is_in_cooldown():
            cooldown.enqueue("learn_from_description", {"name": name, "description": description})
            msg = cooldown.notify_user_cooldown(player=player)
            return {"success": False, "queued": True, "cooldown_seconds": cooldown.get_remaining_cooldown(), "message": msg}

        if player and hasattr(player, "write_log"):
            player.write_log(f"⚙ Learning skill '{name}' from description...")

        try:
            skill = self.generator.generate_from_description(goal=name, description=description)
            skill.source = "description"

            # Check for near-duplicates
            near_dup = find_near_duplicate(skill)
            dup_info = None
            if near_dup:
                dup_info = {"duplicate_detected": True, "duplicate_of": near_dup.id, "existing_name": near_dup.name}
                logger.info("Near-duplicate skill detected: '%s' matches existing '%s'", skill.name, near_dup.name)

            skill, val_report = self.validator.validate_and_repair(skill)
            if not val_report["valid"]:
                return {
                    "success": False,
                    "skill_id": skill.id,
                    "message": f"Skill validation failed after repair attempt: {'; '.join(val_report['errors'])}. Please re-phrase or re-learn.",
                    "validation": val_report,
                }
            save_skill(skill)
            self.registry.save_and_register(skill)

            msg = (
                f"Generated skill '{skill.name}' (ID: {skill.id}) with {len(skill.steps)} steps from description. "
                "Validation completed."
            )
            if dup_info:
                msg += f" (Note: similar to existing skill '{dup_info['existing_name']}')"

            if player and hasattr(player, "show_content"):
                player.show_content(
                    "SKILL CREATED FROM DESCRIPTION",
                    f"{msg}\n\nSteps:\n" + "\n".join(f"{s.step_id}. {s.thought} ({s.tool})" for s in skill.steps)
                )
            if player and hasattr(player, "write_log"):
                player.write_log(f"✔ {msg}")

            res_dict = {
                "success": True,
                "skill_id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "steps": [s.to_dict() for s in skill.steps],
                "parameters": [p.to_dict() for p in skill.parameters],
                "validation": val_report,
                "message": msg,
            }
            if dup_info:
                res_dict["duplicate_info"] = dup_info
            return res_dict
        except Exception as e:
            if is_quota_error(e):
                cooldown.set_cooldown(60.0)
                cooldown.enqueue("learn_from_description", {"name": name, "description": description})
                msg = cooldown.notify_user_cooldown(player=player)
                return {"success": False, "queued": True, "cooldown_seconds": 60.0, "message": msg}
            logger.error("Error generating skill from description: %s", e, exc_info=True)
            return {"success": False, "message": f"Failed to generate skill from description: {e}"}

    def test_skill(
        self,
        skill_id: str,
        test_parameters: Optional[dict] = None,
        player=None,
    ) -> ActionResult:
        """Dry-test or test-run a learned skill before final approval."""
        skill = self.registry.get_skill(skill_id)
        if not skill:
            return result_failed(action=f"test_skill:{skill_id}", message=f"Skill '{skill_id}' not found.")

        if player and hasattr(player, "write_log"):
            player.write_log(f"🧪 Testing skill '{skill.name}'...")

        params = dict(test_parameters or {})
        params.setdefault("__user_confirmed__", True)

        # Execute in test mode (allow_unapproved=True)
        result = self.executor.execute(
            skill=skill,
            parameters=params,
            player=player,
            allow_unapproved=True,
        )

        if result.is_success():
            # Mark validated in storage
            update_skill_status(skill.id, SkillStatus.VALIDATED)
            skill.status = SkillStatus.VALIDATED.value
            self.registry.save_and_register(skill)

        return result

    def approve_skill(self, skill_id: str, player=None) -> ActionResult:
        """User approval gate: activate skill for autonomous usage."""
        skill = self.registry.get_skill(skill_id)
        if not skill:
            return result_failed(action=f"approve_skill:{skill_id}", message=f"Skill '{skill_id}' not found.")

        update_skill_status(skill.id, SkillStatus.APPROVED)
        skill.status = SkillStatus.APPROVED.value
        self.registry.save_and_register(skill)

        msg = f"Skill '{skill.name}' ({skill.id}) is now APPROVED and available for autonomous use."
        if player and hasattr(player, "write_log"):
            player.write_log(f"★ {msg}")

        logger.info(msg)
        return result_done(
            action=f"approve_skill:{skill_id}",
            message=msg,
            data={"skill_id": skill.id, "status": skill.status},
        )

    def show_skill(self, skill_id_or_name: str) -> Optional[dict]:
        """Inspect and return detailed representation of a skill."""
        skill = self.registry.get_skill(skill_id_or_name)
        if not skill:
            skill = load_skill(skill_id_or_name)
        if not skill:
            matched, _, _ = self.registry.find_matching_skill(skill_id_or_name, threshold=0.3)
            if matched:
                skill = matched
        if not skill:
            return None
        return skill.to_dict()

    def rename_skill(self, skill_id_or_name: str, new_name: str) -> ActionResult:
        """Rename an existing skill."""
        updated = storage_rename_skill(skill_id_or_name, new_name)
        if updated:
            self.registry.save_and_register(updated)
            return result_done(action=f"rename_skill:{updated.id}", message=f"Skill '{updated.id}' renamed to '{new_name}'.", data=updated.to_dict())
        return result_failed(action=f"rename_skill:{skill_id_or_name}", message=f"Skill '{skill_id_or_name}' not found.")

    def edit_skill(self, skill_id_or_name: str, field_name: str, new_value: Any) -> ActionResult:
        """Edit a parameter, description, or property of an existing skill."""
        updated = storage_edit_skill(skill_id_or_name, field_name, new_value)
        if updated:
            self.registry.save_and_register(updated)
            return result_done(action=f"edit_skill:{updated.id}", message=f"Skill '{updated.id}' updated {field_name}={new_value}.", data=updated.to_dict())
        return result_failed(action=f"edit_skill:{skill_id_or_name}", message=f"Skill '{skill_id_or_name}' not found.")

    def disable_skill(self, skill_id_or_name: str) -> ActionResult:
        """Disable a skill from autonomous usage."""
        skill = self.registry.get_skill(skill_id_or_name) or load_skill(skill_id_or_name)
        if not skill:
            return result_failed(action=f"disable_skill:{skill_id_or_name}", message=f"Skill '{skill_id_or_name}' not found.")
        skill.status = SkillStatus.DISABLED.value
        save_skill(skill)
        self.registry.save_and_register(skill)
        return result_done(action=f"disable_skill:{skill.id}", message=f"Skill '{skill.name}' ({skill.id}) is now disabled.", data={"status": skill.status})

    def restore_skill(self, skill_id_or_name: str) -> ActionResult:
        """Restore a disabled or deleted skill."""
        restored = storage_restore_skill(skill_id_or_name)
        if restored:
            self.registry.save_and_register(restored)
            return result_done(action=f"restore_skill:{restored.id}", message=f"Skill '{restored.name}' ({restored.id}) restored and approved.", data=restored.to_dict())
        return result_failed(action=f"restore_skill:{skill_id_or_name}", message=f"Skill '{skill_id_or_name}' could not be restored.")

    def delete_skill(self, skill_id: str) -> ActionResult:
        """Permanently remove a learned skill."""
        success = self.registry.remove_skill(skill_id)
        if success:
            return result_done(action=f"delete_skill:{skill_id}", message=f"Skill '{skill_id}' deleted.")
        return result_failed(
            action=f"delete_skill:{skill_id}", message=f"Skill '{skill_id}' could not be deleted or not found."
        )

    def execute_skill(
        self,
        skill_id: str,
        parameters: Optional[dict] = None,
        player=None,
        session_memory=None,
        utterance: str = "",
    ) -> ActionResult:
        """Run an approved skill with automatic semantic matching and session fallback."""
        skill = self.registry.get_skill(skill_id)
        if not skill:
            # 1. Try semantic matching on requested skill_id or query
            matched, score, _ = self.registry.find_matching_skill(skill_id, threshold=0.25)
            if matched:
                skill = matched
                logger.info("Matched skill '%s' for requested '%s' (score: %.2f)", skill.id, skill_id, score)
            # 2. Try the current or most recent demonstration session
            elif self.current_session and self.current_session.generated_skill:
                skill = self.current_session.generated_skill
                logger.info("Using session skill '%s' for requested '%s'", skill.id, skill_id)
            else:
                all_skills = self.registry.get_all_skills(approved_only=False)
                if all_skills:
                    skill = all_skills[-1]
                    logger.info("Falling back to most recently registered skill '%s'", skill.id)

        if not skill:
            return result_failed(action=f"execute_skill:{skill_id}", message=f"Skill '{skill_id}' not found.")

        # Self-improvement: check if skill failed 3 consecutive times
        if skill.needs_relearning and not (parameters or {}).get("force") and not (parameters or {}).get("__user_confirmed__"):
            msg = f"Skill '{skill.name}' has failed {skill.consecutive_failures} consecutive times and needs re-learning. Would you like me to re-learn it?"
            if player and hasattr(player, "write_log"):
                player.write_log(f"⚠️ {msg}")
            return result_failed(
                action=f"execute_skill:{skill.id}",
                message=msg,
                error="NeedsRelearning",
                data={"skill_id": skill.id, "needs_relearning": True, "consecutive_failures": skill.consecutive_failures},
            )

        res = self.executor.execute(
            skill=skill,
            parameters=parameters,
            player=player,
            session_memory=session_memory,
            allow_unapproved=True,
            utterance=utterance,
        )

        # Update execution history and stats
        if res.is_success():
            skill.usage_count += 1
            skill.success_count += 1
            skill.consecutive_failures = 0
            skill.needs_relearning = False
        else:
            skill.usage_count += 1
            skill.failure_count += 1
            skill.consecutive_failures += 1
            if skill.consecutive_failures >= 3:
                skill.needs_relearning = True
            step_fail = res.data.get("failed_step") if isinstance(res.data, dict) else None
            skill.failure_history.append({
                "timestamp": datetime.now().isoformat(),
                "error": str(res.message or res.error or "Execution error"),
                "step": step_fail,
            })
            skill.failure_history = skill.failure_history[-10:]

        skill.last_used_at = datetime.now().isoformat()
        save_skill(skill)
        self.registry.save_and_register(skill)

        return res


def get_default_learning_manager(action_registry=None) -> LearningManager:
    return LearningManager.get_instance(action_registry=action_registry)
