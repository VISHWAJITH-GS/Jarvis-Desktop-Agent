"""
core/task_approval.py — Task-level Approval Scope and Token Manager.

Enforces:
  1. One approval for a multi-step task (MEDIUM or HIGH):
     Sub-steps inside the approved scope run without further confirmation prompts.
  2. Scope boundaries:
     - allowed tools
     - target app / tab / window
     - target folder
     - expiry: 5 minutes (300 seconds)
     - step cap: default 25 steps
  3. Sensitive or out-of-scope actions:
     - Anything outside scope (different app, send message, file deletion, payments, pc shutdown)
       triggers a new confirmation request.
     - Protected destructive operations (delete, format, payment, shutdown) can NEVER be bypassed
       by a task approval scope.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class TaskApprovalScope:
    scope_id: str
    goal: str
    allowed_tools: Set[str] = field(default_factory=set)
    allowed_apps: Set[str] = field(default_factory=set)
    allowed_folders: List[Path] = field(default_factory=list)
    created_at: float = field(default_factory=time.monotonic)
    expires_in: float = 300.0  # 5 minutes
    step_cap: int = 25
    steps_executed: int = 0
    active: bool = True

    def is_expired(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.monotonic()
        return (t - self.created_at) > self.expires_in

    def deactivate(self) -> None:
        """Mark scope inactive."""
        self.active = False

    def is_step_cap_reached(self) -> bool:
        return self.steps_executed >= self.step_cap

    def is_valid(self) -> bool:
        return self.active and not self.is_expired() and not self.is_step_cap_reached()

    def matches_action(
        self,
        tool: str,
        params: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Check if an action falls cleanly within this approved scope."""
        if not self.is_valid():
            return False

        t = str(tool or "").strip().lower()
        p = dict(params or {})
        ctx = dict(context or {})
        action = str(p.get("action") or "").strip().lower()

        # 1. Protected destructive actions (delete, format, pay, shutdown) can NEVER be bypassed
        from core.risk_tiers import is_destructive_protected
        if is_destructive_protected(t, action):
            return False

        # 2. External messaging / communication / payments out of bounds
        if t in ("whatsapp_send", "send_message", "send_email", "make_payment") or action in ("send_message", "pay", "order"):
            return False

        # 3. Check allowed tools
        ui_primitives = {
            "computer_control",
            "windows_uia",
            "browser_control",
            "display_window",
            "action_executor",
            "click_element",
            "input_text",
            "scroll_page",
            "scroll_element",
            "press_key",
            "page_to_file",
            "copy_page_text_to_clipboard",
        }
        if self.allowed_tools and t not in self.allowed_tools:
            # Allow basic UI primitives if the task is an orchestrator/desktop/browser task
            compound_parents = {"takeover_desktop", "browser_control", "computer_control", "agent_orchestrator"}
            if not (t in ui_primitives and any(ut in self.allowed_tools for ut in compound_parents)):
                return False

        # 4. Check folder scope if file write
        if t in ("file_controller", "file_processor", "page_to_file"):
            target_path_str = str(
                p.get("path")
                or p.get("destination")
                or p.get("folder")
                or p.get("filepath")
                or p.get("filename")
                or ""
            )
            if target_path_str and self.allowed_folders:
                try:
                    tp = Path(target_path_str).expanduser().resolve()
                    if not any(tp == f.resolve() or tp.is_relative_to(f.resolve()) for f in self.allowed_folders):
                        return False
                except Exception:
                    return False

        # 5. Check app scope if targeting a specific app
        target_app = str(p.get("app_name") or p.get("target") or ctx.get("app_name") or "").lower()
        if target_app and self.allowed_apps:
            if not any(a.lower() in target_app or target_app in a.lower() for a in self.allowed_apps):
                return False

        return True


class TaskApprovalStore:
    """Thread-safe store for active task approval scopes."""

    _lock = threading.RLock()
    _active_scope: Optional[TaskApprovalScope] = None
    _scopes: Dict[str, TaskApprovalScope] = {}

    @classmethod
    def get_instance(cls):
        return cls

    @classmethod
    def get_all_active_scopes(cls) -> List[TaskApprovalScope]:
        with cls._lock:
            return [s for s in cls._scopes.values() if s.is_valid()]

    @classmethod
    def create_scope(
        cls,
        goal: str,
        allowed_tools: Optional[Set[str]] = None,
        allowed_apps: Optional[Set[str]] = None,
        allowed_folders: Optional[List[Path]] = None,
        expires_in: float = 300.0,
        step_cap: int = 25,
    ) -> TaskApprovalScope:
        scope_id = str(uuid.uuid4())[:8]
        tools = set(allowed_tools or [])
        apps = set(allowed_apps or [])
        folders = list(allowed_folders or [])
        scope = TaskApprovalScope(
            scope_id=scope_id,
            goal=goal,
            allowed_tools=tools,
            allowed_apps=apps,
            allowed_folders=folders,
            expires_in=expires_in,
            step_cap=step_cap,
        )
        with cls._lock:
            cls._scopes[scope_id] = scope
            cls._active_scope = scope
        logger.info(
            f"[TaskApproval] Created scope {scope_id} for goal: '{goal}' "
            f"(expires in {expires_in}s, cap={step_cap}, tools={tools})"
        )
        return scope

    @classmethod
    def get_active_scope(cls) -> Optional[TaskApprovalScope]:
        with cls._lock:
            if cls._active_scope and cls._active_scope.is_valid():
                return cls._active_scope
            if cls._active_scope and not cls._active_scope.is_valid():
                cls._active_scope.active = False
                cls._active_scope = None
            return None

    @classmethod
    def record_step(cls) -> None:
        with cls._lock:
            if cls._active_scope and cls._active_scope.is_valid():
                cls._active_scope.steps_executed += 1
                if cls._active_scope.is_step_cap_reached():
                    logger.info(
                        f"[TaskApproval] Scope {cls._active_scope.scope_id} reached step cap "
                        f"({cls._active_scope.step_cap})."
                    )
                    cls._active_scope.active = False
                    cls._active_scope = None

    @classmethod
    def clear_active_scope(cls) -> None:
        with cls._lock:
            if cls._active_scope:
                cls._active_scope.active = False
                cls._active_scope = None

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._active_scope = None
            cls._scopes.clear()
