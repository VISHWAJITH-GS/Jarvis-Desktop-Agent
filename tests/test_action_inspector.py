from unittest.mock import patch

import pytest

from actions.action_inspector import TOOL, action_inspector
from core.outcome import ActionResult
from core.permissions import (
    clear_audit_trail,
    get_audit_entry_by_id,
    get_audit_summary,
    log_action_audit,
    query_audit_log,
)


@pytest.fixture(autouse=True)
def clean_audit_env(tmp_path):
    # Route audit DB to temporary path for isolated tests
    test_db = tmp_path / "test_audit.db"
    with patch("core.permissions._get_audit_db_path", return_value=test_db):
        clear_audit_trail(confirm=True)
        yield test_db
        clear_audit_trail(confirm=True)


def test_tool_declaration_structure():
    assert TOOL["name"] == "action_inspector"
    assert "parameters" in TOOL
    assert "handler" in TOOL
    assert TOOL.get("brief") is not None
    assert TOOL.get("tier") == "core"
    assert TOOL.get("permission") == "read_only"


def test_audit_logging_and_query():
    log_action_audit(
        action="open_app",
        parameters={"app_name": "notepad"},
        permission_level="normal",
        status="done",
        verified=True,
    )
    log_action_audit(
        action="windows_control",
        parameters={"action": "close_app", "app": "notepad"},
        permission_level="sensitive",
        status="done",
        verified=True,
    )
    log_action_audit(
        action="delete_file",
        parameters={"path": "C:\\secret.txt"},
        permission_level="destructive",
        status="blocked",
        verified=False,
        error="User denied confirmation",
    )

    # Query all
    all_logs = query_audit_log(limit=10)
    assert len(all_logs) == 3

    # Filter by action
    filtered_action = query_audit_log(action="open_app")
    assert len(filtered_action) == 1
    assert filtered_action[0]["action"] == "open_app"

    # Filter by status
    filtered_status = query_audit_log(status="blocked")
    assert len(filtered_status) == 1
    assert filtered_status[0]["status"] == "blocked"

    # Get entry by ID
    entry_id = all_logs[0]["id"]
    detail = get_audit_entry_by_id(entry_id)
    assert detail is not None
    assert detail["id"] == entry_id

    # Summary
    summary = get_audit_summary()
    assert summary["total_actions"] == 3
    assert summary["failed_count"] == 1
    assert summary["verified_count"] == 2


def test_action_inspector_history():
    log_action_audit(
        action="test_action",
        parameters={"key": "val"},
        permission_level="normal",
        status="done",
        verified=True,
    )

    res = action_inspector({"action": "history"})
    assert isinstance(res, ActionResult)
    assert res.status == "done"
    assert res.success is True
    assert len(res.data.get("entries", [])) >= 1


def test_action_inspector_detail():
    log_action_audit(
        action="specific_task",
        parameters={"data": 123},
        permission_level="normal",
        status="done",
        verified=True,
    )
    recent = query_audit_log(limit=1)
    entry_id = recent[0]["id"]

    res = action_inspector({"action": "detail", "id": entry_id})
    assert res.status == "done"
    assert res.data["entry"]["action"] == "specific_task"

    # Non-existent ID
    res_missing = action_inspector({"action": "detail", "id": 99999})
    assert res_missing.status == "failed"


def test_action_inspector_summary():
    log_action_audit("a1", {}, "normal", "done", True)
    log_action_audit("a2", {}, "sensitive", "failed", False, error="timeout")

    res = action_inspector({"action": "summary"})
    assert res.status == "done"
    data = res.data
    assert data["total_actions"] == 2
    assert data["verified_count"] == 1
    assert data["failed_count"] == 1


def test_action_inspector_dry_run_known_tool():
    res = action_inspector({
        "action": "dry_run",
        "tool_name": "open_app",
        "parameters": {"app_name": "spotify"},
    })
    assert res.status == "done"
    data = res.data
    assert data["tool"] == "open_app"
    assert data["valid_arguments"] is True
    assert "risk_tier" in data
    assert "requires_confirmation" in data


def test_action_inspector_dry_run_invalid_parameters():
    res = action_inspector({
        "action": "dry_run",
        "tool_name": "open_app",
        "parameters": {"unknown_param": 123},
    })
    assert res.status == "done"
    data = res.data
    assert data["tool"] == "open_app"
    assert data["valid_arguments"] is False
    assert data["validation_error"] is not None


def test_action_inspector_dry_run_unregistered_tool():
    res = action_inspector({
        "action": "dry_run",
        "tool_name": "non_existent_fake_tool_xyz",
    })
    assert res.status == "failed"
    assert "not registered" in res.error


def test_action_inspector_inspect_tools():
    # List all tools
    res_list = action_inspector({"action": "inspect"})
    assert res_list.status == "done"
    tools = res_list.data.get("tools", [])
    assert len(tools) > 10
    names = [t["name"] for t in tools]
    assert "open_app" in names
    assert "action_inspector" in names

    # Inspect single tool
    res_single = action_inspector({"action": "inspect", "tool_name": "action_inspector"})
    assert res_single.status == "done"
    assert res_single.data["name"] == "action_inspector"
    assert res_single.data["brief"] is not None
    assert "properties" in res_single.data["parameters"]


def test_action_inspector_unsupported_action():
    res = action_inspector({"action": "invalid_unknown_action"})
    assert res.status == "unsupported"
    assert res.success is False
