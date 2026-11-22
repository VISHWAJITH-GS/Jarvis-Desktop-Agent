import pytest
from unittest.mock import MagicMock

from gui.display_registry import (
    DisplayRegistry,
    DisplayEntry,
    register_display,
    unregister_display,
    list_displays,
    resolve_display,
    minimize_display,
    restore_display,
    close_display,
    close_all_displays,
)


@pytest.fixture(autouse=True)
def clean_registry():
    DisplayRegistry.clear()
    yield
    DisplayRegistry.clear()


def test_register_and_unregister():
    assert len(list_displays()) == 0
    w_mock = MagicMock()
    entry = register_display(1, title="Telemetry Monitor", kind="telemetry", qt_window_ref=w_mock)

    assert entry.display_id == 1
    assert entry.title == "Telemetry Monitor"
    assert DisplayRegistry.has_display(1)
    assert len(list_displays()) == 1

    unregister_display(1)
    assert not DisplayRegistry.has_display(1)
    assert len(list_displays()) == 0


def test_resolve_by_numeric_id():
    w1 = MagicMock()
    w2 = MagicMock()
    register_display(1, title="Telemetry", kind="telemetry", qt_window_ref=w1)
    register_display(2, title="Memory Explorer", kind="memory", qt_window_ref=w2)

    status, target, cands = resolve_display(1)
    assert status == "exact"
    assert target.display_id == 1

    status, target, cands = resolve_display("2")
    assert status == "exact"
    assert target.display_id == 2


def test_resolve_by_exact_and_fuzzy_title():
    w1 = MagicMock()
    w2 = MagicMock()
    register_display(1, title="Telemetry Monitor", kind="telemetry", qt_window_ref=w1)
    register_display(2, title="Memory Explorer", kind="memory", qt_window_ref=w2)

    # Exact
    status, target, cands = resolve_display("Telemetry Monitor")
    assert status == "exact"
    assert target.display_id == 1

    # Fuzzy
    status, target, cands = resolve_display("memory")
    assert status == "exact"
    assert target.display_id == 2


def test_resolve_single_open_display_fallback():
    w1 = MagicMock()
    register_display(1, title="Active Chart", kind="chart", qt_window_ref=w1)

    # When target is generic/None, single display resolves automatically
    status, target, cands = resolve_display(None)
    assert status == "exact"
    assert target.display_id == 1


def test_resolve_ambiguous_returns_ask_with_candidates():
    w1 = MagicMock()
    w2 = MagicMock()
    register_display(1, title="Project Alpha Notes", kind="notes", qt_window_ref=w1)
    register_display(2, title="Project Beta Notes", kind="notes", qt_window_ref=w2)

    status, target, cands = resolve_display("Project")
    assert status == "ask"
    assert len(cands) == 2


def test_minimize_and_restore():
    w = MagicMock()
    register_display(1, title="Log Viewer", kind="log", qt_window_ref=w)

    res_min = minimize_display(1)
    assert "minimized" in res_min.lower()
    entry = DisplayRegistry.get_display(1)
    assert entry.is_minimized is True

    res_rest = restore_display(1)
    assert "focus" in res_rest.lower() or "restored" in res_rest.lower()
    assert entry.is_minimized is False


def test_close_display_with_unsaved_content():
    w = MagicMock()
    register_display(1, title="Code Editor", kind="editor", qt_window_ref=w, has_unsaved_content=True)

    # Without force, refuse to close
    res = close_display(1, force=False)
    assert "unsaved" in res.lower()
    assert DisplayRegistry.has_display(1)  # not closed

    # With force, closes cleanly
    res_forced = close_display(1, force=True)
    assert "closed" in res_forced.lower()
    assert not DisplayRegistry.has_display(1)


def test_close_all_displays():
    w1 = MagicMock()
    w2 = MagicMock()
    register_display(1, title="Display 1", kind="view", qt_window_ref=w1)
    register_display(2, title="Display 2", kind="view", qt_window_ref=w2)

    res = close_all_displays()
    assert "closed all" in res.lower()
    assert len(list_displays()) == 0
