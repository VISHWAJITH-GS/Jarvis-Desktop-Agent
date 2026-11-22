"""
core/schema_utils.py — Shared Schema Validation and Tool Metadata Utilities

Consolidates schema helpers, metadata stripping, and rigorous parameter type/enum
validation shared between core.action_loader and core.plugin_loader.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}

_BEHAVIORS = ("BLOCKING", "NON_BLOCKING")
_SCHEDULING = ("WHEN_IDLE", "SILENT", "INTERRUPT")

META_KEYS = ("tier", "brief")


def strip_meta(decls: list) -> list:
    """Declarations without META_KEYS, deduplicated by function name. Non-dict entries pass through untouched;
    a declaration carrying none is returned as the same object, so what an API
    receives is byte-for-byte what it received before these keys existed."""
    out = []
    seen = set()
    for decl in decls:
        name = decl.get("name") if isinstance(decl, dict) else getattr(decl, "name", None)
        if name:
            if name in seen:
                continue
            seen.add(name)
        if isinstance(decl, dict) and any(k in decl for k in META_KEYS):
            out.append({k: v for k, v in decl.items() if k not in META_KEYS})
        else:
            out.append(decl)
    return out


def _opt_upper(value: Any, allowed: tuple[str, ...]) -> Optional[str]:
    v = str(value or "").strip().upper()
    return v if v in allowed else None


def _opt_tier(value: Any) -> Optional[str]:
    """ "core" or "extra" from a declaration, or None when it did not say.
    Typos fall back to None rather than crashing.
    """
    text = str(value or "").strip().lower()
    return text if text in ("core", "extra") else None


def _opt_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def validate_parameters(schema: dict, parameters: dict) -> Tuple[bool, str]:
    """
    Rigorously validate arguments against a JSON schema:
    1. Checks required properties are present and non-empty.
    2. Validates declared types (STRING, INTEGER, NUMBER, BOOLEAN, ARRAY, OBJECT).
    3. Enforces enum values when defined.
    """
    if not isinstance(parameters, dict):
        return False, "Parameters must be provided as a dictionary."

    sch = schema or {}
    properties = sch.get("properties", {})
    required = sch.get("required", [])

    # 1. Required keys check
    for req in required:
        if req not in parameters:
            return False, f"Missing required parameter '{req}'"
        val = parameters[req]
        if val is None or (isinstance(val, str) and not val.strip()):
            return False, f"Required parameter '{req}' cannot be null or empty."

    # 2. Type and Enum checks for provided properties
    for param_name, param_val in parameters.items():
        if param_name not in properties or param_val is None:
            continue

        prop_schema = properties[param_name]
        expected_type = str(prop_schema.get("type") or "").upper().strip()

        # Type validation
        if expected_type == "STRING":
            if not isinstance(param_val, str):
                return False, f"Parameter '{param_name}' must be a string, got {type(param_val).__name__}."
        elif expected_type == "INTEGER":
            if not isinstance(param_val, int) or isinstance(param_val, bool):
                # Try integer string conversion if string
                if isinstance(param_val, str) and param_val.isdigit():
                    pass
                else:
                    return False, f"Parameter '{param_name}' must be an integer, got {type(param_val).__name__}."
        elif expected_type == "NUMBER":
            if not isinstance(param_val, (int, float)) or isinstance(param_val, bool):
                try:
                    float(param_val)
                except (ValueError, TypeError):
                    return False, f"Parameter '{param_name}' must be a number, got {type(param_val).__name__}."
        elif expected_type == "BOOLEAN":
            if not isinstance(param_val, bool):
                if isinstance(param_val, str) and param_val.lower() in ("true", "false"):
                    pass
                else:
                    return False, f"Parameter '{param_name}' must be a boolean, got {type(param_val).__name__}."
        elif expected_type == "ARRAY":
            if not isinstance(param_val, (list, tuple)):
                return False, f"Parameter '{param_name}' must be a list/array, got {type(param_val).__name__}."
        elif expected_type == "OBJECT":
            if not isinstance(param_val, dict):
                return False, f"Parameter '{param_name}' must be an object/dict, got {type(param_val).__name__}."

        # Enum validation
        allowed_enums = prop_schema.get("enum")
        if allowed_enums and isinstance(allowed_enums, (list, tuple, set)):
            # Case-insensitive comparison if string enum
            val_match = False
            for allowed in allowed_enums:
                if isinstance(param_val, str) and isinstance(allowed, str):
                    if param_val.lower() == allowed.lower():
                        val_match = True
                        break
                elif param_val == allowed:
                    val_match = True
                    break
            if not val_match:
                return (
                    False,
                    f"Value '{param_val}' for parameter '{param_name}' is not in allowed choices: {list(allowed_enums)}.",
                )

    return True, ""
