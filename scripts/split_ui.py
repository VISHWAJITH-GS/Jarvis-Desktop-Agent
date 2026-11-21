"""
scripts/split_ui.py — Script to modularize ui.py into gui/ package.
"""

import os
import sys


def main():
    # Read original ui.py
    with open("ui.py", "r", encoding="utf-8") as f:
        src = f.read()

    lines = src.splitlines(keepends=True)
    if len(lines) < 1000:
        print("ui.py already trimmed, fetching from git...")
        import subprocess

        res = subprocess.run(["git", "show", "HEAD:ui.py"], capture_output=True, text=True, encoding="utf-8")
        lines = res.stdout.splitlines(keepends=True)

    print(f"Read ui.py: {len(lines)} lines")

    os.makedirs("gui", exist_ok=True)

    class_indices = {}
    for i, line in enumerate(lines):
        if line.startswith("class "):
            name = line.split()[1].split("(")[0].split(":")[0]
            class_indices[name] = i

    print("Discovered class indices:")
    for k, v in class_indices.items():
        print(f"  {k}: line {v + 1}")

    pyqt_header = [
        "from __future__ import annotations\n",
        "from PyQt6.QtCore import *\n",
        "from PyQt6.QtGui import *\n",
        "from PyQt6.QtWidgets import *\n",
    ]

    # Build gui/theme.py
    theme_content = (
        [
            "from __future__ import annotations\n",
            "import colorsys\n",
            "from PyQt6.QtGui import QColor\n",
            "\n",
        ]
        + lines[class_indices["C"] : class_indices["_ResizeEdge"]]
        + [
            "\n",
            "_RESIZE_MARGIN = 8\n",
            "RESIZE_MARGIN = 8\n",
            "__all__ = [\n",
            '    "C",\n',
            '    "_HUE_LINKED",\n',
            '    "_PALETTE_DEFAULTS",\n',
            '    "DEFAULT_UI_COLOR",\n',
            '    "COLOR_STATE_LISTENING",\n',
            '    "COLOR_STATE_SPEAKING",\n',
            '    "COLOR_STATE_THINKING",\n',
            '    "COLOR_STATE_PROCESSING",\n',
            '    "COLOR_BLUE_PRIMARY",\n',
            '    "COLOR_BLUE_GLOW",\n',
            '    "COLOR_BLUE_OUTER",\n',
            '    "COLOR_BLUE_HIGHLIGHT",\n',
            '    "_lerp_color",\n',
            '    "apply_ui_accent",\n',
            '    "_NO_SCROLLBAR_STYLE",\n',
            '    "_RESIZE_MARGIN",\n',
            '    "RESIZE_MARGIN",\n',
            "]\n",
        ]
    )
    with open("gui/theme.py", "w", encoding="utf-8") as f:
        f.writelines(theme_content)

    # Build gui/base.py
    base_content = (
        lines[: class_indices["C"]]
        + [
            "from gui.theme import *\n",
            "from gui.theme import _RESIZE_MARGIN, _NO_SCROLLBAR_STYLE, _HUE_LINKED, _PALETTE_DEFAULTS, _lerp_color\n",
            "\n",
        ]
        + lines[class_indices["_ResizeEdge"] : class_indices["HudFadeOverlay"]]
        + [
            "\n",
            "__all__ = [\n",
            '    "_WIN_HIDE",\n',
            '    "_base_dir",\n',
            '    "BASE_DIR",\n',
            '    "CONFIG_DIR",\n',
            '    "API_FILE",\n',
            '    "_read_full_config",\n',
            '    "APP_VERSION",\n',
            '    "APP_PROTOCOL",\n',
            '    "_DEFAULT_W",\n',
            '    "_DEFAULT_H",\n',
            '    "_MIN_W",\n',
            '    "_MIN_H",\n',
            '    "_LEFT_W",\n',
            '    "_RIGHT_W",\n',
            '    "_OS",\n',
            '    "_ResizeEdge",\n',
            '    "_detect_resize_edge",\n',
            '    "_cursor_for_edge",\n',
            '    "_FramelessWindowResizeMixin",\n',
            '    "current_palette",\n',
            '    "retheme_all_widgets",\n',
            '    "qcol",\n',
            '    "_nvml_gpu_windows",\n',
            '    "_SysMetrics",\n',
            '    "rgba",\n',
            '    "_load_app_fonts",\n',
            '    "keena_font",\n',
            '    "HAVE_VIDEO",\n',
            '    "HoloAvatar",\n',
            '    "get_uicon",\n',
            "]\n",
        ]
    )
    with open("gui/base.py", "w", encoding="utf-8") as f:
        f.writelines(base_content)

    std_imports = [
        "import json\n",
        "import math\n",
        "import os\n",
        "import platform\n",
        "import random\n",
        "import re\n",
        "import subprocess\n",
        "import sys\n",
        "import threading\n",
        "import time\n",
        "from pathlib import Path\n",
    ]

    base_imports = (
        pyqt_header
        + std_imports
        + [
            "from gui.base import *\n",
            "from gui.base import _WIN_HIDE, _base_dir, BASE_DIR, CONFIG_DIR, API_FILE, _read_full_config, _DEFAULT_W, _DEFAULT_H, _MIN_W, _MIN_H, _LEFT_W, _RIGHT_W, _OS, _ResizeEdge, _detect_resize_edge, _cursor_for_edge, _FramelessWindowResizeMixin, _SysMetrics, _load_app_fonts, keena_font, HAVE_VIDEO, HoloAvatar, get_uicon\n",
            "from gui.theme import *\n",
            "from gui.theme import _RESIZE_MARGIN, _NO_SCROLLBAR_STYLE, _HUE_LINKED, _PALETTE_DEFAULTS, _lerp_color\n",
            "\n",
        ]
    )

    # Build gui/hud.py
    hud_content = base_imports + lines[class_indices["HudFadeOverlay"] : class_indices["MetricBar"]]
    with open("gui/hud.py", "w", encoding="utf-8") as f:
        f.writelines(hud_content)

    # Build gui/widgets.py
    widgets_content = base_imports + lines[class_indices["MetricBar"] : class_indices["SetupOverlay"]]
    with open("gui/widgets.py", "w", encoding="utf-8") as f:
        f.writelines(widgets_content)

    # Build gui/overlays.py
    overlays_content = (
        base_imports
        + [
            "from gui.widgets import *\n",
            "\n",
        ]
        + lines[class_indices["SetupOverlay"] : class_indices["_ScrollSafeCombo"]]
    )
    with open("gui/overlays.py", "w", encoding="utf-8") as f:
        f.writelines(overlays_content)

    # Build gui/providers_panel.py
    prov_content = (
        base_imports
        + [
            "from gui.overlays import _HudOverlay\n",
            "\n",
        ]
        + lines[class_indices["_ScrollSafeCombo"] : class_indices["ClipboardPanel"]]
    )
    with open("gui/providers_panel.py", "w", encoding="utf-8") as f:
        f.writelines(prov_content)

    # Build gui/settings_overlays.py
    sett_content = (
        base_imports
        + [
            "from gui.overlays import _HudOverlay\n",
            "\n",
        ]
        + lines[class_indices["ClipboardPanel"] : class_indices["BottomChatDock"]]
    )
    with open("gui/settings_overlays.py", "w", encoding="utf-8") as f:
        f.writelines(sett_content)

    # Build gui/docks.py
    docks_content = (
        base_imports
        + [
            "from gui.widgets import *\n",
            "\n",
        ]
        + lines[class_indices["BottomChatDock"] : class_indices["RemindersDisplayWindow"]]
    )
    with open("gui/docks.py", "w", encoding="utf-8") as f:
        f.writelines(docks_content)

    # Build gui/display_windows.py
    disp_content = (
        base_imports
        + [
            "from gui.widgets import *\n",
            "\n",
        ]
        + lines[class_indices["RemindersDisplayWindow"] : class_indices["ArmouryCrateTelemetryWidget"]]
    )
    with open("gui/display_windows.py", "w", encoding="utf-8") as f:
        f.writelines(disp_content)

    # Build gui/telemetry_and_displays.py
    telem_content = (
        base_imports
        + [
            "from gui.widgets import *\n",
            "\n",
        ]
        + lines[class_indices["ArmouryCrateTelemetryWidget"] : class_indices["MainWindow"]]
    )
    with open("gui/telemetry_and_displays.py", "w", encoding="utf-8") as f:
        f.writelines(telem_content)

    # Split MainWindow into mixins
    mw_lines = lines[class_indices["MainWindow"] : class_indices["_RootShim"]]
    method_offsets = {}
    for i, line in enumerate(mw_lines):
        if line.startswith("    def "):
            mname = line.split()[1].split("(")[0]
            method_offsets[mname] = i

    audio_start = method_offsets["_build_hud_controls_bar"]
    audio_end = method_offsets["_toggle_fullscreen"]

    media_start = method_offsets["_on_video_open"]
    media_end = method_offsets["resizeEvent"]

    panels_start = method_offsets["_build_input_row"]
    panels_end = method_offsets["_build_footer"]

    controls_start = method_offsets["_build_footer"]
    controls_end = len(mw_lines)

    # WindowMediaMixin
    media_mixin_content = (
        base_imports
        + [
            "class WindowMediaMixin:\n",
            '    """Video playback and desktop shortcut management for MainWindow."""\n',
        ]
        + mw_lines[media_start:media_end]
    )
    with open("gui/window_media.py", "w", encoding="utf-8") as f:
        f.writelines(media_mixin_content)

    # WindowAudioMixin
    audio_mixin_content = (
        base_imports
        + [
            "class WindowAudioMixin:\n",
            '    """Cyber drawer and audio management for MainWindow."""\n',
        ]
        + mw_lines[audio_start:audio_end]
    )
    with open("gui/window_audio.py", "w", encoding="utf-8") as f:
        f.writelines(audio_mixin_content)

    # WindowPanelsMixin
    panels_mixin_content = (
        base_imports
        + [
            "class WindowPanelsMixin:\n",
            '    """Interactive content, quiz, code job, and review panels for MainWindow."""\n',
        ]
        + mw_lines[panels_start:panels_end]
    )
    with open("gui/window_panels.py", "w", encoding="utf-8") as f:
        f.writelines(panels_mixin_content)

    # WindowControlsMixin
    controls_mixin_content = (
        base_imports
        + [
            "class WindowControlsMixin:\n",
            '    """Settings, PTT, wake word, overlays, and system controls for MainWindow."""\n',
        ]
        + mw_lines[controls_start:controls_end]
    )
    with open("gui/window_controls.py", "w", encoding="utf-8") as f:
        f.writelines(controls_mixin_content)

    # Core MainWindow
    core_mw_content = (
        base_imports
        + [
            "from gui.hud import *\n",
            "from gui.widgets import *\n",
            "from gui.overlays import *\n",
            "from gui.providers_panel import *\n",
            "from gui.settings_overlays import *\n",
            "from gui.docks import *\n",
            "from gui.display_windows import *\n",
            "from gui.telemetry_and_displays import *\n",
            "from gui.window_media import WindowMediaMixin\n",
            "from gui.window_audio import WindowAudioMixin\n",
            "from gui.window_panels import WindowPanelsMixin\n",
            "from gui.window_controls import WindowControlsMixin\n",
            "\n",
            "class MainWindow(\n",
            "    WindowMediaMixin,\n",
            "    WindowAudioMixin,\n",
            "    WindowPanelsMixin,\n",
            "    WindowControlsMixin,\n",
            "    _FramelessWindowResizeMixin,\n",
            "    QMainWindow,\n",
            "):\n",
        ]
        + mw_lines[1:audio_start]
        + mw_lines[audio_end:media_start]
        + mw_lines[media_end:panels_start]
    )
    with open("gui/window.py", "w", encoding="utf-8") as f:
        f.writelines(core_mw_content)

    # Build gui/facade.py
    facade_content = (
        base_imports
        + [
            "from gui.window import MainWindow\n",
            "\n",
        ]
        + lines[class_indices["_RootShim"] :]
    )
    with open("gui/facade.py", "w", encoding="utf-8") as f:
        f.writelines(facade_content)

    # Build gui/__init__.py
    init_content = """# gui/__init__.py — Jarvis Desktop Agent GUI package.
from __future__ import annotations

from gui.theme import *
from gui.base import *
from gui.hud import *
from gui.widgets import *
from gui.overlays import *
from gui.providers_panel import *
from gui.settings_overlays import *
from gui.docks import *
from gui.display_windows import *
from gui.telemetry_and_displays import *
from gui.window_media import *
from gui.window_audio import *
from gui.window_panels import *
from gui.window_controls import *
from gui.window import *
from gui.facade import *

__all__ = [
    "C",
    "apply_ui_accent",
    "DEFAULT_UI_COLOR",
    "MainWindow",
    "JarvisUI",
    "HudCanvas",
    "HudConversationWidget",
    "BottomChatDock",
    "LeftSearchDock",
    "ScreenGlowOverlay",
    "SlideDownAlert",
    "RemindersDisplayWindow",
    "MemoryActionsDisplayWindow",
    "ArmouryCrateTelemetryWidget",
    "MinimizedDisplayPill",
    "MinimizedDisplaysDock",
    "JarvisDisplayWindow",
    "MetricBar",
    "LogWidget",
    "FileDropZone",
    "SetupOverlay",
    "CustomizeOverlay",
    "PluginManagerOverlay",
    "ConfirmBanner",
    "AudioDeviceOverlay",
    "MemoryOverlay",
    "ProvidersOverlay",
    "ClipboardPanel",
    "PluginSettingsOverlay",
    "RemoteKeyOverlay",
]
"""
    with open("gui/__init__.py", "w", encoding="utf-8") as f:
        f.write(init_content)

    # Build thin ui.py
    ui_content = """# ui.py — Thin facade exposing JarvisUI and re-exporting all GUI symbols for compatibility.
from __future__ import annotations

from gui import *
from gui import __all__ as _gui_all

__all__ = list(_gui_all)
"""
    with open("ui.py", "w", encoding="utf-8") as f:
        f.write(ui_content)

    print("Successfully modularized ui.py into gui/ package!")


if __name__ == "__main__":
    main()
