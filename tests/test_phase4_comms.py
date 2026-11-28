import json
from unittest.mock import MagicMock, patch

from core import outcome
from plugins.email_manager import (
    PLUGIN as EMAIL_PLUGIN,
)
from plugins.email_manager import (
    email_list,
    email_read,
    email_send,
)
from plugins.email_manager import (
    run as email_run,
)
from plugins.telegram_manager import (
    PLUGIN as TELEGRAM_PLUGIN,
)
from plugins.telegram_manager import (
    run as telegram_run,
)
from plugins.telegram_manager import (
    telegram_send,
    telegram_status,
)
from plugins.weather_travel import (
    PLUGIN as WEATHER_PLUGIN,
)
from plugins.weather_travel import (
    flight_open,
    flight_search,
    weather_get,
)


def _parse_res(raw: str) -> dict:
    nr = outcome.normalize_result(raw, "action")
    return {
        "status": nr.status,
        "success": nr.success,
        "raw": raw,
    }


def test_email_manager_declaration_and_gating():
    assert EMAIL_PLUGIN["name"] == "email_manager"
    props = EMAIL_PLUGIN["parameters"]["properties"]
    assert "to" in props
    assert "subject" in props
    assert "body" in props
    assert "confirmed" in props

    # email_send unconfirmed must ask for confirmation
    res_ask = email_send("recipient@example.com", "Project Status", "Everything is on schedule.", confirmed=False)
    assert "status=ask" in res_ask or "Are you sure" in res_ask

    # email_list unconfigured returns clean outcome failure (never raises)
    res_list = email_list()
    assert _parse_res(res_list)["status"] in ("failed", "done")

    # email_read missing msg_id
    res_read = email_read("")
    assert _parse_res(res_read)["status"] == "failed"

    # dispatcher test
    res_disp = email_run({"action": "unknown_action_xyz"})
    assert _parse_res(res_disp)["status"] == "failed"


def test_telegram_manager_declaration_and_operations():
    assert TELEGRAM_PLUGIN["name"] == "telegram_manager"
    props = TELEGRAM_PLUGIN["parameters"]["properties"]
    assert "action" in props
    assert "message" in props

    # telegram_status without token returns clean status
    res_status = telegram_status()
    assert "telegram_status" in res_status

    # telegram_send missing message
    res_send_no_msg = telegram_send("")
    assert _parse_res(res_send_no_msg)["status"] == "failed"

    # Mocked successful Telegram send
    with patch("plugins.telegram_manager._call_tg_api", return_value=(True, {"result": {"message_id": 42}})):
        with patch("plugins.telegram_manager._get_telegram_config", return_value=("dummy_token", "123456")):
            res_mock = telegram_send("Test alert from JARVIS")
            assert _parse_res(res_mock)["status"] == "done"
            assert "message_id=42" in res_mock

    # dispatcher test
    res_disp = telegram_run({"action": "telegram_status"})
    assert "telegram_status" in res_disp


def test_weather_travel_declaration_and_flight_search():
    assert WEATHER_PLUGIN["name"] == "weather_travel"
    props = WEATHER_PLUGIN["parameters"]["properties"]
    assert "city" in props
    assert "origin" in props
    assert "destination" in props

    # Flight search
    res_flight = flight_search("JFK", "LHR", date="2026-12-01", cabin="business")
    assert _parse_res(res_flight)["status"] == "done"
    assert "google_flights_url" in res_flight
    assert "skyscanner_url" in res_flight

    # Flight open validation
    res_bad_url = flight_open("ftp://invalid-url")
    assert _parse_res(res_bad_url)["status"] == "failed"

    # Weather get (with mock to avoid network flaky tests)
    mock_weather_data = {
        "current": {"temperature_2m": 18.5, "weather_code": 1, "relative_humidity_2m": 60, "wind_speed_10m": 12.0},
        "daily": {"time": ["2026-11-28"], "temperature_2m_max": [20.0], "temperature_2m_min": [14.0], "weather_code": [1]},
    }
    with patch("plugins.weather_travel._geocode_city", return_value=(51.5, -0.12, "London, United Kingdom")):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_weather_data).encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            res_weather = weather_get("London")
            assert _parse_res(res_weather)["status"] == "done"
            assert "temperature_c=18.5" in res_weather
