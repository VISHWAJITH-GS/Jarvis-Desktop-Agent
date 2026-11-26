"""
core/apps/tool_result.py — Standard ToolResult Specification for High-Level App Actions.

Shape:
{
    status: ok | pending_confirmation | needs_login | not_found | failed | uncertain,
    summary: str,
    data: dict,
    verified: bool,
    confirmation_token: str | None,
}
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from core import outcome

STATUS_OK = "ok"
STATUS_PENDING_CONFIRMATION = "pending_confirmation"
STATUS_NEEDS_LOGIN = "needs_login"
STATUS_NOT_FOUND = "not_found"
STATUS_FAILED = "failed"
STATUS_UNCERTAIN = "uncertain"

ALLOWED_STATUSES = {
    STATUS_OK,
    STATUS_PENDING_CONFIRMATION,
    STATUS_NEEDS_LOGIN,
    STATUS_NOT_FOUND,
    STATUS_FAILED,
    STATUS_UNCERTAIN,
}


@dataclass
class ToolResult:
    status: str
    summary: str
    data: Dict[str, Any] = field(default_factory=dict)
    verified: bool = False
    confirmation_token: Optional[str] = None
    error: Optional[str] = None

    def __post_init__(self):
        if self.status not in ALLOWED_STATUSES:
            self.status = STATUS_FAILED if not self.verified else STATUS_OK

    def is_success(self) -> bool:
        return self.status == STATUS_OK and self.verified

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "data": self.data,
            "verified": self.verified,
            "confirmation_token": self.confirmation_token,
            "error": self.error,
        }

    def to_speech_text(self) -> str:
        return self.summary

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    def to_action_result(self, action_name: str = "app_tool") -> outcome.ActionResult:
        status_map = {
            STATUS_OK: outcome.STATUS_DONE,
            STATUS_PENDING_CONFIRMATION: outcome.STATUS_NEEDS_CONFIRMATION,
            STATUS_NEEDS_LOGIN: outcome.STATUS_BLOCKED,
            STATUS_NOT_FOUND: outcome.STATUS_FAILED,
            STATUS_FAILED: outcome.STATUS_FAILED,
            STATUS_UNCERTAIN: outcome.STATUS_UNVERIFIED,
        }
        res_status = status_map.get(self.status, outcome.STATUS_FAILED)
        return outcome.ActionResult(
            status=res_status,
            action=action_name,
            success=self.is_success(),
            verified=self.verified,
            message=self.summary,
            error=self.error,
            data=self.data,
        )

    @classmethod
    def ok(cls, summary: str, data: Optional[Dict[str, Any]] = None, verified: bool = True) -> ToolResult:
        return cls(status=STATUS_OK, summary=summary, data=data or {}, verified=verified)

    @classmethod
    def pending_confirmation(
        cls, summary: str, confirmation_token: str, data: Optional[Dict[str, Any]] = None
    ) -> ToolResult:
        return cls(
            status=STATUS_PENDING_CONFIRMATION,
            summary=summary,
            data=data or {},
            verified=False,
            confirmation_token=confirmation_token,
        )

    @classmethod
    def needs_login(cls, summary: str, data: Optional[Dict[str, Any]] = None) -> ToolResult:
        return cls(status=STATUS_NEEDS_LOGIN, summary=summary, data=data or {}, verified=False)

    @classmethod
    def not_found(cls, summary: str, data: Optional[Dict[str, Any]] = None) -> ToolResult:
        return cls(status=STATUS_NOT_FOUND, summary=summary, data=data or {}, verified=False)

    @classmethod
    def failed(cls, summary: str, error: Optional[str] = None, data: Optional[Dict[str, Any]] = None) -> ToolResult:
        return cls(
            status=STATUS_FAILED, summary=summary, error=error or summary, data=data or {}, verified=False
        )

    @classmethod
    def uncertain(cls, summary: str, data: Optional[Dict[str, Any]] = None) -> ToolResult:
        return cls(status=STATUS_UNCERTAIN, summary=summary, data=data or {}, verified=False)
