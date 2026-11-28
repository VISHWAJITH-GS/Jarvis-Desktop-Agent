"""
plugins/home_assistant.py — Home Assistant Smart Home Integration for JARVIS.

Integrates with Home Assistant REST API to monitor and control smart home devices:
- ha_list_devices: List states and entities (lights, switches, sensors, climate).
- ha_get_state: Get current state and attributes of a specific entity.
- ha_call_service: Call service (turn on/off, toggle, set temperature, etc.).
- ha_trigger_scene: Activate a Home Assistant scene.

Configured via memory.config_manager.get_plugin_setting ('url' and 'token').
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Optional, Tuple

from core import outcome
from memory.config_manager import get_plugin_setting

logger = logging.getLogger("home_assistant")


def _get_ha_config() -> Tuple[Optional[str], Optional[str]]:
    url = get_plugin_setting("home_assistant", "url", "http://homeassistant.local:8123")
    token = get_plugin_setting("home_assistant", "token", None)
    return url, token


def _call_ha_api(endpoint: str, method: str = "GET", payload: Optional[dict] = None) -> Tuple[bool, Any]:
    url, token = _get_ha_config()
    if not token:
        return False, "Home Assistant token not configured. Set 'token' in home_assistant settings."

    clean_url = url.rstrip("/") + "/api/" + endpoint.lstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None

    try:
        req = urllib.request.Request(clean_url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return True, body
    except Exception as e:
        logger.error("Home Assistant API error on '%s': %s", endpoint, e)
        return False, str(e)


def ha_list_devices(domain: Optional[str] = None, limit: int = 20) -> str:
    ok, res = _call_ha_api("states")
    if not ok:
        return outcome.failed("ha_list_devices", f"Failed to list devices: {res}")

    entities = []
    if isinstance(res, list):
        for item in res:
            eid = item.get("entity_id", "")
            if domain and not eid.startswith(f"{domain}."):
                continue
            friendly_name = item.get("attributes", {}).get("friendly_name", eid)
            entities.append({
                "entity_id": eid,
                "name": friendly_name,
                "state": item.get("state"),
            })

    return outcome.done(
        "ha_list_devices",
        domain=domain,
        count=len(entities[:limit]),
        devices=entities[:limit],
    )


def ha_get_state(entity_id: str) -> str:
    if not entity_id:
        return outcome.failed("ha_get_state", "Missing entity_id.")

    ok, res = _call_ha_api(f"states/{entity_id}")
    if not ok:
        return outcome.failed("ha_get_state", f"Could not get state for '{entity_id}': {res}")

    if isinstance(res, dict):
        return outcome.done(
            "ha_get_state",
            entity_id=entity_id,
            state=res.get("state"),
            friendly_name=res.get("attributes", {}).get("friendly_name", entity_id),
            last_changed=res.get("last_changed"),
        )
    return outcome.failed("ha_get_state", "Invalid response from Home Assistant.")


def ha_call_service(
    domain: str,
    service: str,
    entity_id: Optional[str] = None,
    service_data: Optional[dict] = None,
) -> str:
    if not domain or not service:
        return outcome.failed("ha_call_service", "Missing domain or service name.")

    payload = dict(service_data or {})
    if entity_id:
        payload["entity_id"] = entity_id

    ok, res = _call_ha_api(f"services/{domain}/{service}", method="POST", payload=payload)
    if not ok:
        return outcome.failed("ha_call_service", f"Failed to call service {domain}.{service}: {res}")

    return outcome.done(
        "ha_call_service",
        domain=domain,
        service=service,
        entity_id=entity_id,
        verified=True,
    )


def ha_trigger_scene(scene: str) -> str:
    if not scene:
        return outcome.failed("ha_trigger_scene", "Missing scene name or entity_id.")

    entity_id = scene if scene.startswith("scene.") else f"scene.{scene.lower().replace(' ', '_')}"
    return ha_call_service("scene", "turn_on", entity_id=entity_id)


def run(parameters: dict, player: Any = None, session_memory: Any = None) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).lower().strip()

    if action in ("ha_list_devices", "list_devices", "list_entities"):
        domain = params.get("domain")
        limit = int(params.get("limit", 20))
        return ha_list_devices(domain=domain, limit=limit)

    elif action in ("ha_get_state", "get_state"):
        entity_id = str(params.get("entity_id", ""))
        return ha_get_state(entity_id)

    elif action in ("ha_call_service", "call_service"):
        domain = str(params.get("domain", ""))
        service = str(params.get("service", ""))
        entity_id = params.get("entity_id")
        service_data = params.get("service_data")
        return ha_call_service(domain, service, entity_id=entity_id, service_data=service_data)

    elif action in ("ha_trigger_scene", "trigger_scene", "scene"):
        scene = str(params.get("scene", ""))
        return ha_trigger_scene(scene)

    return outcome.failed(
        "home_assistant",
        f"Unknown action '{action}'. Supported: ha_list_devices, ha_get_state, ha_call_service, ha_trigger_scene",
    )


PLUGIN = {
    "name": "home_assistant",
    "description": (
        "Home Assistant smart home control: query devices and entity states (lights, thermostats, switches, locks), "
        "call Home Assistant services, and trigger automation scenes."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Exact action: ha_list_devices | ha_get_state | ha_call_service | ha_trigger_scene",
            },
            "domain": {
                "type": "STRING",
                "description": "Entity domain: light | switch | climate | scene | lock | sensor",
            },
            "service": {
                "type": "STRING",
                "description": "Service to call: turn_on | turn_off | toggle | set_temperature",
            },
            "entity_id": {
                "type": "STRING",
                "description": "Target entity ID e.g. 'light.living_room' or 'climate.thermostat'.",
            },
            "scene": {
                "type": "STRING",
                "description": "Scene name or entity ID to trigger.",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Maximum devices to list (default: 20).",
            },
            "service_data": {
                "type": "OBJECT",
                "description": "Optional extra parameters for the service.",
            },
        },
        "required": ["action"],
    },
    "permissions": ["home_assistant"],
}
