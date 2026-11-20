# gui/__init__.py — Jarvis Desktop Agent GUI package.
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
    "COLOR_STATE_LISTENING",
    "COLOR_STATE_SPEAKING",
    "COLOR_STATE_THINKING",
    "COLOR_STATE_PROCESSING",
    "COLOR_BLUE_PRIMARY",
    "COLOR_BLUE_GLOW",
    "COLOR_BLUE_OUTER",
    "COLOR_BLUE_HIGHLIGHT",
    "_FramelessWindowResizeMixin",
    "_ResizeEdge",
    "_cursor_for_edge",
    "_detect_resize_edge",
    "_NO_SCROLLBAR_STYLE",
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
    "format_visual_content_html",
]
