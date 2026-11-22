"""
tests/test_lazy_action_loader.py — Tests for zero-import lazy action loading.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from core.action_loader import ActionRecord, ActionRegistry, LazyActionHandler, discover_actions
from core import outcome

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_lazy_action_discovery_imports_no_action_modules():
    """Verify that discovering actions does not eagerly import action modules."""
    check_mods = [
        "actions.computer_control",
        "actions.screen_processor",
        "actions.screen_analysis",
        "actions.browser_control",
        "actions.desktop",
    ]
    saved = {m: sys.modules.pop(m) for m in check_mods if m in sys.modules}
    try:
        # Discover actions
        actions_dir = REPO_ROOT / "actions"
        registry = discover_actions(actions_dir, logger=lambda _: None)

        # Actions discovered
        decls = registry.get_tool_declarations()
        assert len(decls) >= 49

        # Verify that heavy action modules like computer_control, screen_processor,
        # browser_control are NOT loaded into sys.modules
        for mod_name in check_mods:
            assert mod_name not in sys.modules, f"{mod_name} was eagerly imported at discovery!"
    finally:
        sys.modules.update(saved)


def test_lazy_action_declarations_complete_and_valid():
    """Verify all declarations have required fields and match expected schema."""
    registry = discover_actions(REPO_ROOT / "actions", logger=lambda _: None)
    decls = registry.get_tool_declarations()
    assert len(decls) >= 49
    for d in decls:
        assert "name" in d and isinstance(d["name"], str)
        assert "description" in d and isinstance(d["description"], str)
        assert "parameters" in d and isinstance(d["parameters"], dict)
        assert d["parameters"].get("type") == "OBJECT"


def test_lazy_action_execution_and_caching():
    """Verify that invoking an action loads its handler and caches it."""
    registry = discover_actions(REPO_ROOT / "actions", logger=lambda _: None)
    rec = registry.get_action_record("web_search")
    assert rec is not None
    assert isinstance(rec.handler, LazyActionHandler)

    # Calling execute resolves and caches handler
    res = registry.execute("web_search", {"query": "python"})
    assert isinstance(res, outcome.ActionResult)
    assert not isinstance(rec.handler, LazyActionHandler)
    assert callable(rec.handler)


def test_failed_lazy_action_returns_clean_error():
    """Verify that a broken lazy import returns ActionResult.failed, never crashes."""
    registry = discover_actions(REPO_ROOT / "actions", logger=lambda _: None)
    broken_handler = LazyActionHandler(
        action_name="broken_tool",
        module_name="actions.non_existent_module_xyz",
        func_name="missing_func",
        file_name="broken_tool.py",
    )
    broken_rec = ActionRecord(
        name="broken_tool",
        description="A broken test tool",
        handler=broken_handler,
        valid=True,
    )
    registry._actions["broken_tool"] = broken_rec

    res = registry.execute("broken_tool", {})
    assert res.status == outcome.STATUS_FAILED
    assert not res.success
    assert "Failed to load action 'broken_tool'" in res.message
