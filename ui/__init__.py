"""
ui/__init__.py — Jarvis UI package compatibility facade.
Exposes JarvisUI and all GUI classes and re-exports gui.* and event_bus.
"""
from __future__ import annotations

import gui
for _k, _v in gui.__dict__.items():
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = _v

from ui.event_bus import UIEventBus, get_ui_event_bus
from gui import __all__ as _gui_all
__all__ = list(_gui_all) + ["UIEventBus", "get_ui_event_bus"]
