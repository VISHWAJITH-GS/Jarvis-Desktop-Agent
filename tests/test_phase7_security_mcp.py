import os
from unittest.mock import MagicMock, patch

from actions.security_privacy import (
    TOOL as SEC_TOOL,
)
from actions.security_privacy import (
    credential_check_password,
    credential_check_reuse,
    credential_generate,
    privacy_dns_lookup,
    privacy_listening_ports,
    security_kill,
    security_processes,
)
from core import outcome
from plugins.mcp_client import (
    PLUGIN as MCP_PLUGIN,
)
from plugins.mcp_client import (
    mcp_catalogue,
    mcp_status,
)
from plugins.mcp_client import (
    run as mcp_run,
)


def _parse_res(raw: str) -> dict:
    nr = outcome.normalize_result(raw, "action")
    return {
        "status": nr.status,
        "success": nr.success,
        "raw": raw,
    }


def test_security_privacy_operations():
    assert SEC_TOOL["name"] == "security_privacy"
    props = SEC_TOOL["parameters"]["properties"]
    assert "action" in props
    assert "password" in props
    assert "pid" in props

    # Process enumeration
    res_ps = security_processes(limit=5)
    assert _parse_res(res_ps)["status"] == "done"
    assert "processes=" in res_ps

    # Kill protection on self PID
    res_kill_self = security_kill(os.getpid())
    assert _parse_res(res_kill_self)["status"] == "failed"

    # Kill confirmation gating
    res_kill_unconf = security_kill(999999, confirmed=False)
    # 999999 does not exist or requires confirmation
    assert _parse_res(res_kill_unconf)["status"] in ("ask", "failed")

    # Listening ports audit
    res_ports = privacy_listening_ports(limit=5)
    assert _parse_res(res_ports)["status"] == "done"

    # DNS lookup
    res_dns = privacy_dns_lookup("localhost")
    assert _parse_res(res_dns)["status"] == "done"
    assert "127.0.0.1" in res_dns

    # Password strength check
    res_pwd_weak = credential_check_password("123456")
    assert "strength=weak" in res_pwd_weak
    assert "is_common_dict=true" in res_pwd_weak

    res_pwd_strong = credential_check_password("K8#mQ9$xL2!vP0@w")
    assert "strength=very_strong" in res_pwd_strong

    # Password generation
    res_gen = credential_generate(length=20, include_symbols=True)
    assert _parse_res(res_gen)["status"] == "done"
    assert "length=20" in res_gen

    # HIBP breach check (mocked)
    with patch("urllib.request.urlopen") as mock_url:
        mock_resp = MagicMock()
        # SHA1 of "password" is 5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8
        # Suffix is 1E4C9B93F3F0682250B6CF8331B7EE68FD8
        mock_resp.read.return_value = b"1E4C9B93F3F0682250B6CF8331B7EE68FD8:3861493\r\n"
        mock_url.return_value.__enter__.return_value = mock_resp

        res_pwned = credential_check_reuse("password")
        assert _parse_res(res_pwned)["status"] == "done"
        assert "pwned=true" in res_pwned


def test_mcp_client_operations():
    assert MCP_PLUGIN["name"] == "mcp_client"
    props = MCP_PLUGIN["parameters"]["properties"]
    assert "action" in props

    # Catalogue listing
    res_cat = mcp_catalogue()
    assert _parse_res(res_cat)["status"] == "done"
    assert "fetch" in res_cat
    assert "sqlite" in res_cat

    # Status check
    res_stat = mcp_status()
    assert _parse_res(res_stat)["status"] == "done"

    # Dispatcher call
    res_disp = mcp_run({"action": "mcp_catalogue"})
    assert _parse_res(res_disp)["status"] == "done"
