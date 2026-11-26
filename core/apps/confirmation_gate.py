"""
core/apps/confirmation_gate.py — Immutable Human Confirmation Gate for App Tools.

Creates a pending token with a human-readable summary.
Resolves ONLY on an explicit yes from the user's voice/UI turn, never from the model.
No env var, flag or config may disable it.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from core import confirm
from core.apps.tool_result import ToolResult

logger = logging.getLogger("confirmation_gate")


class AppConfirmationGate:
    @classmethod
    def request_confirmation(
        cls,
        tool_name: str,
        action_summary: str,
        run_callable: Callable[[], ToolResult],
        parameters: Optional[Dict[str, Any]] = None,
        title: Optional[str] = None,
    ) -> ToolResult:
        """
        Parks an action behind the immutable user confirmation gate.
        Returns ToolResult.pending_confirmation.
        """
        params = dict(parameters or {})
        act_title = title or f"Execute {tool_name}"

        def _wrapped_run() -> Any:
            res = run_callable()
            if isinstance(res, ToolResult):
                return res.to_action_result(action_name=tool_name)
            return res

        pending = confirm.save_pending(
            tool=tool_name,
            params={**params, "_execution_authorized": True, "confirmed": True},
            run=_wrapped_run,
            expires_in=60.0,
            title=act_title,
            detail=action_summary,
        )

        return ToolResult.pending_confirmation(
            summary=action_summary,
            confirmation_token=pending.id,
            data={"pending_id": pending.id, "tool": tool_name, "params": params},
        )
