from unittest.mock import MagicMock, patch

from actions.deep_research import (
    TOOL as RESEARCH_TOOL,
)
from actions.deep_research import (
    deep_research,
    deep_research_handler,
    research_status,
)
from actions.workflow_recorder import (
    TOOL as RECORDER_TOOL,
)
from actions.workflow_recorder import (
    macro_delete,
    macro_list,
    macro_play,
    macro_save,
)
from core import outcome


def _parse_res(raw: str) -> dict:
    nr = outcome.normalize_result(raw, "action")
    return {
        "status": nr.status,
        "success": nr.success,
        "raw": raw,
    }


def test_workflow_recorder_lifecycle():
    assert RECORDER_TOOL["name"] == "workflow_recorder"
    props = RECORDER_TOOL["parameters"]["properties"]
    assert "action" in props
    assert "name" in props
    assert "steps" in props

    steps = [
        {"type": "click", "x": 100, "y": 200},
        {"type": "text", "text": "unit_test"},
        {"type": "hotkey", "keys": "ctrl+c"},
    ]

    # Save
    res_save = macro_save("test_macro_1", steps, description="Unit test macro")
    assert _parse_res(res_save)["status"] == "done"
    assert "test_macro_1" in res_save

    # List
    res_list = macro_list()
    assert _parse_res(res_list)["status"] == "done"
    assert "test_macro_1" in res_list

    # Play with mocked pyautogui
    with patch("actions.workflow_recorder.pyautogui") as mock_pg:
        mock_pg.click = MagicMock()
        mock_pg.typewrite = MagicMock()
        mock_pg.hotkey = MagicMock()

        res_play = macro_play("test_macro_1", speed=2.0)
        assert _parse_res(res_play)["status"] == "done"
        assert mock_pg.click.called
        assert mock_pg.typewrite.called
        assert mock_pg.hotkey.called

    # Delete
    res_del = macro_delete("test_macro_1")
    assert _parse_res(res_del)["status"] == "done"


def test_deep_research_pipeline_and_status():
    assert RESEARCH_TOOL["name"] == "deep_research"
    props = RESEARCH_TOOL["parameters"]["properties"]
    assert "topic" in props

    mock_snippets = [
        {"title": "Quantum Supremacy", "snippet": "A milestone in quantum computing demonstrating speedups.", "url": "https://example.com/quantum"},
        {"title": "Qubit Fidelity", "snippet": "Error rates and coherence times in superconducting circuits.", "url": "https://example.com/fidelity"},
    ]

    with patch("actions.deep_research._fetch_source_snippets", return_value=mock_snippets):
        res_research = deep_research("Quantum Computing Advancements", depth="standard")
        assert _parse_res(res_research)["status"] == "done"
        assert "sources_count=2" in res_research

        # Check created file
        status_res = research_status()
        assert _parse_res(status_res)["status"] == "done"
        assert "Quantum Computing Advancements" in status_res

        # Test handler dispatch
        res_h = deep_research_handler({"action": "research_status"})
        assert _parse_res(res_h)["status"] == "done"
