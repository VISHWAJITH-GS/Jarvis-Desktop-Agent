"""
actions/action_inspector.py — Transparency, Audit Inspection & Dry-Run Preview.

Consolidates:
  - action_history / action_detail / action_summary (FR-TR1, FR-TR2, FR-TR3)
  - dry_run / tool_inspect (FR-TR4, FR-TR5)

Allows JARVIS to inspect what happened in previous tool runs, inspect
registered tool definitions and schemas, and dry-run/preview an action before
it executes.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.action_loader import get_default_registry
from core.outcome import ActionResult
from core.permissions import (
    get_audit_entry_by_id,
    get_audit_summary,
    query_audit_log,
)
from core.risk_tiers import get_action_tier
from core.schema_utils import validate_parameters


def action_inspector(
    parameters: Optional[Dict[str, Any]] = None,
    player: Any = None,
    session_memory: Any = None,
) -> ActionResult:
    params = parameters or {}
    action = str(params.get("action") or "history").strip().lower()
    tool_name = str(params.get("tool_name") or "").strip()
    status_filter = str(params.get("status") or "").strip()
    entry_id = params.get("id")
    limit = int(params.get("limit") or 20)
    dry_params = params.get("parameters") or {}
    if not isinstance(dry_params, dict):
        dry_params = {}

    # ── 1. History ───────────────────────────────────────────────────────────
    if action in ("history", "action_history", "list"):
        entries = query_audit_log(
            action=tool_name or None,
            status=status_filter or None,
            limit=limit,
        )
        msg = f"Retrieved {len(entries)} action audit records."
        if tool_name:
            msg += f" Filter: tool='{tool_name}'."
        if status_filter:
            msg += f" Status='{status_filter}'."
        return ActionResult.done(
            "action_inspector",
            message=msg,
            data={"entries": entries, "count": len(entries)},
        )

    # ── 2. Detail ────────────────────────────────────────────────────────────
    elif action in ("detail", "action_detail", "get"):
        entry: Optional[Dict[str, Any]] = None
        if entry_id is None:
            # Fall back to latest entry
            recent = query_audit_log(limit=1)
            if not recent:
                return ActionResult.failed(
                    "action_inspector",
                    error="No audit log entries found to inspect.",
                )
            entry = recent[0]
        else:
            entry = get_audit_entry_by_id(int(entry_id))
            if not entry:
                return ActionResult.failed(
                    "action_inspector",
                    error=f"No audit entry found matching ID {entry_id}.",
                )
        return ActionResult.done(
            "action_inspector",
            message=f"Audit detail for action '{entry.get('action')}' (ID {entry.get('id')}).",
            data={"entry": entry},
        )

    # ── 3. Summary ───────────────────────────────────────────────────────────
    elif action in ("summary", "action_summary", "stats"):
        summary = get_audit_summary()
        tot = summary.get("total_actions", 0)
        vf = summary.get("verified_count", 0)
        fl = summary.get("failed_count", 0)
        msg = f"Audit summary: {tot} total actions executed ({vf} verified, {fl} failed/blocked)."
        return ActionResult.done(
            "action_inspector",
            message=msg,
            data=summary,
        )

    # ── 4. Dry-run preview ───────────────────────────────────────────────────
    elif action in ("dry_run", "preview", "simulate"):
        if not tool_name:
            return ActionResult.failed(
                "action_inspector",
                error="Parameter 'tool_name' is required for dry_run preview.",
            )

        reg = get_default_registry()
        act_rec = reg.get_action_record(tool_name) if reg else None

        if not act_rec:
            return ActionResult.failed(
                "action_inspector",
                error=f"Tool or action '{tool_name}' is not registered.",
            )

        # Validate arguments against schema
        val_ok, val_err = validate_parameters(act_rec.parameters or {}, dry_params)

        # Compute tier & risk
        risk_tier = get_action_tier(tool_name, dry_params)
        from core.permissions import POLICY_ASK, get_permission_registry
        perm_reg = get_permission_registry()
        perm_entry = perm_reg.find_by_action(tool_name, dry_params)
        policy_str = perm_entry.default_policy if perm_entry else "allow"

        # Determine confirmation requirement
        from core.risk_tiers import RiskTier
        needs_confirm = (
            risk_tier in (RiskTier.HIGH, RiskTier.MEDIUM)
            or policy_str == POLICY_ASK
            or getattr(act_rec, "permission", "") in ("sensitive", "destructive")
        )

        preview_data = {
            "tool": tool_name,
            "tier": str(act_rec.tier or "core"),
            "risk_tier": str(risk_tier.value),
            "permission_policy": policy_str,
            "requires_confirmation": needs_confirm,
            "valid_arguments": val_ok,
            "validation_error": val_err if not val_ok else None,
            "will_execute_without_prompt": val_ok and not needs_confirm,
        }

        verdict_str = "would require confirmation" if needs_confirm else "would run autonomously"
        valid_str = "valid arguments" if val_ok else f"invalid arguments: {val_err}"
        msg = f"Dry-run for '{tool_name}': {risk_tier.value.upper()} tier, {verdict_str} ({valid_str})."

        return ActionResult.done(
            "action_inspector",
            message=msg,
            data=preview_data,
        )

    # ── 5. Tool inspection ───────────────────────────────────────────────────
    elif action in ("inspect", "tool_inspect", "tools"):
        reg = get_default_registry()
        if not reg:
            return ActionResult.failed("action_inspector", error="Action registry is not initialized.")

        if tool_name:
            rec = reg.get_action_record(tool_name)
            if not rec:
                return ActionResult.failed(
                    "action_inspector",
                    error=f"Tool '{tool_name}' not found.",
                )
            data = {
                "name": rec.name,
                "description": rec.description,
                "brief": rec.brief or "",
                "tier": rec.tier or "core",
                "permission": rec.permission,
                "parameters": rec.parameters,
                "file": rec.file,
            }
            return ActionResult.done(
                "action_inspector",
                message=f"Inspection details for tool '{rec.name}'.",
                data=data,
            )

        # List all tools with tier & brief
        tools_list = []
        for name in sorted(reg.list_actions()):
            r = reg.get_action_record(name)
            if r:
                tools_list.append({
                    "name": r.name,
                    "tier": r.tier or "core",
                    "brief": r.brief or (r.description[:80] + "..." if len(r.description) > 80 else r.description),
                    "permission": r.permission,
                })

        msg = f"Discovered {len(tools_list)} registered tools in catalog."
        return ActionResult.done(
            "action_inspector",
            message=msg,
            data={"tools": tools_list, "total": len(tools_list)},
        )

    else:
        return ActionResult.unsupported(
            "action_inspector",
            f"Unsupported inspection action '{action}'. Use 'history', 'detail', 'summary', 'dry_run', or 'inspect'.",
        )


TOOL = {
    "name": "action_inspector",
    "description": (
        "Inspects action execution history, provides dry-run simulation of tools before execution, "
        "summarizes audit stats, and inspects registered tool schemas and tiers."
    ),
    "brief": "Query audit history, preview dry-run, or inspect tools",
    "tier": "core",
    "permission": "read_only",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Inspection action: 'history', 'detail', 'summary', 'dry_run', or 'inspect'",
                "enum": ["history", "detail", "summary", "dry_run", "inspect"],
            },
            "tool_name": {
                "type": "STRING",
                "description": "Tool or action name to filter by, dry-run, or inspect",
            },
            "status": {
                "type": "STRING",
                "description": "Filter history by execution status (e.g. 'done', 'failed', 'blocked', 'needs_confirmation')",
            },
            "id": {
                "type": "INTEGER",
                "description": "Audit entry ID for 'detail'",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Maximum number of audit entries to return (default 20)",
            },
            "parameters": {
                "type": "OBJECT",
                "description": "Parameters to test in 'dry_run' mode",
            },
        },
        "required": ["action"],
    },
    "handler": action_inspector,
}
