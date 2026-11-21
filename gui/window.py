from __future__ import annotations
from PyQt6.QtCore import *
from PyQt6.QtGui import *
from PyQt6.QtWidgets import *
import json
import math
import os
import platform
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from gui.base import *
from gui.base import (
    _WIN_HIDE,
    _base_dir,
    BASE_DIR,
    CONFIG_DIR,
    API_FILE,
    _read_full_config,
    _DEFAULT_W,
    _DEFAULT_H,
    _MIN_W,
    _MIN_H,
    _LEFT_W,
    _RIGHT_W,
    _OS,
    _ResizeEdge,
    _detect_resize_edge,
    _cursor_for_edge,
    _FramelessWindowResizeMixin,
    _SysMetrics,
    _metrics,
    _nvml_gpu_windows,
    _load_app_fonts,
    keena_font,
    HAVE_VIDEO,
    HoloAvatar,
    get_uicon,
    QGraphicsVideoItem,
    QAudioOutput,
    QMediaPlayer,
)
from gui.theme import *
from gui.theme import _RESIZE_MARGIN, _NO_SCROLLBAR_STYLE, _HUE_LINKED, _PALETTE_DEFAULTS, _lerp_color

from gui.hud import *
from gui.widgets import *
from gui.overlays import *
from gui.providers_panel import *
from gui.settings_overlays import *
from gui.docks import *
from gui.display_windows import *
from gui.telemetry_and_displays import *
from gui.window_media import WindowMediaMixin
from gui.window_audio import WindowAudioMixin
from gui.window_panels import WindowPanelsMixin
from gui.window_controls import WindowControlsMixin


