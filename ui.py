# ui.py — Thin facade exposing JarvisUI and re-exporting all GUI symbols for compatibility.
from __future__ import annotations

import gui

for _k, _v in gui.__dict__.items():
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = _v

from gui import __all__ as _gui_all

__all__ = list(_gui_all)
