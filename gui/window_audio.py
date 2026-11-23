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


class WindowAudioMixin:
    """Cyber drawer and audio management for MainWindow."""

    def _build_hud_controls_bar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(36)
        bar.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(6, 0, 6, 4)
        lay.setSpacing(4)

        def _btn(txt, tip, icon_name=None):
            btn_txt = f" {txt.strip()}" if icon_name and txt.strip() else txt
            b = QPushButton(btn_txt)
            b.setFont(keena_font(8, True))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            if icon_name:
                b.setIcon(get_uicon(icon_name, color=C.PRI, size=13))
                b.setIconSize(QSize(13, 13))
            b.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(24, 4, 8, 220);
                    color: {C.TEXT};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 4px;
                    padding: 3px 8px;
                }}
                QPushButton:hover {{
                    background: rgba(255, 23, 68, 0.25);
                    border: 1px solid {C.PRI};
                    color: #ffffff;
                }}
                QPushButton:pressed {{
                    background: rgba(255, 23, 68, 0.45);
                }}
            """)
            return b

        self._hud_mute_btn = _btn("MIC", "Mute/Unmute microphone (F4)", "circle-microphone")
        self._hud_mute_btn.clicked.connect(self._toggle_mute)
        lay.addWidget(self._hud_mute_btn)

        self._hud_exp_btn = _btn("EXPAND", "Expand attached JARVIS Intelligence Matrix", "angle-small-down")
        self._hud_exp_btn.clicked.connect(lambda: self._toggle_expanded())
        lay.addWidget(self._hud_exp_btn)

        self._hud_chat_btn = _btn("CHAT", "Toggle Bottom Chat Log Dock", "comment")
        self._hud_chat_btn.clicked.connect(self.toggle_chat_dock)
        lay.addWidget(self._hud_chat_btn)

        self._hud_mini_btn = _btn("MINI", "Minimize Arc Reactor to bottom-left (Favicon size)", "mini")
        self._hud_mini_btn.clicked.connect(self.toggle_minimize_top)
        lay.addWidget(self._hud_mini_btn)

        self._hud_close_btn = _btn("CLOSE", "Close JARVIS Display", "cross")
        self._hud_close_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(35, 6, 10, 210);
                color: #ff5252;
                border: 1px solid rgba(255, 23, 68, 0.4);
                border-radius: 4px;
                padding: 3px 8px;
            }}
            QPushButton:hover {{
                background: rgba(255, 23, 68, 0.45);
                color: #ffffff;
                border-color: #ff1744;
            }}
        """)
        self._hud_close_btn.clicked.connect(self.close_display)
        lay.addWidget(self._hud_close_btn)

        return bar

    def _tab_btn_style(self, active: bool) -> str:
        if active:
            return f"""
                QPushButton {{
                    background: rgba(255, 23, 68, 0.25);
                    color: #ffffff;
                    border: 1px solid {C.PRI};
                    border-radius: 3px;
                    padding: 5px 8px;
                }}
            """
        return f"""
            QPushButton {{
                background: rgba(20, 5, 18, 180);
                color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
                padding: 5px 8px;
            }}
            QPushButton:hover {{
                background: rgba(255, 23, 68, 0.12);
                color: {C.TEXT};
                border-color: {C.BORDER_B};
            }}
        """

    def _build_cyber_drawer(self) -> QWidget:
        w = QWidget()
        w.setMinimumWidth(380)
        w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        w.setStyleSheet(f"""
            QWidget#CyberDrawer {{
                background: rgba(14, 4, 12, 238);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        w.setObjectName("CyberDrawer")

        v = QVBoxLayout(w)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(8)

        # Header
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        title = QLabel("JARVIS INTELLIGENCE MATRIX")
        title.setFont(keena_font(9, True, 2))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()

        def _drawer_btn(txt, tip, cb, icon_name=None):
            btn_txt = f" {txt.strip()}" if icon_name and txt.strip() else txt
            b = QPushButton(btn_txt)
            b.setFont(keena_font(8, True))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            if icon_name:
                b.setIcon(get_uicon(icon_name, color=C.PRI, size=13))
                b.setIconSize(QSize(13, 13))
            b.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(24, 4, 8, 220);
                    color: {C.TEXT};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 3px;
                    padding: 3px 8px;
                }}
                QPushButton:hover {{
                    background: rgba(255, 23, 68, 0.25);
                    border: 1px solid {C.PRI};
                    color: #ffffff;
                }}
            """)
            b.clicked.connect(cb)
            return b

        self._drawer_rem_btn = _drawer_btn(
            "REMIND", "Open Reminders Management Matrix", lambda: self._show_reminders("upcoming"), "calendar"
        )
        self._hud_rem_btn = self._drawer_rem_btn
        hdr.addWidget(self._drawer_rem_btn)

        self._drawer_mem_btn = _drawer_btn(
            "MEM/ACT", "Open Memory & Actions Platform", lambda: self._show_memory_actions("memory"), "database"
        )
        self._hud_mem_btn = self._drawer_mem_btn
        hdr.addWidget(self._drawer_mem_btn)

        btn_close = QPushButton("")
        btn_close.setIcon(get_uicon("angle-small-up", color=C.TEXT_DIM, size=14))
        btn_close.setIconSize(QSize(14, 14))
        btn_close.setFont(keena_font(10, True))
        btn_close.setFixedSize(24, 24)
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setToolTip("Collapse drawer into compact HUD")
        btn_close.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI}; }}
        """)
        btn_close.clicked.connect(lambda: self._toggle_expanded(False))
        hdr.addWidget(btn_close)
        v.addLayout(hdr)

        # Tabs row
        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(4)
        self._drawer_tabs = QStackedWidget()

        tab_names = [("CHAT & LOG", 0), ("TELEMETRY", 1), ("AUDIO CORE", 2), ("SETTINGS", 3), ("TASKS", 4)]
        self._tab_btns = []

        def _make_tab_btn(name, idx):
            btn = QPushButton(name)
            btn.setFont(keena_font(8, True, 1))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setCheckable(True)
            if idx == 0:
                btn.setChecked(True)

            def _on_click():
                for b in self._tab_btns:
                    b.setChecked(False)
                    b.setStyleSheet(self._tab_btn_style(False))
                btn.setChecked(True)
                btn.setStyleSheet(self._tab_btn_style(True))
                self._drawer_tabs.setCurrentIndex(idx)
                if idx == 2:
                    self._refresh_audio_tab()
                elif idx == 4:
                    self._refresh_tasks_tab()

            btn.clicked.connect(_on_click)
            btn.setStyleSheet(self._tab_btn_style(idx == 0))
            return btn

        for name, idx in tab_names:
            b = _make_tab_btn(name, idx)
            self._tab_btns.append(b)
            tabs_row.addWidget(b)
        v.addLayout(tabs_row)

        # Tab 0: Chat & Log
        p0 = QWidget()
        p0.setStyleSheet("background: transparent;")
        p0_v = QVBoxLayout(p0)
        p0_v.setContentsMargins(0, 4, 0, 0)
        p0_v.setSpacing(6)

        # Full conversation and activity chat log
        self._log.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._log.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._log.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(12, 3, 11, 220);
                color: {C.TEXT};
                border: 1px solid {C.BORDER_B};
                border-radius: 4px;
                padding: 6px 8px;
            }}
            QScrollBar:vertical, QScrollBar:horizontal {{
                background: transparent;
                width: 0px;
                height: 0px;
                border: none;
            }}
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
                background: transparent;
                width: 0px;
                height: 0px;
            }}
        """)
        p0_v.addWidget(self._log, stretch=1)
        p0_v.addLayout(self._build_input_row())
        p0_v.addWidget(self._drop_zone)
        p0_v.addWidget(self._file_hint)
        self._drawer_tabs.addWidget(p0)

        # Tab 1: Telemetry (Armoury Crate hardware matrix)
        p1 = QWidget()
        p1.setStyleSheet("background: transparent;")
        p1_v = QVBoxLayout(p1)
        p1_v.setContentsMargins(0, 2, 0, 2)
        p1_v.setSpacing(4)
        self._armoury_telemetry = ArmouryCrateTelemetryWidget(self)
        p1_v.addWidget(self._armoury_telemetry, stretch=1)
        self._drawer_tabs.addWidget(p1)

        # Tab 2: Audio Core
        p2 = self._build_audio_tab()
        self._drawer_tabs.addWidget(p2)

        # Tab 3: Settings
        p3 = self._build_settings_tab()
        self._drawer_tabs.addWidget(p3)

        # Tab 4: Tasks & Habits
        p4 = self._build_tasks_tab()
        self._drawer_tabs.addWidget(p4)

        v.addWidget(self._drawer_tabs, stretch=1)
        return w

    def _build_tasks_tab(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 6, 4, 6)
        lay.setSpacing(8)

        # Header with Refresh button
        top = QHBoxLayout()
        hdr_lbl = QLabel("TODAY'S TASKS & HABITS")
        hdr_lbl.setFont(keena_font(8, True))
        hdr_lbl.setStyleSheet(f"color: {C.PRI}; letter-spacing: 1.5px;")
        top.addWidget(hdr_lbl, stretch=1)

        ref_btn = QPushButton("↻ REFRESH")
        ref_btn.setFont(keena_font(8, True))
        ref_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ref_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 255, 255, 0.05); color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER_B}; border-radius: 4px; padding: 2px 8px;
            }}
            QPushButton:hover {{ background: rgba(99, 102, 241, 0.2); color: {C.TEXT}; }}
        """)
        ref_btn.clicked.connect(self._refresh_tasks_tab)
        top.addWidget(ref_btn)
        lay.addLayout(top)

        # Scroll area for items
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background: transparent; border: none;")
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._tasks_scroll_content = QWidget()
        self._tasks_scroll_content.setStyleSheet("background: transparent;")
        self._tasks_layout = QVBoxLayout(self._tasks_scroll_content)
        self._tasks_layout.setContentsMargins(0, 0, 0, 0)
        self._tasks_layout.setSpacing(6)
        scroll.setWidget(self._tasks_scroll_content)

        lay.addWidget(scroll, stretch=1)
        return w

    def _refresh_tasks_tab(self) -> None:
        if not hasattr(self, "_tasks_layout"):
            return

        # Clear existing items
        while self._tasks_layout.count() > 0:
            item = self._tasks_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        try:
            from plugins.productivity import get_store
            from datetime import datetime as _dt
            store = get_store()
            data = store.load()
            tasks = list(data.get("tasks", {}).values())
            habits = list(data.get("habits", {}).values())
            now_str = _dt.now().strftime("%Y-%m-%d")

            today_tasks = [
                t for t in tasks
                if t.get("status") == "pending"
                and (not t.get("due") or t["due"].startswith(now_str) or t["due"].split("T")[0] < now_str)
            ]
            active_habits = [h for h in habits if not h.get("archived", False)]

            # Section: Tasks
            t_hdr = QLabel(f"PENDING TASKS ({len(today_tasks)})")
            t_hdr.setFont(keena_font(7, True))
            t_hdr.setStyleSheet(f"color: {C.TEXT_DIM}; letter-spacing: 1px;")
            self._tasks_layout.addWidget(t_hdr)

            if not today_tasks:
                empty_lbl = QLabel("No pending tasks due today.")
                empty_lbl.setFont(QFont("Montserrat", 9))
                empty_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; padding: 4px;")
                self._tasks_layout.addWidget(empty_lbl)
            else:
                for t in today_tasks:
                    row = QWidget()
                    row.setStyleSheet(f"background: rgba(255, 255, 255, 0.03); border: 1px solid {C.BORDER_B}; border-radius: 4px; padding: 4px;")
                    r_lay = QHBoxLayout(row)
                    r_lay.setContentsMargins(6, 4, 6, 4)

                    due_txt = t.get('due', 'None')[:16].replace('T', ' ')
                    info = QLabel(f"{t['title']}<br><span style='color:{C.TEXT_DIM};font-size:8pt;'>Due: {due_txt}</span>")
                    info.setTextFormat(Qt.TextFormat.RichText)
                    info.setFont(QFont("Montserrat", 9))
                    info.setStyleSheet(f"color: {C.TEXT};")
                    r_lay.addWidget(info, stretch=1)

                    done_btn = QPushButton("✓")
                    done_btn.setFixedSize(24, 24)
                    done_btn.setFont(QFont("Montserrat", 10, QFont.Weight.Bold))
                    done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    done_btn.setToolTip("Mark completed")
                    done_btn.setStyleSheet(f"""
                        QPushButton {{
                            background: rgba(34, 197, 94, 0.15); color: #22c55e;
                            border: 1px solid rgba(34, 197, 94, 0.3); border-radius: 4px;
                        }}
                        QPushButton:hover {{ background: rgba(34, 197, 94, 0.35); }}
                    """)
                    task_id = t["id"]
                    done_btn.clicked.connect(lambda _, tid=task_id: self._ui_complete_task(tid))
                    r_lay.addWidget(done_btn)
                    self._tasks_layout.addWidget(row)

            # Section: Habits
            h_hdr = QLabel(f"ACTIVE HABITS ({len(active_habits)})")
            h_hdr.setFont(keena_font(7, True))
            h_hdr.setStyleSheet(f"color: {C.TEXT_DIM}; letter-spacing: 1px; margin-top: 8px;")
            self._tasks_layout.addWidget(h_hdr)

            if not active_habits:
                empty_h = QLabel("No active habits tracked.")
                empty_h.setFont(QFont("Montserrat", 9))
                empty_h.setStyleSheet(f"color: {C.TEXT_DIM}; padding: 4px;")
                self._tasks_layout.addWidget(empty_h)
            else:
                for h in active_habits:
                    row = QWidget()
                    row.setStyleSheet(f"background: rgba(255, 255, 255, 0.03); border: 1px solid {C.BORDER_B}; border-radius: 4px; padding: 4px;")
                    r_lay = QHBoxLayout(row)
                    r_lay.setContentsMargins(6, 4, 6, 4)

                    logged_today = now_str in h.get("log", [])
                    info = QLabel(f"{h['name']}<br><span style='color:{C.TEXT_DIM};font-size:8pt;'>Streak: {h.get('streak', 0)}d | Target: {h.get('target', 'daily')}</span>")
                    info.setTextFormat(Qt.TextFormat.RichText)
                    info.setFont(QFont("Montserrat", 9))
                    info.setStyleSheet(f"color: {C.TEXT};")
                    r_lay.addWidget(info, stretch=1)

                    if logged_today:
                        badge = QLabel("✓ DONE")
                        badge.setFont(keena_font(7, True))
                        badge.setStyleSheet("color: #22c55e; padding: 2px 6px;")
                        r_lay.addWidget(badge)
                    else:
                        log_btn = QPushButton("✓")
                        log_btn.setFixedSize(24, 24)
                        log_btn.setFont(QFont("Montserrat", 10, QFont.Weight.Bold))
                        log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                        log_btn.setToolTip("Log habit completion today")
                        log_btn.setStyleSheet(f"""
                            QPushButton {{
                                background: rgba(99, 102, 241, 0.15); color: #818cf8;
                                border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 4px;
                            }}
                            QPushButton:hover {{ background: rgba(99, 102, 241, 0.35); }}
                        """)
                        hid = h["id"]
                        log_btn.clicked.connect(lambda _, hid=hid: self._ui_log_habit(hid))
                        r_lay.addWidget(log_btn)

                    self._tasks_layout.addWidget(row)

            self._tasks_layout.addStretch(1)
        except Exception as e:
            err_lbl = QLabel("Tasks panel: Productivity plugin not loaded")
            err_lbl.setFont(QFont("Montserrat", 8))
            err_lbl.setStyleSheet("color: #ef4444; padding: 6px;")
            self._tasks_layout.addWidget(err_lbl)

    def _ui_complete_task(self, task_id: str) -> None:
        try:
            from plugins.productivity import complete_task, get_store
            complete_task(get_store(), {"task_id": task_id})
            self._refresh_tasks_tab()
        except Exception as e:
            pass

    def _ui_log_habit(self, habit_id: str) -> None:
        try:
            from plugins.productivity import log_habit, get_store
            log_habit(get_store(), {"habit_id": habit_id})
            self._refresh_tasks_tab()
        except Exception as e:
            pass

    def _build_audio_tab(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 6, 4, 6)
        lay.setSpacing(10)

        def _hdr(txt):
            l = QLabel(txt)
            l.setFont(keena_font(8, True))
            l.setStyleSheet(f"color: {C.PRI}; border-bottom: 1px solid {C.BORDER}; padding-bottom: 3px;")
            return l

        lay.addWidget(_hdr("AUDIO STATUS"))
        self._audio_status_card = QFrame()
        self._audio_status_card.setStyleSheet(f"""
            QFrame {{
                background: {C.PANEL2};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 4px;
            }}
        """)
        st_lay = QVBoxLayout(self._audio_status_card)
        st_lay.setContentsMargins(6, 5, 6, 5)
        st_lay.setSpacing(3)

        def _st_lbl(txt: str) -> QLabel:
            lbl = QLabel(txt)
            lbl.setFont(keena_font(7))
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color: {C.TEXT_DIM}; border: none; background: transparent;")
            st_lay.addWidget(lbl)
            return lbl

        self._lbl_audio_mic = _st_lbl("MIC: Initializing...")
        self._lbl_audio_spk = _st_lbl("SPK: Initializing...")
        self._lbl_audio_bt = _st_lbl("BLUETOOTH: Disconnected")
        self._lbl_audio_rates = _st_lbl("RATES: IN 16000 Hz · OUT 24000 Hz")
        self._lbl_audio_state = _st_lbl("STATUS: Initializing · Switch: Idle")
        self._lbl_audio_routing = _st_lbl("ROUTING: Automatic Hybrid · A2DP Stereo: Restored")
        self._lbl_audio_dest = _st_lbl("AUDIO DESTINATION: Initializing...")
        lay.addWidget(self._audio_status_card)

        lay.addWidget(_hdr("AUDIO ROUTING ARCHITECTURE"))
        self._combo_routing = QComboBox()
        self._combo_routing.setStyleSheet(f"""
            QComboBox {{
                background: {C.PANEL2}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 5px;
            }}
            QComboBox::drop-down {{ border: none; }}
        """)
        from memory.config_manager import (
            get_audio_routing_mode,
            ROUTING_MODE_AUTO_HYBRID,
            ROUTING_MODE_BUILTIN_ONLY,
            ROUTING_MODE_BLUETOOTH_ONLY,
        )

        self._combo_routing.addItem("Automatic Hybrid (OPPO Earbuds + Laptop Wake Mic)", ROUTING_MODE_AUTO_HYBRID)
        self._combo_routing.addItem("Laptop Microphone Only (Preserves Stereo Always)", ROUTING_MODE_BUILTIN_ONLY)
        self._combo_routing.addItem("OPPO Microphone Only (Continuous Hands-Free)", ROUTING_MODE_BLUETOOTH_ONLY)
        cur_rm = get_audio_routing_mode()
        idx_rm = self._combo_routing.findData(cur_rm)
        self._combo_routing.setCurrentIndex(idx_rm if idx_rm >= 0 else 0)
        self._combo_routing.currentIndexChanged.connect(self._on_routing_tab_changed)
        lay.addWidget(self._combo_routing)

        lay.addWidget(_hdr("AUDIO INPUT (MICROPHONE)"))
        self._combo_mic = QComboBox()
        self._combo_mic.setStyleSheet(f"""
            QComboBox {{
                background: {C.PANEL2}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 5px;
            }}
            QComboBox::drop-down {{ border: none; }}
        """)
        try:
            from core.audio_devices import DEFAULT_LABEL
            from memory.config_manager import get_input_device

            cur_in = get_input_device() or DEFAULT_LABEL
            self._combo_mic.addItem(DEFAULT_LABEL)
            if cur_in != DEFAULT_LABEL:
                self._combo_mic.addItem(cur_in)
                self._combo_mic.setCurrentText(cur_in)
        except Exception:
            self._combo_mic.addItem("System default")
        self._combo_mic.currentTextChanged.connect(self._on_mic_combo_changed)
        lay.addWidget(self._combo_mic)

        lay.addWidget(_hdr("AUDIO OUTPUT (SPEAKERS)"))
        self._combo_spk = QComboBox()
        self._combo_spk.setStyleSheet(self._combo_mic.styleSheet())
        try:
            from core.audio_devices import DEFAULT_LABEL
            from memory.config_manager import get_output_device

            cur_out = get_output_device() or DEFAULT_LABEL
            self._combo_spk.addItem(DEFAULT_LABEL)
            if cur_out != DEFAULT_LABEL:
                self._combo_spk.addItem(cur_out)
                self._combo_spk.setCurrentText(cur_out)
        except Exception:
            self._combo_spk.addItem("System default")
        self._combo_spk.currentTextChanged.connect(self._on_spk_combo_changed)
        lay.addWidget(self._combo_spk)

        lay.addWidget(_hdr("VOICE PERSONA"))
        self._combo_voice = QComboBox()
        self._combo_voice.setStyleSheet(self._combo_mic.styleSheet())
        self._combo_voice.addItems(["Puck", "Charon", "Kore", "Fenrir", "Aoede"])
        self._combo_voice.currentTextChanged.connect(self._on_voice_combo_changed)
        lay.addWidget(self._combo_voice)

        lay.addWidget(_hdr("WAKE ENGINE & SHORTCUTS"))
        self._wake_toggle_btn = QPushButton("WAKE WORD: DISABLED (CLICK TO TOGGLE)")
        self._wake_toggle_btn.setFont(keena_font(8, True))
        self._wake_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._wake_toggle_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER_A}; border-radius: 3px; padding: 6px;
            }}
            QPushButton:hover {{ border-color: {C.PRI}; color: {C.TEXT}; }}
        """)
        self._wake_toggle_btn.clicked.connect(self._ui_toggle_wake)
        lay.addWidget(self._wake_toggle_btn)

        self._auto_sleep_toggle_btn = QPushButton("AUTO-SLEEP: ENABLED (CLICK TO TOGGLE)")
        self._auto_sleep_toggle_btn.setFont(keena_font(8, True))
        self._auto_sleep_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._auto_sleep_toggle_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER_A}; border-radius: 3px; padding: 6px;
            }}
            QPushButton:hover {{ border-color: {C.PRI}; color: {C.TEXT}; }}
        """)
        self._auto_sleep_toggle_btn.clicked.connect(self._toggle_auto_sleep)
        lay.addWidget(self._auto_sleep_toggle_btn)

        ptt_info = QLabel("PUSH-TO-TALK: Hold Space or Alt to speak")
        ptt_info.setFont(keena_font(8))
        ptt_info.setStyleSheet(f"color: {C.TEXT_DIM};")
        lay.addWidget(ptt_info)

        lay.addStretch()
        return w

    def _on_voice_combo_changed(self, voice_name: str):
        try:
            from memory import config_manager

            config_manager.save_voice(voice_name)
        except Exception as exc:
            print(f"[UI] Voice save failed: {exc}")
        if self.on_voice_change:
            self.on_voice_change()

    def _on_mic_combo_changed(self, mic_name: str):
        from core.audio_devices import DEFAULT_LABEL

        val = "" if mic_name == DEFAULT_LABEL else mic_name

        def _apply():
            try:
                from core.audio_manager import get_audio_manager
                from memory import config_manager

                mgr = get_audio_manager()
                if mgr is not None:
                    mgr.switch_input(val, persist=True, set_os_default=False)
                else:
                    config_manager.save_input_device(val)
                    if self.on_audio_device_change:
                        self.on_audio_device_change()
            except Exception as exc:
                print(f"[UI] Mic switch failed: {exc}")

        threading.Thread(target=_apply, daemon=True, name="ui-mic-switch").start()

    def _on_spk_combo_changed(self, spk_name: str):
        from core.audio_devices import DEFAULT_LABEL

        val = "" if spk_name == DEFAULT_LABEL else spk_name

        def _apply():
            try:
                from core.audio_manager import get_audio_manager
                from memory import config_manager

                mgr = get_audio_manager()
                if mgr is not None:
                    mgr.switch_output(val, persist=True, set_os_default=bool(val))
                else:
                    config_manager.save_output_device(val)
                    if self.on_audio_device_change:
                        self.on_audio_device_change()
            except Exception as exc:
                print(f"[UI] Speaker switch failed: {exc}")

        threading.Thread(target=_apply, daemon=True, name="ui-spk-switch").start()

    def _apply_audio_status(self, status: dict):
        """Update the UI audio status indicators immediately after a verified stream event."""
        if not isinstance(status, dict):
            return
        self._last_audio_status = dict(status)

        def _badge_color(st: str) -> str:
            low = str(st or "").lower()
            if "failed" in low:
                return C.MUTED_C
            if "disconnected" in low:
                return "#f4b942"
            if "switching" in low or "initializing" in low:
                return "#38BDF8"
            if "default" in low:
                return "#f4b942"
            return C.GREEN

        in_dev = status.get("input_device", "System default")
        out_dev = status.get("output_device", "System default")
        in_st = status.get("input_status", "Initializing")
        out_st = status.get("output_status", "Initializing")
        bt_st = status.get("bluetooth_status", "Disconnected")
        in_sr = status.get("input_sample_rate", 16000)
        out_sr = status.get("output_sample_rate", 24000)
        init_st = status.get("init_status", "Initializing")
        sw_st = status.get("switching_status", "Idle")

        if hasattr(self, "_lbl_audio_mic") and self._lbl_audio_mic:
            self._lbl_audio_mic.setText(f"MIC: {in_dev}  [{in_st}]")
            self._lbl_audio_mic.setStyleSheet(f"color: {_badge_color(in_st)}; border: none; background: transparent;")
        if hasattr(self, "_lbl_audio_spk") and self._lbl_audio_spk:
            self._lbl_audio_spk.setText(f"SPK: {out_dev}  [{out_st}]")
            self._lbl_audio_spk.setStyleSheet(f"color: {_badge_color(out_st)}; border: none; background: transparent;")
        if hasattr(self, "_lbl_audio_bt") and self._lbl_audio_bt:
            bt_col = C.GREEN if status.get("bluetooth_connected") else C.TEXT_DIM
            self._lbl_audio_bt.setText(f"BLUETOOTH: {bt_st}")
            self._lbl_audio_bt.setStyleSheet(f"color: {bt_col}; border: none; background: transparent;")
        if hasattr(self, "_lbl_audio_rates") and self._lbl_audio_rates:
            self._lbl_audio_rates.setText(f"RATES: IN {in_sr} Hz · OUT {out_sr} Hz")
        if hasattr(self, "_lbl_audio_state") and self._lbl_audio_state:
            self._lbl_audio_state.setText(f"INIT: {init_st} · SWITCH: {sw_st}")
        if hasattr(self, "_lbl_audio_routing") and self._lbl_audio_routing:
            rmode = status.get("routing_mode", "auto_hybrid")
            role = status.get("active_input_role", "builtin")
            trans = status.get("transition_state", "idle")
            a2dp = status.get("a2dp_restored", True)
            mode_lbl = (
                "Hybrid" if rmode == "auto_hybrid" else ("Laptop Mic" if rmode == "built_in_only" else "OPPO Mic")
            )
            role_lbl = "Laptop Mic" if role == "builtin" else "OPPO Mic"
            stereo_lbl = "Stereo Restored" if a2dp else "HFP Active"
            trans_lbl = f" · Transition: {trans}" if trans != "idle" else ""
            self._lbl_audio_routing.setText(f"ROUTING: {mode_lbl} ({role_lbl}) · {stereo_lbl}{trans_lbl}")
            self._lbl_audio_routing.setStyleSheet(
                f"color: {C.GREEN if a2dp else '#38BDF8'}; border: none; background: transparent;"
            )
        if hasattr(self, "_lbl_audio_dest") and self._lbl_audio_dest:
            try:
                from memory import config_manager
                eng = config_manager.get_active_engine().replace('_', ' ').title()
                dest = config_manager.get_audio_destination_label()
                self._lbl_audio_dest.setText(f"AUDIO: {eng} · {dest}")
                self._lbl_audio_dest.setStyleSheet(f"color: {C.PRI if eng.lower().startswith('gemini') else '#38BDF8'}; border: none; background: transparent;")
            except Exception:
                pass

    def _on_routing_tab_changed(self, idx: int):
        if not hasattr(self, "_combo_routing"):
            return
        mode = self._combo_routing.currentData()
        if not mode:
            return
        from memory import config_manager

        config_manager.save_audio_routing_mode(mode)
        if hasattr(self, "on_routing_mode_change") and self.on_routing_mode_change:
            try:
                self.on_routing_mode_change(mode)
            except Exception as e:
                print(f"[UI] on_routing_mode_change error: {e}")
        self._refresh_routing_btn()

    def _refresh_audio_tab(self):
        try:
            from core.audio_devices import get_input_devices, get_output_devices, DEFAULT_LABEL
            from memory.config_manager import get_input_device, get_output_device

            cur_in = get_input_device() or DEFAULT_LABEL
            cur_out = get_output_device() or DEFAULT_LABEL

            # Never trigger a PortAudio reset from the Qt UI thread!
            in_devs = get_input_devices(refresh=False)
            out_devs = get_output_devices(refresh=False)

            if hasattr(self, "_combo_mic") and self._combo_mic:
                self._combo_mic.blockSignals(True)
                self._combo_mic.clear()
                for dev in in_devs:
                    self._combo_mic.addItem(dev)
                idx_in = self._combo_mic.findText(cur_in)
                self._combo_mic.setCurrentIndex(idx_in if idx_in >= 0 else 0)
                self._combo_mic.blockSignals(False)

            if hasattr(self, "_combo_spk") and self._combo_spk:
                self._combo_spk.blockSignals(True)
                self._combo_spk.clear()
                for dev in out_devs:
                    self._combo_spk.addItem(dev)
                idx_out = self._combo_spk.findText(cur_out)
                self._combo_spk.setCurrentIndex(idx_out if idx_out >= 0 else 0)
                self._combo_spk.blockSignals(False)

            if hasattr(self, "_combo_routing") and self._combo_routing:
                from memory.config_manager import get_audio_routing_mode

                cur_rm = get_audio_routing_mode()
                self._combo_routing.blockSignals(True)
                idx_rm = self._combo_routing.findData(cur_rm)
                if idx_rm >= 0:
                    self._combo_routing.setCurrentIndex(idx_rm)
                self._combo_routing.blockSignals(False)
        except Exception as exc:
            print(f"[UI] Refresh audio tab error: {exc}")

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 6, 4, 6)
        lay.setSpacing(8)

        def _hdr(txt):
            l = QLabel(txt)
            l.setFont(keena_font(8, True))
            l.setStyleSheet(f"color: {C.PRI}; border-bottom: 1px solid {C.BORDER}; padding-bottom: 3px;")
            return l

        def _action_btn(txt, cb, icon_name=None):
            btn_txt = f" {txt.strip()}" if icon_name and txt.strip() else txt
            b = QPushButton(btn_txt)
            b.setFont(keena_font(9, True))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if icon_name:
                b.setIcon(get_uicon(icon_name, color=C.PRI, size=15))
                b.setIconSize(QSize(15, 15))
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {C.PANEL2}; color: {C.TEXT};
                    border: 1px solid {C.BORDER_B}; border-radius: 4px; padding: 8px 12px;
                }}
                QPushButton:hover {{
                    background: rgba(255, 23, 68, 0.25);
                    border: 1px solid {C.PRI}; color: #ffffff;
                }}
            """)
            b.clicked.connect(cb)
            return b

        lay.addWidget(_hdr("NEURAL INTELLIGENCE CONFIGURATION"))
        lay.addWidget(_action_btn("OPEN REMINDERS MATRIX", lambda: self._show_reminders("upcoming"), "calendar"))
        lay.addWidget(
            _action_btn("OPEN MEMORY & ACTIONS PLATFORM", lambda: self._show_memory_actions("memory"), "database")
        )
        lay.addWidget(_action_btn("CONFIGURE GEMINI API KEY", self._show_setup, "settings"))
        lay.addWidget(_action_btn("ACTION CONFIRMATION TIERS", self._open_confirmation_tiers, "shield"))
        lay.addWidget(_action_btn("OPEN PLUGIN MANAGER", self._open_plugin_manager))
        lay.addWidget(_action_btn("CUSTOMIZE APPEARANCE & IDENTITY", self._open_customize))
        lay.addWidget(_action_btn("PAIR PHONE WEB DASHBOARD", self._open_remote))
        lay.addWidget(_action_btn("ENTER BIG SCREEN MODE (F11)", self._toggle_fullscreen))

        lay.addStretch()
        return w

    def _ui_toggle_wake(self):
        if self.on_wake_toggle:
            st = self.wake_get_state() if self.wake_get_state else {}
            curr = st.get("enabled", False)
            self.on_wake_toggle(not curr)
            self._wake_toggle_btn.setText(f"WAKE WORD: {'ENABLED' if not curr else 'DISABLED'}")

    def _toggle_expanded(self, state: bool | None = None):
        if state is None:
            self._expanded = not self._expanded
        else:
            self._expanded = bool(state)

        if hasattr(self, "_drawer"):
            self._drawer.setVisible(self._expanded)

        screen = QApplication.primaryScreen().availableGeometry()
        curr = self.geometry()

        if self._expanded:
            new_w = min(1040, screen.width() - 40)
            new_h = max(580, curr.height())
            right_anchor = curr.x() + curr.width()
            new_x = max(screen.left() + 20, right_anchor - new_w)
            self.setGeometry(new_x, curr.y(), new_w, new_h)
            if hasattr(self, "_hud_exp_btn"):
                self._hud_exp_btn.setText(" COLLAPSE")
                self._hud_exp_btn.setIcon(get_uicon("angle-small-up", color=C.PRI, size=13))
                self._hud_exp_btn.setToolTip("Collapse drawer into compact HUD")
        else:
            new_w = 420
            new_h = 580
            right_anchor = curr.x() + curr.width()
            new_x = max(screen.left() + 20, right_anchor - new_w)
            self.setGeometry(new_x, curr.y(), new_w, new_h)
            self.resize(new_w, new_h)
            if hasattr(self, "_hud_exp_btn"):
                self._hud_exp_btn.setText(" EXPAND")
                self._hud_exp_btn.setIcon(get_uicon("angle-small-down", color=C.PRI, size=13))
                self._hud_exp_btn.setToolTip("Expand attached JARVIS Intelligence Matrix")
