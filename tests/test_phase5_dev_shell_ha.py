import tempfile
from pathlib import Path
from unittest.mock import patch

from actions.dev_tools import (
    TOOL as DEV_TOOL,
)
from actions.dev_tools import (
    codereview_diff,
    codereview_log,
    project_analyze,
    project_create,
)
from actions.sandboxed_shell import (
    TOOL as SHELL_TOOL,
)
from actions.sandboxed_shell import (
    _is_destructive,
    run_shell_command,
)
from core import outcome
from plugins.home_assistant import (
    PLUGIN as HA_PLUGIN,
)
from plugins.home_assistant import (
    ha_call_service,
    ha_list_devices,
)


def _parse_res(raw: str) -> dict:
    nr = outcome.normalize_result(raw, "action")
    return {
        "status": nr.status,
        "success": nr.success,
        "raw": raw,
    }


def test_dev_tools_git_and_scaffolding():
    assert DEV_TOOL["name"] == "dev_tools"
    props = DEV_TOOL["parameters"]["properties"]
    assert "action" in props
    assert "file_path" in props

    # Git log on active repo
    res_log = codereview_log(limit=3)
    assert _parse_res(res_log)["status"] == "done"
    assert "codereview_log" in res_log

    # Git diff on active repo
    res_diff = codereview_diff()
    assert _parse_res(res_diff)["status"] == "done"

    # Project analyze on current repo
    res_analyze = project_analyze()
    assert _parse_res(res_analyze)["status"] == "done"
    assert "python" in res_analyze

    # Project scaffold in temporary directory
    with tempfile.TemporaryDirectory() as tmpdir:
        res_create = project_create("fastapi", "test_api_app", target_dir=tmpdir)
        assert _parse_res(res_create)["status"] == "done"
        created_path = Path(tmpdir) / "test_api_app"
        assert (created_path / "src" / "main.py").exists()
        assert (created_path / "requirements.txt").exists()


def test_sandboxed_shell_execution_and_safety():
    assert SHELL_TOOL["name"] == "sandboxed_shell"
    props = SHELL_TOOL["parameters"]["properties"]
    assert "command" in props
    assert "timeout_sec" in props

    # Safe command execution
    res_cmd = run_shell_command("python -c \"print('test_shell_ok')\"")
    assert _parse_res(res_cmd)["status"] == "done"
    assert "test_shell_ok" in res_cmd

    # Destructive pattern detection
    assert _is_destructive("rm -rf /")
    assert _is_destructive("del /f /s /q C:\\*")
    assert _is_destructive("format D:")
    assert not _is_destructive("dir")
    assert not _is_destructive("pytest -v")

    # Destructive command without confirmation returns ask
    res_dest = run_shell_command("rm -rf /tmp/data", confirmed=False)
    assert "status=ask" in res_dest or "destructive" in res_dest


def test_home_assistant_operations():
    assert HA_PLUGIN["name"] == "home_assistant"
    props = HA_PLUGIN["parameters"]["properties"]
    assert "action" in props
    assert "domain" in props
    assert "service" in props

    # Unconfigured HA returns failure
    with patch("plugins.home_assistant._get_ha_config", return_value=("http://localhost:8123", None)):
        res_list = ha_list_devices()
        assert _parse_res(res_list)["status"] == "failed"

    # Mocked service call
    with patch("plugins.home_assistant._call_ha_api", return_value=(True, [{"entity_id": "light.living_room"}])):
        with patch("plugins.home_assistant._get_ha_config", return_value=("http://localhost:8123", "fake_token")):
            res_svc = ha_call_service("light", "turn_on", entity_id="light.living_room")
            assert _parse_res(res_svc)["status"] == "done"
            assert "verified=true" in res_svc