class MainWindow(
    WindowMediaMixin,
    WindowAudioMixin,
    WindowPanelsMixin,
    WindowControlsMixin,
    _FramelessWindowResizeMixin,
    QMainWindow,
):
    _log_sig = pyqtSignal(str)
    _state_sig = pyqtSignal(str)
    _content_sig = pyqtSignal(str, str)  # (title, text) — thread-safe content display
    _codejob_sig = pyqtSignal(object)  # dev_agent job state — live coding panel
    _reconfig_sig = pyqtSignal()  # trigger setup overlay from any thread
    _video_open_sig = pyqtSignal(str, str, bool, str)  # video, title, muted, audio
    _wake_btns_sig = pyqtSignal()  # wake state resolved off-thread
    _video_close_sig = pyqtSignal()
    _video_mute_sig = pyqtSignal(bool)
    _clipboard_sig = pyqtSignal(str)  # clipboard text changed (thread-safe)
    _mute_sig = pyqtSignal(bool)  # thread-safe mute state toggle
    _mute_state_sig = pyqtSignal(bool, str)  # thread-safe atomic (muted, hud_state) sync
    _confirm_sig = pyqtSignal(str, str)  # (title, detail) — irreversible-action gate
    _confirm_hide_sig = pyqtSignal()
    _wake_dl_sig = pyqtSignal(bool, str)  # wake-word install finished (ok, message)
    _quiz_sig = pyqtSignal(str, object, object)  # (topic, questions, grader)
    _quiz_hide_sig = pyqtSignal()
    _review_sig = pyqtSignal(str, str, object, object)  # document review payload
    _open_display_sig = pyqtSignal(str, str, str)  # (content_type, source, encoded_title)
    _focus_display_sig = pyqtSignal(int)  # (display_id)
    _maximize_display_sig = pyqtSignal(int)  # (display_id: 0 = all)
    _minimize_display_sig = pyqtSignal(int)  # (display_id: 0 = all)
    _close_display_sig = pyqtSignal(int)  # (display_id)
    _stream_user_sig = pyqtSignal(str)  # (streamed user transcript)
    _stream_reply_sig = pyqtSignal(str)  # (streamed assistant transcript)
    _jarvis_window_sig = pyqtSignal(str)  # ("minimize" | "maximize" | "restore")
    _slide_alert_sig = pyqtSignal(str, str)  # (title, message)
    _reminders_sig = pyqtSignal(str)  # (scope)
    _memory_actions_sig = pyqtSignal(str)  # (tab: "memory" | "actions")
    _screen_glow_sig = pyqtSignal(int)  # (duration_ms)
    _refresh_audio_sig = pyqtSignal()  # Trigger audio device dropdown refresh
    _audio_status_sig = pyqtSignal(object)  # Thread-safe AudioManager status snapshot

    def __init__(self, face_path: str):
        super().__init__()
        _load_app_fonts()
        self._face_path = face_path
        app = QApplication.instance()
        if app:
            app.setStyleSheet((app.styleSheet() or "") + "\n" + _NO_SCROLLBAR_STYLE)

        # Load customization from config
        _cfg = _read_full_config()
        self._assistant_name: str = (_cfg.get("assistant_name") or "JARVIS").strip()
        _display = self._assistant_name.upper()

        # Apply the saved UI colour BEFORE panels/stylesheets are built
        _ui_color = (_cfg.get("ui_color") or "").strip()
        if _ui_color and _ui_color.lower() != DEFAULT_UI_COLOR:
            apply_ui_accent(_ui_color)

        self.setWindowTitle(f"{_display} — {APP_VERSION}")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(280, 380)
        self._init_frameless_resize()
        self._is_closed: bool = False
        self._expanded: bool = False
        self._is_fullscreen: bool = False

        screen = QApplication.primaryScreen().availableGeometry()
        hud_w, hud_h = 420, 580
        init_x = max(screen.left() + 20, screen.right() - hud_w - 24)
        init_y = max(screen.top() + 20, screen.bottom() - hud_h - 24)
        self.setGeometry(init_x, init_y, hud_w, hud_h)

        self.on_text_command = None
        self.on_remote_clicked = None  # callable: () -> (url, key) | None
        self.on_interrupt = None  # callable: () -> None — stop JARVIS mid-speech
        self.on_mute_toggle = None  # callable: () -> None — central VoiceStateManager mute toggle
        self.on_voice_change = None  # callable: () -> None — rebuild session with new voice
        self.on_audio_device_change = None  # callable: () -> None — reopen audio streams
        self._confirm_overlay = None  # live ConfirmBanner, if one is on screen
        self.get_plugins = None  # callable: () -> list[dict], set by JarvisLive
        self.get_plugin_settings = None  # callable: () -> list[dict] settings schemas, set by JarvisLive
        self.on_wake_toggle = None  # callable: (enable: bool) -> str, set by JarvisLive
        self.on_wake_manual = None  # callable: () -> None — manual sleep/wake
        self.on_auto_sleep_toggle = None  # callable: (enable: bool) -> bool, set by JarvisLive
        self.auto_sleep_is_enabled = None  # callable: () -> bool, set by JarvisLive
        self.on_routing_mode_change = None  # callable: (mode: str) -> None, set by JarvisLive
        self.get_routing_mode = None  # callable: () -> str, set by JarvisLive
        self.on_push_to_talk = None  # callable: (enable: bool) -> str scope
        self.ptt_hold = None  # callable: (held: bool) -> None — windowed chord
        self.wake_get_state = None  # callable: () -> dict {enabled, awake, ready}
        self._muted = False
        self._current_file: str | None = None
        self._remote_overlay: RemoteKeyOverlay | None = None
        self._customize_overlay: CustomizeOverlay | None = None

        self._left_panel = self._build_left_panel()
        self._right_panel = self._build_right_panel()

        # Center column: HUD + resizable content panel via QSplitter
        self.hud = HudCanvas(face_path, _display)
        self.hud.reactor_clicked.connect(self.toggle_minimize_top)
        self._content_panel = self._build_content_panel()
        self._quiz_panel = self._build_quiz_panel()

        # ── Video surface ────────────────────────────────────────────────
        self._video_split = False  # is the sound a separate stream?
        self._video_auto_muted = False  # did JARVIS close the mic, or the user?
        self._video_on = False
        self._video_cont = QWidget()
        self._video_cont.setStyleSheet(f"background: {C.BG};")
        _vid_v = QVBoxLayout(self._video_cont)
        _vid_v.setContentsMargins(8, 6, 8, 8)
        _vid_v.setSpacing(4)

        _vid_hdr = QHBoxLayout()
        self._video_title = QLabel("▶  VIDEO")
        self._video_title.setFont(QFont("Montserrat", 9, QFont.Weight.Bold))
        self._video_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _vid_hdr.addWidget(self._video_title)
        _vid_hdr.addStretch()

        def _vid_btn(text: str) -> QPushButton:
            b = QPushButton(text)
            b.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    color: {C.TEXT_DIM}; background: transparent;
                    border: none; padding: 2px 6px;
                }}
                QPushButton:hover {{ color: {C.PRI}; }}
            """)
            return b

        self._video_mute_btn = _vid_btn("🔇  SOUND OFF")
        self._video_mute_btn.clicked.connect(self._toggle_video_mute)
        _vid_hdr.addWidget(self._video_mute_btn)

        _vid_x = _vid_btn("✕  CLOSE")
        _vid_x.clicked.connect(self.stop_video)
        _vid_hdr.addWidget(_vid_x)
        _vid_v.addLayout(_vid_hdr)

        if HAVE_VIDEO:
            self._video_scene = QGraphicsScene(self)
            self._video_item = QGraphicsVideoItem()
            self._video_scene.addItem(self._video_item)
            self._video_widget = QGraphicsView(self._video_scene)
            self._video_widget.setStyleSheet("background: #000; border: none;")
            self._video_widget.setFrameShape(QGraphicsView.Shape.NoFrame)
            self._video_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._video_widget.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._video_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            _vid_v.addWidget(self._video_widget, stretch=1)

            self._video_audio = QAudioOutput()
            self._video_audio.setMuted(True)
            self._video_player = QMediaPlayer()
            self._video_player.setVideoOutput(self._video_item)
            self._video_item.nativeSizeChanged.connect(self._fit_video)
            self._video_player.setAudioOutput(self._video_audio)
            self._video_player.errorOccurred.connect(self._on_video_error)

            self._video_sound = QMediaPlayer()
            self._video_sound_out = QAudioOutput()
            self._video_sound_out.setMuted(True)
            self._video_sound.setAudioOutput(self._video_sound_out)

            self._video_sync = QTimer(self)
            self._video_sync.setInterval(1000)
            self._video_sync.timeout.connect(self._sync_video_sound)
        else:
            self._video_scene = None
            self._video_item = None
            self._video_widget = None
            self._video_player = None
            self._video_audio = None
            self._video_sound = None
            self._video_sound_out = None
            self._video_sync = None
            _miss = QLabel("Video playback is not available in this Qt install.")
            _miss.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _miss.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            _vid_v.addWidget(_miss, stretch=1)

        # Stack: 0 = animated HUD, 1 = video
        self._hud_cam_stack = QStackedWidget()
        self._hud_cam_stack.addWidget(self.hud)
        self._hud_cam_stack.addWidget(self._video_cont)

        # ── Compact HUD container (only the compact HUD!)
        self.hud_box = QWidget()
        self.hud_box.setMinimumWidth(300)
        self.hud_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.hud_box.setStyleSheet("background: transparent;")
        self.hud_box.setMouseTracking(True)
        self.hud_box.installEventFilter(self)
        self.hud.setMouseTracking(True)
        self.hud.installEventFilter(self)
        hud_v = QVBoxLayout(self.hud_box)
        hud_v.setContentsMargins(0, 0, 0, 0)
        hud_v.setSpacing(0)
        hud_v.addWidget(self._hud_cam_stack, stretch=1)
        self.hud_ctrl_bar = self._build_hud_controls_bar()
        hud_v.addWidget(self.hud_ctrl_bar, stretch=0)

        # ── Expanded Cyber Drawer (attached directly to JARVIS window)
        self._drawer = self._build_cyber_drawer()
        self._drawer.setVisible(False)
        self._drawer.setMouseTracking(True)
        self._drawer.installEventFilter(self)

        central = QWidget()
        central.setStyleSheet("background: transparent;")
        central.setMouseTracking(True)
        central.installEventFilter(self)
        self.setCentralWidget(central)

        self._main_h = QHBoxLayout(central)
        self._main_h.setContentsMargins(0, 0, 0, 0)
        self._main_h.setSpacing(8)
        self._main_h.addWidget(self._drawer, stretch=1)
        self._main_h.addWidget(self.hud_box, stretch=0)

        # Edge-docked components:
        # 1. Chat log at bottom center of screen (long width, short height)
        self.bottom_chat_dock = BottomChatDock(self)

        # 2. Search display popping up from left edge
        self.left_search_dock = LeftSearchDock(self)

        # Arc Reactor minimize tracking
        self._is_minimized_top = False
        self._normal_geom = self.geometry()
        self._active_corner = "BR"

        # Dummy compatibility references
        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._quick_drawer = self._build_quick_drawer()
        self._ctrl_drawer = self._build_controls_drawer()
        self._warm_wake_state()
        self._update_autostart_btn(self._check_autostart())
        from memory.config_manager import get_brief_enabled as _gbe

        self._update_brief_btn(_gbe())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metric update timer
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        self._log_sig.connect(self._on_log_line)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)
        self._codejob_sig.connect(self._show_code_job)
        self._reconfig_sig.connect(self._show_setup)
        self._confirm_sig.connect(self._show_confirm_banner)
        self._confirm_hide_sig.connect(self._hide_confirm_banner)
        self._wake_btns_sig.connect(self._refresh_wake_btns)
        self._video_open_sig.connect(self._on_video_open)
        self._video_close_sig.connect(self._on_video_close)
        self._video_mute_sig.connect(self._on_video_mute)
        self._mute_sig.connect(self._set_muted)
        self._mute_state_sig.connect(self._sync_mute_state)
        self._clipboard_sig.connect(self._show_clipboard_panel)
        self._wake_dl_sig.connect(self._on_wake_install_done)
        self._quiz_sig.connect(self._show_quiz)
        self._quiz_hide_sig.connect(self._hide_quiz)
        self._review_sig.connect(self._show_review)
        self._stream_user_sig.connect(self._on_stream_user)
        self._stream_reply_sig.connect(self._on_stream_reply)
        self._jarvis_window_sig.connect(self._on_jarvis_window_control)
        self._slide_alert_sig.connect(self._show_slide_alert)
        self._reminders_sig.connect(self._show_reminders)
        self._memory_actions_sig.connect(self._show_memory_actions)
        self._screen_glow_sig.connect(self._on_screen_glow)
        self._refresh_audio_sig.connect(self._refresh_audio_tab)
        self._audio_status_sig.connect(self._apply_audio_status)
        self._screen_glow_overlay = ScreenGlowOverlay(self)
        self._search_dock_minimized_to_bar = False
        self._reminders_dock_minimized_to_bar = False
        self._memory_actions_dock_minimized_to_bar = False
        self._slide_alert = None
        self._reminders_dock = None
        self._memory_actions_dock = None
        self._displays: dict[int, JarvisDisplayWindow] = {}
        self._display_counter: int = 0
        self._reserved_display_ids: set[int] = set()
        self._minimized_display_ids: set[int] = set()
        self._dots_overlay = MinimizedDisplaysDock(self)
        self._dots_overlay.dot_clicked.connect(self._on_restore_display)
        self._open_display_sig.connect(self._on_open_display)
        self._focus_display_sig.connect(self._on_focus_display)
        self._maximize_display_sig.connect(self._on_maximize_display_win)
        self._minimize_display_sig.connect(self._on_minimize_display_win)
        self._close_display_sig.connect(self._on_close_display_win)

        # Clipboard triggers removed per user request
        self._clipboard_panel = None

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self._do_interrupt)

    def showEvent(self, event):
        super().showEvent(event)
        # Chat box is popped up ONLY if selected by the user. Do not auto-show on start.

    def closeEvent(self, event):
        self._is_closed = True
        if getattr(self, "_clipboard_panel", None) is not None:
            try:
                self._clipboard_panel.close()
            except Exception:
                pass
        if hasattr(self, "_dots_overlay"):
            self._dots_overlay.close()
        if hasattr(self, "_displays"):
            for w in list(self._displays.values()):
                try:
                    w.close()
                except Exception:
                    pass
            self._displays.clear()
            self._minimized_display_ids.clear()
            self._reserved_display_ids.clear()
            self._display_counter = 0
        if hasattr(self, "bottom_chat_dock"):
            self.bottom_chat_dock.close()
        if hasattr(self, "left_search_dock"):
            self.left_search_dock.close()
        super().closeEvent(event)

    def toggle_minimize_top(self):
        """
        Minimizes ONLY the circular Arc Reactor element to the left-bottom of the screen
        into a small favicon-sized floating orb, or restores it back to normal position.
        Triggered by clicking the Arc Reactor or the ◀ MINI button.
        """
        screen = QApplication.primaryScreen().availableGeometry()
        MINI_SIZE = 58  # Favicon-sized circular orb

        if getattr(self, "_is_minimized_top", False):
            # Restore to normal position at bottom-right
            self.hud._mini_mode = False
            self.setMinimumSize(280, 380)
            if hasattr(self, "hud_box"):
                self.hud_box.setMinimumWidth(300)
                self.hud_box.setMaximumWidth(16777215)
            if hasattr(self, "hud_ctrl_bar"):
                self.hud_ctrl_bar.setVisible(True)
            target = getattr(self, "_normal_geom", None)
            if not target:
                init_x = max(screen.left() + 20, screen.right() - 420 - 24)
                init_y = max(screen.top() + 20, screen.bottom() - 580 - 24)
                target = QRect(init_x, init_y, 420, 580)
            self._is_minimized_top = False
            if hasattr(self, "_hud_mini_btn"):
                self._hud_mini_btn.setText("MINI")
                self._hud_mini_btn.setToolTip("Minimize Arc Reactor circle to bottom-left (Favicon size)")
        else:
            if getattr(self, "_expanded", False):
                self._toggle_expanded(False)
            self._normal_geom = self.geometry()
            self.hud._mini_mode = True
            if hasattr(self, "hud_ctrl_bar"):
                self.hud_ctrl_bar.setVisible(False)
            if hasattr(self, "hud_box"):
                self.hud_box.setFixedWidth(MINI_SIZE)
            self.setMinimumSize(MINI_SIZE, MINI_SIZE)
            target_x = screen.left() + 24
            target_y = screen.bottom() - MINI_SIZE - 24
            target = QRect(target_x, target_y, MINI_SIZE, MINI_SIZE)
            self._is_minimized_top = True
            if hasattr(self, "_hud_mini_btn"):
                self._hud_mini_btn.setText("NORM")
                self._hud_mini_btn.setToolTip("Restore Arc Reactor to normal position")

        self._geom_anim = QPropertyAnimation(self, b"geometry")
        self._geom_anim.setDuration(300)
        self._geom_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._geom_anim.setStartValue(self.geometry())
        self._geom_anim.setEndValue(target)
        self._geom_anim.start()

    def snap_to_corner(self, corner: str):
        """
        Smoothly moves JARVIS to one of the 4 screen corners:
        'TL' (Top-Left), 'TR' (Top-Right), 'BL' (Bottom-Left), 'BR' (Bottom-Right).
        """
        screen = QApplication.primaryScreen().availableGeometry()
        pad = 24
        w = self.width()
        h = self.height()
        corner = corner.upper().strip()

        if corner == "TL":
            target_x = screen.left() + pad
            target_y = screen.top() + pad
        elif corner == "TR":
            target_x = screen.right() - w - pad
            target_y = screen.top() + pad
        elif corner == "BL":
            target_x = screen.left() + pad
            target_y = screen.bottom() - h - pad
        else:  # 'BR' default
            target_x = screen.right() - w - pad
            target_y = screen.bottom() - h - pad

        self._active_corner = corner
        target_pos = QPoint(target_x, target_y)

        self._pos_anim = QPropertyAnimation(self, b"pos")
        self._pos_anim.setDuration(280)
        self._pos_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._pos_anim.setStartValue(self.pos())
        self._pos_anim.setEndValue(target_pos)
        self._pos_anim.start()

    def cycle_corners(self):
        """Cycles smoothly through the 4 corners: BR -> BL -> TL -> TR -> BR."""
        order = ["BR", "BL", "TL", "TR"]
        curr = getattr(self, "_active_corner", "BR")
        try:
            next_idx = (order.index(curr) + 1) % len(order)
        except ValueError:
            next_idx = 0
        self.snap_to_corner(order[next_idx])

    def maximize_display(self):
        """Maximizes JARVIS to fullscreen mode or restores it."""
        self._toggle_fullscreen()

    def expand_display(self):
        """Expands the attached Cyber Drawer."""
        self._toggle_expanded(True)

    def collapse_display(self):
        """Collapses the Cyber Drawer into the compact HUD."""
        self._toggle_expanded(False)

    def close_display(self):
        """Closes the JARVIS display cleanly."""
        self.close()

    def toggle_chat_dock(self):
        """Toggle bottom chat dock visibility."""
        if hasattr(self, "bottom_chat_dock"):
            if self.bottom_chat_dock.isVisible():
                self.bottom_chat_dock.hide()
            else:
                self.bottom_chat_dock.show()
                self.bottom_chat_dock.raise_()

    def _get_next_display_id(self, reserve: bool = False) -> int:
        """Calculate the next sequential display ID starting from 1 based on active displays."""
        if not self._displays and not self._reserved_display_ids:
            self._display_counter = 0
            if reserve:
                self._reserved_display_ids.add(1)
            return 1
        d_id = 1
        while d_id in self._displays or d_id in self._reserved_display_ids:
            d_id += 1
        if reserve:
            self._reserved_display_ids.add(d_id)
        return d_id

    def _on_open_display(self, content_type: str, source: str, encoded_title: str):
        disp_id = None
        title = encoded_title
        if ":" in encoded_title:
            id_str, t = encoded_title.split(":", 1)
            try:
                candidate_id = int(id_str)
                if candidate_id > 0 and candidate_id not in self._displays:
                    disp_id = candidate_id
                    title = t
            except ValueError:
                pass

        if disp_id is None:
            disp_id = self._get_next_display_id(reserve=False)

        self._reserved_display_ids.discard(disp_id)
        self._display_counter = max(self._display_counter, disp_id)

        name = f"DISPLAY {disp_id}"
        win = JarvisDisplayWindow(disp_id, name, content_type, source, title)
        self._displays[disp_id] = win
        win.closed.connect(self._on_display_closed)
        win.minimized_requested.connect(self._on_display_minimized)

        screen = QApplication.primaryScreen().availableGeometry()
        offset = ((disp_id - 1) % 6) * 36
        x = max(
            screen.left() + 20, min(screen.right() - win.width() - 20, screen.center().x() - win.width() // 2 + offset)
        )
        y = max(
            screen.top() + 20,
            min(screen.bottom() - win.height() - 20, screen.center().y() - win.height() // 2 + offset),
        )
        win.move(x, y)
        win.show()
        win.raise_()
        win.activateWindow()
        self._on_log_line(f"SYS: Opened {name} ({content_type.upper()})")
        return disp_id

    def _on_display_minimized(self, d_id: int):
        self._minimized_display_ids.add(d_id)
        self._update_dots_overlay()

    def _on_restore_display(self, d_id):
        if d_id == "search":
            self._search_dock_minimized_to_bar = False
            self._update_dots_overlay()
            if hasattr(self, "left_search_dock") and self.left_search_dock:
                self.left_search_dock.restore_from_dock()
            return

        if d_id == "reminders":
            self._reminders_dock_minimized_to_bar = False
            self._update_dots_overlay()
            if hasattr(self, "_reminders_dock") and self._reminders_dock:
                self._reminders_dock.restore_from_dock()
            return

        if d_id == "memory_actions":
            self._memory_actions_dock_minimized_to_bar = False
            self._update_dots_overlay()
            if hasattr(self, "_memory_actions_dock") and self._memory_actions_dock:
                self._memory_actions_dock.restore_from_dock()
            return

        self._minimized_display_ids.discard(d_id)
        win = self._displays.get(d_id)
        if win:
            win.setWindowState(Qt.WindowState.WindowNoState)
            win.show()
            win.raise_()
            win.activateWindow()
        self._update_dots_overlay()

    def _on_display_closed(self, d_id: int):
        self._reserved_display_ids.discard(d_id)
        self._minimized_display_ids.discard(d_id)
        self._displays.pop(d_id, None)
        if not self._displays:
            self._display_counter = 0
            self._reserved_display_ids.clear()
        self._update_dots_overlay()

    def _update_dots_overlay(self):
        items = []
        for did in sorted(list(self._minimized_display_ids)):
            win = self._displays.get(did)
            title = win.title_text if win else f"Display {did}"
            c_type = win.content_type if win else "image"
            items.append((did, title, c_type))

        if getattr(self, "_search_dock_minimized_to_bar", False):
            s_title = self._content_title_lbl.text() if hasattr(self, "_content_title_lbl") else "Search Intel"
            items.append(("search", s_title, "search"))

        if getattr(self, "_reminders_dock_minimized_to_bar", False):
            items.append(("reminders", "Reminders Matrix", "reminders"))

        if getattr(self, "_memory_actions_dock_minimized_to_bar", False):
            items.append(("memory_actions", "Memory & Actions Matrix", "memory_actions"))

        self._dots_overlay.update_displays(items)

    def _on_screen_glow(self, duration_ms: int = 2500):
        if hasattr(self, "_screen_glow_overlay"):
            self._screen_glow_overlay.trigger_glow(duration_ms)

    def _update_dots_overlay_position(self):
        if hasattr(self, "_dots_overlay") and self._dots_overlay.isVisible():
            self._dots_overlay.reposition()

    def _on_focus_display(self, display_id: int = 0):
        if not self._displays:
            self._on_log_line("SYS: No active displays to focus")
            return False
        if display_id == 0:
            display_id = max(self._displays.keys())
        if display_id in self._displays:
            self._minimized_display_ids.discard(display_id)
            self._update_dots_overlay()
            win = self._displays[display_id]
            screen = QApplication.primaryScreen().availableGeometry()
            cx = max(screen.left() + 20, screen.center().x() - win.width() // 2)
            cy = max(screen.top() + 20, screen.center().y() - win.height() // 2)
            win.move(cx, cy)
            win.setWindowState(Qt.WindowState.WindowNoState)
            win.show()
            win.raise_()
            win.activateWindow()
            self._on_log_line(f"SYS: Focused DISPLAY {display_id} in center of screen")
            return True
        self._on_log_line(f"SYS: DISPLAY {display_id} not found")
        return False

    def _on_maximize_display_win(self, display_id: int = 0) -> bool:
        if not self._displays:
            self._on_log_line("SYS: No active displays to maximize")
            return False
        if display_id == 0:
            for w in list(self._displays.values()):
                self._minimized_display_ids.discard(w.display_id)
                w.maximize_window()
            self._update_dots_overlay()
            self._on_log_line("SYS: Maximized all displays")
            return True
        elif display_id in self._displays:
            self._minimized_display_ids.discard(display_id)
            self._update_dots_overlay()
            self._displays[display_id].maximize_window()
            self._on_log_line(f"SYS: Maximized DISPLAY {display_id}")
            return True
        self._on_log_line(f"SYS: DISPLAY {display_id} not found to maximize")
        return False

    def _on_minimize_display_win(self, display_id: int = 0) -> bool:
        if not self._displays:
            self._on_log_line("SYS: No active displays to minimize")
            return False
        if display_id == 0:
            for did, w in list(self._displays.items()):
                self._minimized_display_ids.add(did)
                w.hide()
            self._update_dots_overlay()
            self._on_log_line("SYS: Minimized all displays to left edge dock")
            return True
        elif display_id in self._displays:
            self._minimized_display_ids.add(display_id)
            self._displays[display_id].hide()
            self._update_dots_overlay()
            self._on_log_line(f"SYS: Minimized DISPLAY {display_id} to left edge dock")
            return True
        self._on_log_line(f"SYS: DISPLAY {display_id} not found to minimize")
        return False

    def _on_close_display_win(self, display_id: int):
        if display_id == 0:
            for w in list(self._displays.values()):
                try:
                    w.close()
                except Exception:
                    pass
            self._displays.clear()
            self._minimized_display_ids.clear()
            self._reserved_display_ids.clear()
            self._display_counter = 0
            self._update_dots_overlay()
            self._on_log_line("SYS: Closed all display windows")
            return True
        elif display_id in self._displays:
            self._reserved_display_ids.discard(display_id)
            self._minimized_display_ids.discard(display_id)
            try:
                self._displays[display_id].close()
            except Exception:
                pass
            if not self._displays:
                self._display_counter = 0
                self._reserved_display_ids.clear()
            self._update_dots_overlay()
            self._on_log_line(f"SYS: Closed DISPLAY {display_id}")
            return True
        return False

    def _toggle_fullscreen(self):
        self._is_fullscreen = not getattr(self, "_is_fullscreen", False)
        if self._is_fullscreen:
            self._prev_geom = self.geometry()
            self._was_expanded = self._expanded
            if hasattr(self, "hud_ctrl_bar"):
                self.hud_ctrl_bar.setVisible(False)
            screen = QApplication.primaryScreen().geometry()
            self.setGeometry(screen)
            self.hud.set_big_mode(True)
        else:
            if hasattr(self, "hud_ctrl_bar"):
                self.hud_ctrl_bar.setVisible(True)
            self.hud.set_big_mode(False)
            target = getattr(self, "_normal_geom", None) or getattr(self, "_prev_geom", None)
            if not target:
                screen = QApplication.primaryScreen().availableGeometry()
                target = QRect(screen.right() - 440, screen.bottom() - 610, 420, 580)
            self.setGeometry(target)

    def _on_log_line(self, text: str):
        self._log.append_log(text)
        if hasattr(self, "bottom_chat_dock") and hasattr(self.bottom_chat_dock, "_dock_log"):
            self.bottom_chat_dock._dock_log.append_log(text)
        asst = getattr(self, "_assistant_name", "JARVIS").upper()
        if text.startswith("You: "):
            self.hud.set_user_text(text[5:].strip())
        elif text.startswith(f"{asst}: ") or text.startswith("JARVIS: "):
            idx = text.find(": ")
            self.hud.set_reply_text(text[idx + 2 :].strip())
        elif any(text.startswith(p) for p in ("SYS: ", "ACT: ", "TOOL: ", "ERR: ")):
            parts = text.split(":", 1)
            if len(parts) > 1:
                clean = parts[1].strip().lstrip("▶ 🎤 🔊 🛑 🔇 🎙 ").strip()
                if clean and len(clean) < 50:
                    self.hud.add_card(clean[:32])

    def _on_stream_user(self, text: str):
        if hasattr(self, "hud") and hasattr(self.hud, "stream_user_text"):
            self.hud.stream_user_text(text)

    def _on_stream_reply(self, text: str):
        if hasattr(self, "hud") and hasattr(self.hud, "stream_reply_text"):
            self.hud.stream_reply_text(text)

    def mousePressEvent(self, e):
        if self._handle_mouse_press(e):
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._handle_mouse_move(e):
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._handle_mouse_release(e)
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and e.position().toPoint().y() <= 40:
            self.maximize_display()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def eventFilter(self, watched, event):
        targets = (
            getattr(self, "centralWidget", lambda: None)(),
            getattr(self, "_drawer", None),
            getattr(self, "hud_box", None),
            getattr(self, "hud", None),
        )
        if watched in targets and watched is not None:
            if self._filter_child_mouse_event(watched, event):
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            if getattr(self, "_is_fullscreen", False):
                self._toggle_fullscreen()
                return
            elif self._expanded:
                self._toggle_expanded(False)
                return
            elif self.on_interrupt:
                self.on_interrupt()
                return
        super().keyPressEvent(e)

    # --- Video in the HUD area ---------------------------------------------
    #
    # Everything below runs on the Qt thread. Plugins and the assistant reach it
    # through the signals above, the same way every other panel is driven, so a
    # background thread never touches a widget.
    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = self.centralWidget()
        if getattr(self, "_overlay", None) and self._overlay.isVisible():
            ow, oh = 460, 390
            self._overlay.setGeometry(
                (cw.width() - ow) // 2,
                (cw.height() - oh) // 2,
                ow,
                oh,
            )
        if getattr(self, "_remote_overlay", None) and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            self._remote_overlay.setGeometry(
                (cw.width() - ow) // 2,
                (cw.height() - oh) // 2,
                ow,
                oh,
            )
        if getattr(self, "_customize_overlay", None) and self._customize_overlay.isVisible():
            ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
            self._customize_overlay.setGeometry(
                (cw.width() - ow) // 2,
                (cw.height() - oh) // 2,
                ow,
                oh,
            )
        if getattr(self, "_confirm_overlay", None) and self._confirm_overlay.isVisible():
            ov = self._confirm_overlay
            ov.setGeometry(
                max(0, (cw.width() - ov.width()) // 2),
                max(0, (cw.height() - ov.height()) // 2),
                ov.width(),
                ov.height(),
            )
        # Clipboard panel — bottom-center (triggers removed)
        if getattr(self, "_clipboard_panel", None) is not None and self._clipboard_panel.isVisible():
            self._position_clipboard_panel()
        # Quick drawer — reposition if open
        if hasattr(self, "_quick_drawer") and self._quick_drawer.isVisible():
            self._position_quick_drawer()
        if hasattr(self, "_ctrl_drawer") and self._ctrl_drawer.isVisible():
            self._position_ctrl_drawer()
        self._fit_video()

    def _update_metrics(self):
        snap = _metrics.snapshot()

        # CPU
        cpu = snap["cpu"]
        self._bar_cpu.set_value(cpu, f"{cpu:.0f}%")

        # MEM
        mem = snap["mem"]
        self._bar_mem.set_value(mem, f"{mem:.0f}%")

        # NET
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net * 1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"
        net_pct = min(100, net * 10)  # 10 MB/s = %100
        self._bar_net.set_value(net_pct, net_str)

        # GPU
        gpu = snap["gpu"]
        if gpu >= 0:
            self._bar_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._bar_gpu.set_value(0, "N/A")

        # TMP
        tmp = snap["tmp"]
        if tmp >= 0:
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.set_value(0, "N/A")

        try:
            boot_t = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UP  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("UP  --:--")

        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(f"PROC  {proc_count}")
        except Exception:
            self._proc_lbl.setText("PROC  --")

        try:
            self.hud.set_metrics(cpu, mem, "LIVE" if getattr(self, "_ready", True) else "OFFLINE")
        except Exception:
            pass

        if hasattr(self, "_armoury_telemetry") and self._armoury_telemetry:
            try:
                self._armoury_telemetry.update_metrics(snap)
            except Exception:
                pass

    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(54)
        w.setStyleSheet(f"background: {C.DARK}; border-bottom: 1px solid {C.BORDER_B};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)

        def _badge(txt, color=C.TEXT_MED):
            l = QLabel(txt)
            l.setFont(QFont("Montserrat", 8))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_badge(APP_VERSION, C.PRI_DIM))
        lay.addSpacing(8)
        self._drawer_btn = QPushButton("⚙")
        self._drawer_btn.setFixedSize(26, 26)
        self._drawer_btn.setFont(QFont("Montserrat", 11))
        self._drawer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._drawer_btn.setToolTip("Settings & Controls")
        self._drawer_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 4px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI_DIM}; }}
            QPushButton:checked {{ color: {C.PRI}; border-color: {C.PRI}; background: {C.PRI_GHO}; }}
        """)
        self._drawer_btn.setCheckable(True)
        self._drawer_btn.setToolTip("Setup — things you set once")
        self._drawer_btn.clicked.connect(self._toggle_drawer)
        lay.addWidget(self._drawer_btn)

        # A SECOND drawer, and the split is by how often a thing is touched.
        #
        # One panel held twelve buttons: the ones you press once when you set
        # the assistant up sitting next to the ones you flick on and off every
        # day. Separating them by that — setup behind ⚙, everyday behind 🎛 —
        # is what makes each list short enough to read at a glance.
        self._ctrl_btn = QPushButton("🎛")
        self._ctrl_btn.setFixedSize(26, 26)
        self._ctrl_btn.setFont(QFont("Montserrat", 11))
        self._ctrl_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ctrl_btn.setToolTip("Controls — the everyday switches")
        self._ctrl_btn.setStyleSheet(self._drawer_btn.styleSheet())
        self._ctrl_btn.setCheckable(True)
        self._ctrl_btn.clicked.connect(self._toggle_controls)
        lay.addSpacing(4)
        lay.addWidget(self._ctrl_btn)
        lay.addStretch()

        mid = QVBoxLayout()
        mid.setSpacing(1)
        _disp = self._assistant_name.upper()
        self._title_lbl = QLabel(_disp)
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setFont(QFont("Montserrat", 17, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        mid.addWidget(self._title_lbl)
        _sub_text = "A Friendly Assistant" if _disp in ("JARVIS", "J.A.R.V.I.S") else "Personal AI Assistant"
        self._sub_lbl = QLabel(_sub_text)
        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_lbl.setFont(QFont("Montserrat", 7))
        self._sub_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        mid.addWidget(self._sub_lbl)
        lay.addLayout(mid)
        lay.addStretch()

        right_col = QVBoxLayout()
        right_col.setSpacing(2)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(QFont("Montserrat", 14, QFont.Weight.Bold))
        self._clock_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont("Montserrat", 7))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._date_lbl)
        lay.addLayout(right_col)
        return w

    def _tick_clock(self):
        if hasattr(self, "_clock_lbl"):
            self._clock_lbl.setText(time.strftime("%I:%M:%S %p"))
        if hasattr(self, "_date_lbl"):
            self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-right: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(6)

        hdr = QLabel("◈ SYS MONITOR")
        hdr.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(
            f"color: {C.PRI}; background: transparent; border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;"
        )
        lay.addWidget(hdr)
        lay.addSpacing(2)

        self._bar_cpu = MetricBar("CPU", C.PRI)
        self._bar_mem = MetricBar("MEM", C.ACC2)
        self._bar_net = MetricBar("NET", C.GREEN)
        self._bar_gpu = MetricBar("GPU", C.ACC)
        self._bar_tmp = MetricBar("TMP", "#ff4444")

        for bar in [self._bar_cpu, self._bar_mem, self._bar_net, self._bar_gpu, self._bar_tmp]:
            lay.addWidget(bar)

        lay.addSpacing(4)

        info_panel = QWidget()
        info_panel.setStyleSheet(f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px;")
        ip_lay = QVBoxLayout(info_panel)
        ip_lay.setContentsMargins(6, 5, 6, 5)
        ip_lay.setSpacing(3)

        self._uptime_lbl = QLabel("UP  --:--")
        self._uptime_lbl.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        self._uptime_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none;")
        ip_lay.addWidget(self._uptime_lbl)

        self._proc_lbl = QLabel("PROC  --")
        self._proc_lbl.setFont(QFont("Montserrat", 8))
        self._proc_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(self._proc_lbl)

        os_name = {"Windows": "WIN", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        os_lbl = QLabel(f"OS  {os_name}")
        os_lbl.setFont(QFont("Montserrat", 8))
        os_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent; border: none;")
        ip_lay.addWidget(os_lbl)

        lay.addWidget(info_panel)
        lay.addSpacing(4)

        lay.addStretch()

        for txt, col in [
            ("AI CORE\nACTIVE", C.GREEN),
            ("SEC\nCLEARED", C.PRI),
            ("PROTOCOL\n" + APP_PROTOCOL, C.TEXT_DIM),
        ]:
            lbl = QLabel(txt)
            lbl.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                f"color: {col}; background: {C.PANEL2};"
                f"border: 1px solid {C.BORDER_A}; border-radius: 3px; padding: 4px;"
            )
            lay.addWidget(lbl)

        return w

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-left: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        def _sec(txt):
            l = QLabel(f"▸ {txt}")
            l.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
            l.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            return l

        lay.addWidget(_sec("ACTIVITY LOG"))
        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_sec("FILE UPLOAD"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded — drop or click above to upload")
        self._file_hint.setFont(QFont("Montserrat", 7))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_sec("COMMAND INPUT"))
        lay.addLayout(self._build_input_row())

        self._interrupt_btn = QPushButton("✋  INTERRUPT  [ESC]")
        self._interrupt_btn.setFixedHeight(34)
        self._interrupt_btn.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._interrupt_btn.setStyleSheet(f"""
            QPushButton {{
                background: #140008; color: {C.MUTED_C};
                border: 1px solid {C.MUTED_C}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: #180204; border: 1px solid #ff1744;
            }}
            QPushButton:pressed {{
                background: #300018;
            }}
        """)
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        lay.addWidget(self._interrupt_btn)

        self._mute_btn = QPushButton("🎙  MICROPHONE ACTIVE")
        self._mute_btn.setFixedHeight(30)
        self._mute_btn.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)

        return w

    def _build_quick_drawer(self) -> QWidget:
        """Floating overlay panel shown when the ⚙ header button is toggled."""
        _BTN_STYLE_PRI = f"""
            QPushButton {{
                background: #00091a; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """
        _BTN_STYLE_DIM = f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """

        self._BTN_PRI, self._BTN_DIM = _BTN_STYLE_PRI, _BTN_STYLE_DIM
        w = QWidget(self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.setStyleSheet(f"""
            QWidget#QuickDrawer {{
                background: {C.DARK};
                border: 1px solid {C.BORDER_B};
                border-top: none;
                border-radius: 0 0 6px 6px;
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(5)

        hdr = QLabel("◈ SETUP")
        hdr.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(
            f"color: {C.PRI_DIM}; background: transparent; border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;"
        )
        lay.addWidget(hdr)

        remote_btn = QPushButton("◉  REMOTE CONTROL")
        remote_btn.setFixedHeight(30)
        remote_btn.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remote_btn.setStyleSheet(_BTN_STYLE_PRI)
        remote_btn.clicked.connect(self._open_remote)
        lay.addWidget(remote_btn)

        sc_btn = QPushButton("⊞  CREATE DESKTOP SHORTCUT")
        sc_btn.setFixedHeight(26)
        sc_btn.setFont(QFont("Montserrat", 7))
        sc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sc_btn.setStyleSheet(_BTN_STYLE_DIM)
        sc_btn.clicked.connect(self._create_desktop_shortcut)
        lay.addWidget(sc_btn)

        self._autostart_btn = QPushButton("◉  AUTO-START: OFF")
        self._autostart_btn.setFixedHeight(26)
        self._autostart_btn.setFont(QFont("Montserrat", 7))
        self._autostart_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._autostart_btn.clicked.connect(self._toggle_autostart)
        lay.addWidget(self._autostart_btn)

        cust_btn = QPushButton("⚙  CUSTOMISE ASSISTANT")
        cust_btn.setFixedHeight(26)
        cust_btn.setFont(QFont("Montserrat", 7))
        cust_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cust_btn.setStyleSheet(_BTN_STYLE_DIM)
        cust_btn.clicked.connect(self._open_customize)
        lay.addWidget(cust_btn)

        audio_btn = QPushButton("🎧  AUDIO DEVICES")
        audio_btn.setFixedHeight(26)
        audio_btn.setFont(QFont("Montserrat", 7))
        audio_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        audio_btn.setStyleSheet(_BTN_STYLE_DIM)
        audio_btn.clicked.connect(self._open_audio_devices)
        lay.addWidget(audio_btn)

        mem_btn = QPushButton("🧠  MEMORY")
        mem_btn.setFixedHeight(26)
        mem_btn.setFont(QFont("Montserrat", 7))
        mem_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        mem_btn.setStyleSheet(_BTN_STYLE_DIM)
        mem_btn.clicked.connect(self._open_memory_panel)
        lay.addWidget(mem_btn)

        plugin_btn = QPushButton("🧩  PLUGINS")
        plugin_btn.setFixedHeight(26)
        plugin_btn.setFont(QFont("Montserrat", 7))
        plugin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        plugin_btn.setStyleSheet(_BTN_STYLE_DIM)
        plugin_btn.clicked.connect(self._open_plugin_manager)
        lay.addWidget(plugin_btn)

        settings_btn = QPushButton("⚙  PLUGIN SETTINGS")
        settings_btn.setFixedHeight(26)
        settings_btn.setFont(QFont("Montserrat", 7))
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.setStyleSheet(_BTN_STYLE_DIM)
        settings_btn.clicked.connect(self._open_plugin_settings)
        lay.addWidget(settings_btn)

        # The button is built here with the rest of the drawer; the panel behind
        # it — and the whole providers package — is imported on the click. The
        # drawer used to stutter for a measured 2.104 seconds because a state
        # check imported openwakeword while it was being built, and adding a
        # second thing that does that would undo the fix rather than repeat it.
        prov_btn = QPushButton("🔌  ENGINE && PROVIDERS")
        prov_btn.setFixedHeight(26)
        prov_btn.setFont(QFont("Montserrat", 7))
        prov_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        prov_btn.setStyleSheet(_BTN_STYLE_DIM)
        prov_btn.clicked.connect(self._open_providers)
        lay.addWidget(prov_btn)

        w.adjustSize()
        return w

    def _build_controls_drawer(self) -> QWidget:
        """The switches that get flicked every day, in their own panel."""
        w = QWidget(self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.setStyleSheet(self._quick_drawer.styleSheet())
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(5)

        hdr = QLabel("◈ CONTROLS")
        hdr.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(
            f"color: {C.PRI_DIM}; background: transparent; border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;"
        )
        lay.addWidget(hdr)

        def _row(button, style=None, height=26):
            button.setFixedHeight(height)
            button.setFont(QFont("Montserrat", 7))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if style:
                button.setStyleSheet(style)
            lay.addWidget(button)
            return button

        fs_btn = _row(QPushButton("⛶  FULLSCREEN  [F11]"), self._BTN_DIM)
        fs_btn.clicked.connect(self._toggle_fullscreen)

        self._brief_btn = _row(QPushButton())
        self._brief_btn.clicked.connect(self._toggle_brief)

        self._wake_btn = _row(QPushButton())
        self._wake_btn.clicked.connect(self._toggle_wake_word)
        self._wake_sleep_btn = _row(QPushButton())
        self._wake_sleep_btn.clicked.connect(self._tap_wake_manual)
        # Neutral until the real state lands from the background warm-up.
        self._wake_btn.setText("🎙  WAKE WORD")
        self._wake_btn.setStyleSheet(self._BTN_DIM)
        self._wake_sleep_btn.hide()

        self._auto_sleep_btn = _row(QPushButton())
        self._auto_sleep_btn.clicked.connect(self._toggle_auto_sleep)
        self._auto_sleep_btn.setText("💤  AUTO-SLEEP")
        self._auto_sleep_btn.setStyleSheet(self._BTN_DIM)

        self._routing_btn = _row(QPushButton())
        self._routing_btn.clicked.connect(self._cycle_routing_mode)
        self._refresh_routing_btn()

        self._ptt_btn = _row(QPushButton())
        self._ptt_btn.clicked.connect(self._toggle_ptt)
        self._refresh_talk_btns()

        self._hud_btn = _row(QPushButton())
        self._hud_btn.clicked.connect(self._toggle_hud_style)
        self._refresh_hud_btn()

        w.adjustSize()
        return w

    def _warm_wake_state(self) -> None:
        """Work out the wake-word state off the UI thread, once.

        It used to be resolved when the drawer opened, and that made opening the
        drawer take two seconds the first time — measured at 2.10s, all of it
        `import openwakeword` dragging in onnxruntime behind it. The button
        count had nothing to do with it; the Qt thread was simply waiting on an
        import. Afterwards the same check costs about a millisecond, so it only
        ever needed to happen somewhere other than in front of the user.
        """

        def work():
            try:
                self._wake_state()
            except Exception:
                pass
            self._wake_btns_sig.emit()

        threading.Thread(target=work, daemon=True, name="wake-state-warm").start()

    def _toggle_drawer(self, checked: bool):
        if checked:
            self._close_controls()
            self._position_quick_drawer()
            self._quick_drawer.show()
            self._quick_drawer.raise_()
        else:
            self._quick_drawer.hide()

    def _toggle_controls(self, checked: bool):
        if checked:
            self._close_setup()
            self._refresh_wake_btns()  # cheap now: the state was warmed at boot
            self._position_ctrl_drawer()
            self._ctrl_drawer.show()
            self._ctrl_drawer.raise_()
        else:
            self._ctrl_drawer.hide()

    def _close_setup(self):
        if hasattr(self, "_quick_drawer"):
            self._quick_drawer.hide()
        if hasattr(self, "_drawer_btn"):
            self._drawer_btn.setChecked(False)

    def _close_controls(self):
        if hasattr(self, "_ctrl_drawer"):
            self._ctrl_drawer.hide()
        if hasattr(self, "_ctrl_btn"):
            self._ctrl_btn.setChecked(False)

    def _place_drawer(self, drawer, left: int):
        _W = 220
        drawer.setFixedWidth(_W)
        drawer.adjustSize()
        drawer.setGeometry(left, 54, _W, drawer.sizeHint().height())

    def _position_quick_drawer(self):
        if hasattr(self, "_quick_drawer"):
            self._place_drawer(self._quick_drawer, 12)

    def _position_ctrl_drawer(self):
        # Under its own header button rather than under the other one's.
        if hasattr(self, "_ctrl_drawer"):
            left = 12
            try:
                left = max(12, self._ctrl_btn.mapTo(self.centralWidget(), QPoint(0, 0)).x() - 8)
            except Exception:
                pass
            self._place_drawer(self._ctrl_drawer, left)
