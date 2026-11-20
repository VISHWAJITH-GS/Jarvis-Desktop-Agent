"""
ui/__init__.py — Mark-LVI UI modular package.

Houses the UI event bus, modular UI components, and bridges to the Qt main thread.
Re-exports root ui.py symbols to maintain 100% backward compatibility.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from ui.event_bus import UIEventBus, get_ui_event_bus

_root_ui_path = Path(__file__).resolve().parent.parent / "ui.py"
if _root_ui_path.exists():
    _spec = importlib.util.spec_from_file_location("_legacy_ui", str(_root_ui_path))
    if _spec and _spec.loader:
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules["_legacy_ui"] = _mod
        _spec.loader.exec_module(_mod)
        for _k, _v in _mod.__dict__.items():
            if not _k.startswith("__"):
                globals()[_k] = _v

