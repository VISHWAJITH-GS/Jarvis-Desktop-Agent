import re
import time

from actions.timers_stopwatch import (
    TOOL as TIMERS_TOOL,
)
from actions.timers_stopwatch import (
    stopwatch_lap,
    stopwatch_reset,
    stopwatch_start,
    stopwatch_status,
    stopwatch_stop,
    timer_cancel,
    timer_list,
    timer_set,
    timers_stopwatch,
)
from actions.windows_uia import (
    TOOL as UIA_TOOL,
)
from actions.windows_uia import (
    press_hotkey,
    windows_uia,
)
from core import outcome


def _parse_outcome(raw: str) -> dict:
    nr = outcome.normalize_result(raw, "action")
    kv = dict((m[0], m[1] or m[2]) for m in re.findall(r'(\w+)=(?:"([^"]*)"|(\S+))', raw))
    return {
        "status": nr.status,
        "success": nr.success,
        "kv": kv,
        "raw": raw,
    }


def test_timers_stopwatch_tool_declaration():
    assert TIMERS_TOOL["name"] == "timers_stopwatch"
    assert "timer_set" in TIMERS_TOOL["description"]
    assert "action" in TIMERS_TOOL["parameters"]["properties"]
    assert "duration_seconds" in TIMERS_TOOL["parameters"]["properties"]


def test_timer_lifecycle():
    # Set a short timer
    res_set = timer_set(duration_seconds=0.5, label="Test Timer")
    parsed_set = _parse_outcome(res_set)
    assert parsed_set["status"] == "done"
    timer_id = parsed_set["kv"].get("timer_id")
    assert timer_id is not None
    assert parsed_set["kv"].get("label") == "Test Timer"

    # List timers
    res_list = timer_list()
    parsed_list = _parse_outcome(res_list)
    assert parsed_list["status"] == "done"
    assert timer_id in res_list

    # Cancel timer
    res_cancel = timer_cancel(timer_id)
    parsed_cancel = _parse_outcome(res_cancel)
    assert parsed_cancel["status"] == "done"
    assert parsed_cancel["kv"].get("timer_id") == timer_id


def test_stopwatch_lifecycle():
    label = "unit_test_sw"
    # Reset first to ensure clean state
    stopwatch_reset(label)

    # Start
    res_start = stopwatch_start(label)
    parsed_start = _parse_outcome(res_start)
    assert parsed_start["status"] == "done"

    time.sleep(0.05)

    # Lap
    res_lap = stopwatch_lap(label)
    parsed_lap = _parse_outcome(res_lap)
    assert parsed_lap["status"] == "done"
    assert parsed_lap["kv"].get("lap") == "1"

    # Status
    res_status = stopwatch_status(label)
    parsed_status = _parse_outcome(res_status)
    assert parsed_status["status"] == "done"
    assert parsed_status["kv"].get("laps_count") == "1"

    # Stop
    res_stop = stopwatch_stop(label)
    parsed_stop = _parse_outcome(res_stop)
    assert parsed_stop["status"] == "done"

    # Reset
    res_reset = stopwatch_reset(label)
    parsed_reset = _parse_outcome(res_reset)
    assert parsed_reset["status"] == "done"
    assert parsed_reset["kv"].get("total_elapsed_sec") == "0.0"


def test_timers_stopwatch_dispatcher():
    # Dispatch timer_set via dispatcher
    res = timers_stopwatch({"action": "timer_set", "minutes": 1, "label": "Tea"})
    parsed = _parse_outcome(res)
    assert parsed["status"] == "done"
    assert parsed["kv"].get("duration_seconds") == "60.0"

    # Cancel it
    t_id = parsed["kv"].get("timer_id")
    res_c = timers_stopwatch({"action": "timer_cancel", "timer_id": t_id})
    assert _parse_outcome(res_c)["status"] == "done"

    # Unsupported action
    res_inv = timers_stopwatch({"action": "invalid_action_xyz"})
    assert _parse_outcome(res_inv)["status"] in ("unsupported", "failed")


def test_windows_uia_tool_declaration_and_helpers():
    assert UIA_TOOL["name"] == "windows_uia"
    props = UIA_TOOL["parameters"]["properties"]
    assert "keys" in props
    assert "title" in props
    assert "state" in props

    # Test list_windows
    res_list = windows_uia({"action": "list_windows"})
    parsed_list = _parse_outcome(res_list)
    assert parsed_list["status"] == "done"
    assert "total=" in res_list

    # Test press_hotkey helper function directly
    hotkey_res = press_hotkey("")
    assert hotkey_res.get("status") == "failed"

    # Test windows_uia dispatcher invalid action
    res_unknown = windows_uia({"action": "unsupported_action_99"})
    parsed_unknown = _parse_outcome(res_unknown)
    assert parsed_unknown["status"] == "failed"
