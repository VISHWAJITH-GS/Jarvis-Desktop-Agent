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
from gui.base import _WIN_HIDE, _base_dir, BASE_DIR, CONFIG_DIR, API_FILE, _read_full_config, _DEFAULT_W, _DEFAULT_H, _MIN_W, _MIN_H, _LEFT_W, _RIGHT_W, _OS, _ResizeEdge, _detect_resize_edge, _cursor_for_edge, _FramelessWindowResizeMixin, _SysMetrics, _metrics, _nvml_gpu_windows, _load_app_fonts, keena_font, HAVE_VIDEO, HoloAvatar, get_uicon, QGraphicsVideoItem, QAudioOutput, QMediaPlayer
from gui.theme import *
from gui.theme import _RESIZE_MARGIN, _NO_SCROLLBAR_STYLE, _HUE_LINKED, _PALETTE_DEFAULTS, _lerp_color

from gui.widgets import *

class ArmouryCrateTelemetryWidget(QWidget):
    """
    ASUS ROG / TUF Armoury Crate inspired hardware telemetry matrix.
    Displays CPU, GPU, Fan Cooling, and Memory/Storage with dynamic gauges
    and interactive Operating Mode profiles (Windows, Silent, Performance, Turbo).
    """

    def __init__(self, main_win=None, parent=None):
        super().__init__(parent)
        self._main_win = main_win
        self._mode = "Performance"
        self.setStyleSheet("background: transparent;")
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 4)
        root.setSpacing(6)

        # ── Section 1: Header (Armoury Crate diagonal hazard slashes)
        sec1_hdr = QHBoxLayout()
        sec1_hdr.setContentsMargins(0, 0, 0, 0)
        sec1_hdr.setSpacing(6)
        slashes1 = QLabel("\\\\\\")
        slashes1.setFont(keena_font(9, bold=True))
        slashes1.setStyleSheet("color: #ff7a00; background: transparent;")
        sec1_hdr.addWidget(slashes1)
        t1 = QLabel("System Usage")
        t1.setFont(keena_font(9, bold=True, spacing=1.2))
        t1.setStyleSheet("color: #ffffff; background: transparent;")
        sec1_hdr.addWidget(t1)
        sec1_hdr.addStretch()
        root.addLayout(sec1_hdr)

        def _make_card(title: str):
            c = QFrame()
            c.setStyleSheet(f"""
                QFrame {{
                    background: rgba(18, 5, 18, 225);
                    border: 1px solid rgba(255, 23, 68, 0.28);
                    border-radius: 5px;
                }}
            """)
            cv = QVBoxLayout(c)
            cv.setContentsMargins(8, 6, 8, 6)
            cv.setSpacing(3)
            hdr = QHBoxLayout()
            hdr.setContentsMargins(0, 0, 0, 0)
            tl = QLabel(title)
            tl.setFont(keena_font(9, bold=True))
            tl.setStyleSheet("color: #ffffff; background: transparent;")
            hdr.addWidget(tl)
            hdr.addStretch()
            sl = QLabel("\\\\\\")
            sl.setFont(keena_font(7, bold=True))
            sl.setStyleSheet("color: #ff7a00; background: transparent;")
            hdr.addWidget(sl)
            cv.addLayout(hdr)
            return c, cv

        def _make_row(name: str):
            r = QHBoxLayout()
            r.setContentsMargins(0, 0, 0, 0)
            nl = QLabel(name)
            nl.setFont(keena_font(7))
            nl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            vl = QLabel("--")
            vl.setFont(keena_font(8, bold=True))
            vl.setStyleSheet("color: #ffffff; background: transparent;")
            r.addWidget(nl)
            r.addStretch()
            r.addWidget(vl)
            return r, vl

        def _make_bar(color="#ff7a00"):
            pb = QProgressBar()
            pb.setFixedHeight(4)
            pb.setTextVisible(False)
            pb.setRange(0, 100)
            pb.setValue(0)
            pb.setStyleSheet(f"""
                QProgressBar {{
                    background: rgba(255, 255, 255, 0.08);
                    border: none;
                    border-radius: 2px;
                }}
                QProgressBar::chunk {{
                    background: {color};
                    border-radius: 2px;
                }}
            """)
            return pb

        # ── Vertical Stack of Cards (CPU, RAM, GPU, Storage in 1-by-1 order)
        cards_lay = QVBoxLayout()
        cards_lay.setSpacing(6)

        # 1. CPU Card
        c_cpu, v_cpu = _make_card("CPU")
        r_cu, self.lbl_cpu_usage = _make_row("Usage")
        self.bar_cpu = _make_bar("#ff7a00")
        r_cf, self.lbl_cpu_freq = _make_row("Frequency")
        r_ct, self.lbl_cpu_temp = _make_row("Temperature")
        r_cv, self.lbl_cpu_volt = _make_row("Voltage")
        v_cpu.addLayout(r_cu)
        v_cpu.addWidget(self.bar_cpu)
        v_cpu.addLayout(r_cf)
        v_cpu.addLayout(r_ct)
        v_cpu.addLayout(r_cv)
        cards_lay.addWidget(c_cpu)

        # 2. RAM Card
        c_ram, v_ram = _make_card("RAM")
        r_mr, self.lbl_ram = _make_row("Memory Usage")
        self.bar_ram = _make_bar("#ff1744")
        v_ram.addLayout(r_mr)
        v_ram.addWidget(self.bar_ram)
        cards_lay.addWidget(c_ram)

        # 3. GPU Card
        c_gpu, v_gpu = _make_card("GPU")
        r_gu, self.lbl_gpu_usage = _make_row("Usage")
        self.bar_gpu = _make_bar("#ff1744")
        r_gf, self.lbl_gpu_freq = _make_row("Frequency")
        r_gt, self.lbl_gpu_temp = _make_row("Temperature")
        r_gv, self.lbl_gpu_volt = _make_row("Voltage")
        v_gpu.addLayout(r_gu)
        v_gpu.addWidget(self.bar_gpu)
        v_gpu.addLayout(r_gf)
        v_gpu.addLayout(r_gt)
        v_gpu.addLayout(r_gv)
        cards_lay.addWidget(c_gpu)

        # 4. Storage Card
        c_store, v_store = _make_card("Storage")
        r_ms, self.lbl_storage = _make_row("Disk Usage (C:)")
        self.bar_storage = _make_bar("#00e5ff")
        v_store.addLayout(r_ms)
        v_store.addWidget(self.bar_storage)
        cards_lay.addWidget(c_store)

        self.lbl_net = QLabel("")
        root.addLayout(cards_lay)
        root.addStretch()

    def set_operating_mode(self, mode: str):
        self._mode = mode

    def update_metrics(self, snap: dict):
        cpu = snap.get("cpu", 0.0)
        gpu = snap.get("gpu", -1.0)
        tmp = snap.get("tmp", -1.0)
        net = snap.get("net", 0.0)

        # Real CPU Frequency
        try:
            cf = psutil.cpu_freq()
            cf_val = int(cf.current) if cf and cf.current else 2827
        except Exception:
            cf_val = 2827
        self.lbl_cpu_freq.setText(f"{cf_val} MHz")

        # CPU Usage
        self.lbl_cpu_usage.setText(f"{cpu:.0f} %")
        self.bar_cpu.setValue(int(max(0, min(100, cpu))))

        # CPU Temp & Voltage
        c_temp = int(tmp if tmp > 0 else (46 + (cpu / 100.0) * 36))
        self.lbl_cpu_temp.setText(f"{c_temp} °C")
        self.lbl_cpu_volt.setText(f"{1.05 + (cpu / 100.0) * 0.31:.2f} V")

        # RAM
        try:
            vm = psutil.virtual_memory()
            r_used = vm.used / (1024**3)
            r_total = vm.total / (1024**3)
            r_pct = vm.percent
        except Exception:
            r_used, r_total, r_pct = 9.0, 15.8, 57.0
        self.lbl_ram.setText(f"{r_used:.1f} / {r_total:.1f} GB ({r_pct:.0f} %)")
        self.bar_ram.setValue(int(r_pct))

        # GPU
        if gpu >= 0:
            g_freq = int(210 + (gpu / 100.0) * 1650)
            self.lbl_gpu_freq.setText(f"{g_freq} MHz")
            self.lbl_gpu_usage.setText(f"{gpu:.0f} %")
            self.bar_gpu.setValue(int(max(0, min(100, gpu))))
            g_temp = int(tmp if tmp > 0 else (42 + (gpu / 100.0) * 32))
            self.lbl_gpu_temp.setText(f"{g_temp} °C")
            self.lbl_gpu_volt.setText(f"{0.72 + (gpu / 100.0) * 0.38:.2f} V")
        else:
            self.lbl_gpu_freq.setText("210 MHz")
            self.lbl_gpu_usage.setText("0 %")
            self.bar_gpu.setValue(0)
            self.lbl_gpu_temp.setText("42 °C")
            self.lbl_gpu_volt.setText("0.65 V")

        # Storage
        try:
            drive = "C:\\" if os.name == "nt" else "/"
            du = psutil.disk_usage(drive)
            s_used = du.used / (1024**3)
            s_total = du.total / (1024**3)
            s_pct = du.percent
        except Exception:
            s_used, s_total, s_pct = 105.7, 446.4, 23.7
        self.lbl_storage.setText(f"{s_used:.1f} / {s_total:.1f} GB ({s_pct:.0f} %)")
        self.bar_storage.setValue(int(s_pct))

        # Network (if label present)
        if hasattr(self, "lbl_net"):
            if net < 1.0:
                net_str = f"{net * 1024:.0f} KB/s"
            else:
                net_str = f"{net:.1f} MB/s"
            self.lbl_net.setText(net_str)


class MinimizedDisplayPill(QPushButton):
    def __init__(self, display_id, title: str, content_type: str = "image", parent=None):
        super().__init__(parent)
        self.display_id = display_id
        self.display_title = title or f"Display {display_id}"
        self.content_type = content_type
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

        if self.display_id == "search":
            self.setToolTip("Search & Intel Display (Click to restore)")
        elif self.display_id == "reminders":
            self.setToolTip("Reminders Matrix (Click to restore)")
        elif self.display_id == "memory_actions":
            self.setToolTip("Memory & Actions Platform (Click to restore)")
        else:
            self.setToolTip(f"Display {self.display_id}: {self.display_title}\n(Click to open)")

        self.setFixedHeight(34)
        self._collapsed_w = 40
        self._expanded_w = min(280, max(160, 50 + len(self.display_title) * 8))
        self.setFixedWidth(self._collapsed_w)

        self._is_hovered = False
        self._update_style()
        self._update_text()

    def _update_text(self):
        icon_or_id = str(self.display_id)
        uicon_name = None
        if self.display_id == "search":
            uicon_name = "search"
        elif self.display_id == "reminders":
            uicon_name = "calendar"
        elif self.display_id == "memory_actions":
            uicon_name = "database"

        if uicon_name:
            c = "#ffffff" if self._is_hovered else C.PRI
            self.setIcon(get_uicon(uicon_name, color=c, size=16))
            self.setIconSize(QSize(16, 16))
            if self._is_hovered:
                clean_title = self.display_title.strip()
                if len(clean_title) > 24:
                    clean_title = clean_title[:22] + "..."
                self.setText(f"  {clean_title} ")
            else:
                self.setText("")
        else:
            if self._is_hovered:
                clean_title = self.display_title.strip()
                if len(clean_title) > 24:
                    clean_title = clean_title[:22] + "..."
                self.setText(f" {icon_or_id} │ {clean_title} ")
            else:
                self.setText(icon_or_id)

    def _update_style(self):
        if self._is_hovered:
            self.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 rgba(48, 8, 14, 252), stop:1.0 rgba(16, 3, 5, 245));
                    color: #ffffff;
                    border: 1.5px solid #ff2b47;
                    border-left: 4px solid #ff1744;
                    border-top-right-radius: 6px;
                    border-bottom-right-radius: 6px;
                    border-top-left-radius: 0px;
                    border-bottom-left-radius: 0px;
                    font-family: 'Montserrat';
                    font-size: 11px;
                    font-weight: bold;
                    text-align: left;
                    padding-left: 8px;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(20, 4, 7, 240);
                    color: #ff3b56;
                    border: 1px solid rgba(255, 23, 68, 0.45);
                    border-left: 3.5px solid #ff1744;
                    border-top-right-radius: 6px;
                    border-bottom-right-radius: 6px;
                    border-top-left-radius: 0px;
                    border-bottom-left-radius: 0px;
                    font-family: 'Montserrat';
                    font-size: 12px;
                    font-weight: bold;
                    text-align: center;
                }}
            """)

    def enterEvent(self, e):
        super().enterEvent(e)
        self._is_hovered = True
        self.setFixedWidth(self._expanded_w)
        self._update_text()
        self._update_style()
        if self.parentWidget() and hasattr(self.parentWidget(), "recalculate_size"):
            self.parentWidget().recalculate_size()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self._is_hovered = False
        self.setFixedWidth(self._collapsed_w)
        self._update_text()
        self._update_style()
        if self.parentWidget() and hasattr(self.parentWidget(), "recalculate_size"):
            self.parentWidget().recalculate_size()


class MinimizedDisplaysDock(QWidget):
    dot_clicked = pyqtSignal(object)

    def __init__(self, parent_main_win=None):
        super().__init__(None)
        self._main_win = parent_main_win
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._vbox = QVBoxLayout(self)
        self._vbox.setContentsMargins(0, 6, 6, 6)
        self._vbox.setSpacing(6)
        self._vbox.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        self._buttons: dict[object, MinimizedDisplayPill] = {}

    def update_displays(self, display_items: list[tuple[object, str, str]]):
        while self._vbox.count():
            item = self._vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._buttons.clear()

        if not display_items:
            self.hide()
            return

        def _sort_key(item):
            did = item[0]
            if isinstance(did, int):
                return (0, did)
            if did == "search":
                return (1, 0)
            if did == "reminders":
                return (1, 1)
            if did == "memory_actions":
                return (1, 2)
            return (2, str(did))

        for disp_id, disp_title, c_type in sorted(display_items, key=_sort_key):
            pill = MinimizedDisplayPill(disp_id, disp_title, c_type, self)
            pill.clicked.connect(lambda _, did=disp_id: self.dot_clicked.emit(did))
            self._vbox.addWidget(pill)
            self._buttons[disp_id] = pill

        self._vbox.activate()
        self.show()
        self.recalculate_size()
        self.raise_()

    def recalculate_size(self):
        if not self._buttons:
            return
        max_w = max((p.width() for p in self._buttons.values()), default=40) + 8
        pill_count = len(self._buttons)
        total_h = pill_count * 34 + (pill_count - 1) * 6 + 12
        self.resize(max_w, total_h)
        self.reposition()

    def reposition(self):
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.left()
        y = max(screen.top() + 60, screen.center().y() - self.height() // 2)
        self.move(x, y)


MinimizedDisplaysOverlay = MinimizedDisplaysDock


class JarvisDisplayWindow(_FramelessWindowResizeMixin, QWidget):
    closed = pyqtSignal(int)
    minimized_requested = pyqtSignal(int)
    _img_data_sig = pyqtSignal(bytes)

    def __init__(self, display_id: int, name: str, content_type: str, source, title: str = "", parent=None):
        super().__init__(parent)
        self.display_id = display_id
        self.display_name = (name or f"DISPLAY {display_id}").replace("//", "-").strip()
        self.content_type = (content_type or "image").lower()
        if isinstance(source, dict):
            self._task_data = source
            self.source = str(source.get("path") or source.get("folder") or "")
            self.title_text = (title or f"TASK: {source.get('folder') or 'PROGRESS'}").replace("//", "-").strip()
        else:
            self.source = str(source or "")
            self.title_text = (title or (Path(self.source).name if (self.source and os.path.exists(self.source)) else (self.source[:36] if self.source else "MEDIA"))).replace("//", "-").strip()

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.SubWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(680, 500)
        self.setMinimumSize(320, 240)
        self._init_frameless_resize()
        self._normal_geom = None
        self._is_maximized_disp = False

        self._outer = QWidget(self)
        self._outer.setObjectName("DisplayOuter")
        self._outer.setStyleSheet(f"""
            QWidget#DisplayOuter {{
                background: rgba(10, 3, 10, 245);
                border: 1px solid {C.PRI};
                border-radius: 6px;
            }}
        """)
        self._outer.setMouseTracking(True)
        self._outer.installEventFilter(self)

        main_v = QVBoxLayout(self)
        main_v.setContentsMargins(0, 0, 0, 0)
        main_v.addWidget(self._outer)

        v = QVBoxLayout(self._outer)
        v.setContentsMargins(10, 8, 10, 10)
        v.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(2, 0, 2, 0)
        hdr.setSpacing(8)

        display_tag = f"{self.display_name} - {self.title_text.upper()}".replace("//", "-")
        tag_lbl = QLabel(display_tag)
        tag_lbl.setFont(keena_font(8, bold=True, spacing=1.2))
        tag_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(tag_lbl)
        hdr.addStretch()

        self._min_btn = QPushButton("MIN")
        self._min_btn.setFont(keena_font(7, bold=True))
        self._min_btn.setFixedSize(32, 20)
        self._min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._min_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(24, 4, 8, 220);
                color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
            }}
            QPushButton:hover {{
                border-color: {C.PRI};
                color: #ffffff;
            }}
        """)
        self._min_btn.clicked.connect(self._on_min_btn_clicked)
        hdr.addWidget(self._min_btn)

        self._max_btn = QPushButton("MAX")
        self._max_btn.setFont(keena_font(7, bold=True))
        self._max_btn.setFixedSize(32, 20)
        self._max_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._max_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(24, 4, 8, 220);
                color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
            }}
            QPushButton:hover {{
                border-color: {C.PRI};
                color: #ffffff;
            }}
        """)
        self._max_btn.clicked.connect(self.maximize_window)
        hdr.addWidget(self._max_btn)

        close_btn = QPushButton("")
        close_btn.setIcon(get_uicon("cross", color="#ff5252", size=11))
        close_btn.setFont(keena_font(8, bold=True))
        close_btn.setFixedSize(22, 20)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(35, 6, 10, 210);
                color: #ff5252;
                border: 1px solid rgba(255, 23, 68, 0.4);
                border-radius: 3px;
            }}
            QPushButton:hover {{
                background: rgba(255, 23, 68, 0.45);
                border-color: #ff1744;
                color: #ffffff;
            }}
        """)
        close_btn.clicked.connect(self.close)
        hdr.addWidget(close_btn)
        v.addLayout(hdr)

        self._content_cont = QWidget()
        self._content_cont.setStyleSheet("background: #020003; border-radius: 4px;")
        cont_lay = QVBoxLayout(self._content_cont)
        cont_lay.setContentsMargins(4, 4, 4, 4)

        if self.content_type == "video" and HAVE_VIDEO:
            self._setup_video(cont_lay)
        elif self.content_type == "text":
            self._setup_text(cont_lay)
        elif self.content_type in ("task", "progress", "plan"):
            self._setup_task(cont_lay)
        else:
            self._setup_image(cont_lay)

        v.addWidget(self._content_cont, stretch=1)

    def _setup_task(self, lay: QVBoxLayout):
        self.resize(720, 560)
        self.setMinimumSize(480, 360)

        # ── 1. Goal / Overview Card ──────────────────────────────────────────
        self._goal_box = QWidget()
        self._goal_box.setStyleSheet(f"""
            QWidget {{
                background: rgba(18, 4, 6, 235);
                border: 1px solid rgba(255, 23, 68, 0.4);
                border-radius: 6px;
            }}
        """)
        g_lay = QVBoxLayout(self._goal_box)
        g_lay.setContentsMargins(10, 8, 10, 8)
        g_lay.setSpacing(4)

        self._lbl_goal_title = QLabel("◈ TASK OBJECTIVE & ROADMAP")
        self._lbl_goal_title.setFont(keena_font(9, bold=True))
        self._lbl_goal_title.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
        g_lay.addWidget(self._lbl_goal_title)

        self._lbl_goal_req = QLabel()
        self._lbl_goal_req.setFont(QFont("Montserrat", 9))
        self._lbl_goal_req.setWordWrap(True)
        self._lbl_goal_req.setStyleSheet(f"color: {C.TEXT}; background: transparent; border: none;")
        g_lay.addWidget(self._lbl_goal_req)

        self._lbl_goal_meta = QLabel()
        self._lbl_goal_meta.setFont(keena_font(8, bold=True))
        self._lbl_goal_meta.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        g_lay.addWidget(self._lbl_goal_meta)

        lay.addWidget(self._goal_box)

        # ── 2. Phase Flow & Progress Bar ─────────────────────────────────────
        self._phase_box = QWidget()
        self._phase_box.setStyleSheet("background: transparent; border: none;")
        p_lay = QVBoxLayout(self._phase_box)
        p_lay.setContentsMargins(4, 2, 4, 2)
        p_lay.setSpacing(4)

        self._lbl_phases = QLabel()
        self._lbl_phases.setFont(keena_font(8, bold=True))
        self._lbl_phases.setStyleSheet("background: transparent; border: none;")
        p_lay.addWidget(self._lbl_phases)

        self._task_pbar = QProgressBar()
        self._task_pbar.setFixedHeight(12)
        self._task_pbar.setRange(0, 100)
        self._task_pbar.setValue(0)
        self._task_pbar.setStyleSheet(f"""
            QProgressBar {{
                background: rgba(20, 5, 20, 220);
                border: 1px solid rgba(255, 23, 68, 0.4);
                border-radius: 6px;
                text-align: center;
                color: #ffffff;
                font-family: 'Montserrat';
                font-size: 9px;
                font-weight: bold;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {C.PRI}, stop:1.0 #ff5252);
                border-radius: 5px;
            }}
        """)
        p_lay.addWidget(self._task_pbar)
        lay.addWidget(self._phase_box)

        # ── 3. Split: Files on top, Live Logs on bottom ───────────────────────
        self._files_lbl = QLabel("◈ FILES GENERATED & MODIFIED")
        self._files_lbl.setFont(keena_font(8, bold=True))
        self._files_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none; margin-top: 2px;")
        lay.addWidget(self._files_lbl)

        self._task_files_edit = QTextEdit()
        self._task_files_edit.setReadOnly(True)
        self._task_files_edit.setFixedHeight(95)
        self._task_files_edit.setFrameShape(QFrame.Shape.NoFrame)
        self._task_files_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._task_files_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._task_files_edit.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(10, 2, 4, 220);
                color: {C.TEXT};
                border: 1px solid rgba(255, 23, 68, 0.3);
                border-radius: 4px;
                padding: 6px;
                font-family: 'Montserrat';
                font-size: 9px;
            }}
            QScrollBar:vertical, QScrollBar:horizontal {{
                width: 0px;
                height: 0px;
                background: transparent;
                border: none;
            }}
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
                background: transparent;
                width: 0px;
                height: 0px;
            }}
        """)
        lay.addWidget(self._task_files_edit)

        self._logs_lbl = QLabel("◈ LIVE EXECUTION CONSOLE & STEPS")
        self._logs_lbl.setFont(keena_font(8, bold=True))
        self._logs_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none; margin-top: 2px;")
        lay.addWidget(self._logs_lbl)

        self._task_log_edit = QTextEdit()
        self._task_log_edit.setReadOnly(True)
        self._task_log_edit.setFrameShape(QFrame.Shape.NoFrame)
        self._task_log_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._task_log_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._task_log_edit.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(6, 1, 2, 240);
                color: {C.TEXT};
                border: 1px solid rgba(255, 23, 68, 0.3);
                border-radius: 4px;
                padding: 6px;
                font-family: 'Montserrat';
                font-size: 9px;
            }}
            QScrollBar:vertical, QScrollBar:horizontal {{
                width: 0px;
                height: 0px;
                background: transparent;
                border: none;
            }}
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
                background: transparent;
                width: 0px;
                height: 0px;
            }}
        """)
        lay.addWidget(self._task_log_edit, stretch=1)

        # ── 4. Task Action Bar ────────────────────────────────────────────────
        act_bar = QHBoxLayout()
        act_bar.setContentsMargins(0, 4, 0, 0)
        act_bar.setSpacing(8)

        def _act_btn(txt, tip):
            b = QPushButton(txt)
            b.setFont(keena_font(8, bold=True))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            b.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(28, 4, 8, 220);
                    color: {C.TEXT};
                    border: 1px solid {C.PRI};
                    border-radius: 4px;
                    padding: 5px 10px;
                }}
                QPushButton:hover {{
                    background: rgba(255, 23, 68, 0.3);
                    border-color: #ff1744;
                    color: #ffffff;
                }}
                QPushButton:pressed {{
                    background: rgba(255, 23, 68, 0.5);
                }}
            """)
            return b

        self._btn_open_folder = _act_btn("📂 OPEN FOLDER", "Open project directory in Windows Explorer")
        self._btn_open_folder.clicked.connect(self._on_action_open_folder)
        act_bar.addWidget(self._btn_open_folder)

        self._btn_run_project = _act_btn("▶ RUN PROJECT", "Run the project entry script and view execution output")
        self._btn_run_project.clicked.connect(self._on_action_run_project)
        act_bar.addWidget(self._btn_run_project)

        self._btn_copy_plan = _act_btn("📋 COPY PLAN", "Copy plan and task summary to clipboard")
        self._btn_copy_plan.clicked.connect(self._on_action_copy_plan)
        act_bar.addWidget(self._btn_copy_plan)

        act_bar.addStretch()

        self._btn_stop_task = _act_btn("⏹ STOP TASK", "Halt running task")
        self._btn_stop_task.setStyleSheet(f"""
            QPushButton {{
                background: rgba(35, 6, 10, 220);
                color: #ff5252;
                border: 1px solid rgba(255, 23, 68, 0.5);
                border-radius: 4px;
                padding: 5px 10px;
            }}
            QPushButton:hover {{
                background: rgba(255, 23, 68, 0.4);
                border-color: #ff1744;
                color: #ffffff;
            }}
        """)
        self._btn_stop_task.clicked.connect(self._on_action_stop_task)
        act_bar.addWidget(self._btn_stop_task)

        lay.addLayout(act_bar)

        if hasattr(self, "_task_data") and self._task_data:
            self.update_task_data(self._task_data)

    def _on_action_open_folder(self):
        import subprocess
        path = getattr(self, "_task_path", "")
        if not path and hasattr(self, "_task_data") and isinstance(self._task_data, dict):
            path = self._task_data.get("path") or self._task_data.get("folder") or ""
        if path and os.path.exists(path):
            try:
                subprocess.Popen(f'explorer "{os.path.abspath(path)}"')
            except Exception as ex:
                self._task_log_edit.append(f"[Explorer Error] {ex}")
        else:
            dt = os.path.join(os.path.expanduser("~"), "Desktop")
            subprocess.Popen(f'explorer "{os.path.abspath(dt)}"')

    def _on_action_run_project(self):
        path = getattr(self, "_task_path", "")
        if not path and hasattr(self, "_task_data") and isinstance(self._task_data, dict):
            path = self._task_data.get("path") or ""
        if not path or not os.path.exists(path):
            self._task_log_edit.append("[Run Error] Project path not found or not yet created.")
            return

        def _worker():
            import subprocess, sys
            entry = os.path.join(path, "main.py")
            cmd = [sys.executable, entry] if os.path.exists(entry) else [sys.executable, "-m", "unittest"]
            self._task_log_edit.append(f"\n▶ Running: {' '.join(cmd)}\n")
            try:
                p = subprocess.Popen(cmd, cwd=path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in p.stdout:
                    self._task_log_edit.append(line.rstrip())
                p.wait()
                self._task_log_edit.append(f"\n[Process exited with code {p.returncode}]\n")
            except Exception as e:
                self._task_log_edit.append(f"[Run Project Failed] {e}\n")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_action_copy_plan(self):
        txt_parts = []
        if hasattr(self, "_lbl_goal_req"):
            txt_parts.append(f"TASK OBJECTIVE:\n{self._lbl_goal_req.text()}\n")
        if hasattr(self, "_task_files_edit"):
            txt_parts.append(f"FILES:\n{self._task_files_edit.toPlainText()}\n")
        if hasattr(self, "_task_log_edit"):
            txt_parts.append(f"EXECUTION LOG:\n{self._task_log_edit.toPlainText()}\n")
        full_text = "\n".join(txt_parts)
        QApplication.clipboard().setText(full_text)
        self._btn_copy_plan.setText("✓ COPIED!")
        QTimer.singleShot(1500, lambda: self._btn_copy_plan.setText("📋 COPY PLAN"))

    def _on_action_stop_task(self):
        self._task_stopped = True
        self._btn_stop_task.setEnabled(False)
        self._btn_stop_task.setText("STOPPED")
        if hasattr(self, "_task_log_edit"):
            self._task_log_edit.append("\n[TASK HALTED BY USER]\n")

    def update_task_data(self, data: dict):
        self._task_data = data
        if not hasattr(self, "_task_pbar"):
            return

        status = str(data.get("status") or "planning").lower()
        request = str(data.get("request") or data.get("goal") or data.get("description") or "Task in progress...")
        folder = str(data.get("folder") or "Project")
        self._task_path = str(data.get("path") or "")
        model = str(data.get("model") or "Gemini")
        kind = str(data.get("kind") or "Task").capitalize()

        # Goal & Meta
        self._lbl_goal_title.setText(f"◈ TASK OBJECTIVE — {folder.upper()}")
        self._lbl_goal_req.setText(request)

        finished = data.get("finished")
        started = data.get("started", time.time())
        took = (finished or time.time()) - started
        st_color = "#00ffaa" if status == "done" else ("#ff1744" if status != "failed" else "#ff3366")
        self._lbl_goal_meta.setText(f"Kind: {kind}  │  Model: {model}  │  Status: <font color='{st_color}'>{status.upper()}</font>  │  Elapsed: {took:.1f}s")

        # Phase pills & progress bar
        phases = list(data.get("phases") or ["planning", "writing", "installing", "testing"])
        here = phases.index(status) if status in phases else (len(phases) if (status == "done" or finished) else -1)
        phase_html = []
        for i, ph in enumerate(phases):
            if i < here or status == "done":
                phase_html.append(f"<span style='color:#00ffaa;'>● {ph.upper()} ✓</span>")
            elif i == here:
                phase_html.append(f"<span style='color:#ff5252; font-weight:bold;'>▶ {ph.upper()}</span>")
            else:
                phase_html.append(f"<span style='color:{C.TEXT_DIM};'>○ {ph.upper()}</span>")
        if status == "done":
            phase_html.append("<span style='color:#00ffaa; font-weight:bold;'>✓ COMPLETE</span>")
        elif status == "failed":
            phase_html.append("<span style='color:#ff1744; font-weight:bold;'>✕ FAILED</span>")

        self._lbl_phases.setText(" &nbsp;➔&nbsp; ".join(phase_html))

        # Progress calculation
        if status == "done":
            pct = 100
        elif here >= 0:
            pct = int(((here + 0.5) / max(1, len(phases))) * 100)
        else:
            pct = int(data.get("progress", 10))
        self._task_pbar.setValue(min(100, max(0, pct)))

        # Files list
        files = data.get("files") or {}
        if isinstance(files, dict):
            f_lines = []
            for fname, finfo in files.items():
                if isinstance(finfo, dict):
                    act = "NEW" if finfo.get("action") == "create" else "EDIT"
                    col = "#00ffaa" if act == "NEW" else "#ff1744"
                    desc = finfo.get("what", "")
                    f_lines.append(f"<b><font color='{col}'>[{act}]</font> {fname}</b> — {desc}")
                else:
                    f_lines.append(f"<b>{fname}</b>")
            self._task_files_edit.setHtml("<br>".join(f_lines) if f_lines else "<i>No files registered yet...</i>")
        elif isinstance(files, list):
            f_lines = [f"• {f}" for f in files]
            self._task_files_edit.setHtml("<br>".join(f_lines))

        # Steps / Logs
        steps = data.get("steps") or []
        log_lines = []
        for item in steps:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                t_val, msg = item[0], item[1]
                log_lines.append(f"[{float(t_val):5.1f}s] {msg}")
            else:
                log_lines.append(str(item))

        if log_lines:
            self._task_log_edit.setPlainText("\n".join(log_lines))
            self._task_log_edit.verticalScrollBar().setValue(self._task_log_edit.verticalScrollBar().maximum())

    def _setup_text(self, lay: QVBoxLayout):
        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setFrameShape(QFrame.Shape.NoFrame)
        self._text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._text_edit.setFont(QFont("Montserrat", 10))
        self._text_edit.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                color: {C.TEXT};
                border: none;
                padding: 10px 12px;
                selection-background-color: rgba(255, 23, 68, 0.35);
            }}
            QScrollBar:vertical, QScrollBar:horizontal {{
                background: transparent;
                width: 0px;
                height: 0px;
                border: none;
                margin: 0px;
            }}
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
                background: transparent;
                width: 0px;
                height: 0px;
            }}
        """)
        raw_text = self.source
        if os.path.exists(self.source) and os.path.isfile(self.source):
            try:
                with open(self.source, "r", encoding="utf-8", errors="replace") as f:
                    raw_text = f.read()
            except Exception:
                pass

        if hasattr(self._text_edit, "setMarkdown"):
            self._text_edit.setMarkdown(raw_text)
        else:
            self._text_edit.setPlainText(raw_text)
        lay.addWidget(self._text_edit)

    def _setup_image(self, lay: QVBoxLayout):
        self._img_data_sig.connect(self._on_image_bytes_loaded)
        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet("background: transparent;")
        self._img_lbl.setText("FETCHING VISUAL FEED  -  INTEL ACTIVE...")
        self._img_lbl.setFont(keena_font(8, bold=True))
        lay.addWidget(self._img_lbl)
        self._load_image()

    def _load_image(self):
        if not self.source:
            self._img_lbl.setText("NO IMAGE SOURCE SPECIFIED")
            return
        if os.path.exists(self.source):
            px = QPixmap(self.source)
            if not px.isNull():
                self._original_pixmap = px
                self._update_scaled_image()
                return

        # URL or remote source: download in background thread
        t = threading.Thread(target=self._download_image_worker, daemon=True)
        t.start()

    def _download_image_worker(self):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
        }
        url = self.source
        # Fix Wikipedia thumb URL if present
        if "upload.wikimedia.org" in url and "/thumb/" in url:
            m = re.match(r"(https?://upload\.wikimedia\.org/wikipedia/[^/]+)/thumb/([^/]+/[^/]+/[^/]+)/.*", url)
            if m:
                url = f"{m.group(1)}/{m.group(2)}"

    def _download_image_bytes(self, url: str, headers: dict, timeout: int = 5) -> bytes | None:
        try:
            import urllib.request
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return resp.read()
        except Exception:
            pass
        return None

    def _search_image_fallback(self, query: str, headers: dict) -> bytes | None:
        try:
            from actions.web_search import _get_ddgs
            DDGS = _get_ddgs()
            with DDGS() as ddgs:
                for r in ddgs.images(query, max_results=3):
                    img_url = r.get("image")
                    if not img_url:
                        continue
                    data = self._download_image_bytes(img_url, headers, timeout=4)
                    if data:
                        return data
        except Exception as e:
            print(f"[JarvisDisplayWindow] Fallback search error: {e}")
        return None

    def _fetch_image_thread(self):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
        }
        url = self.source
        # Fix Wikipedia thumb URL if present
        if "upload.wikimedia.org" in url and "/thumb/" in url:
            m = re.match(r"(https?://upload\.wikimedia\.org/wikipedia/[^/]+)/thumb/([^/]+/[^/]+/[^/]+)/.*", url)
            if m:
                url = f"{m.group(1)}/{m.group(2)}"

        data = None
        if url.startswith(("http://", "https://")):
            data = self._download_image_bytes(url, headers, timeout=6)

        # If data fetch failed, search DDG for image
        if not data:
            query = self.title_text or Path(self.source.split("?")[0]).stem.replace("_", " ")
            if query and not query.lower().startswith("display"):
                data = self._search_image_fallback(query, headers)

        self._img_data_sig.emit(data or b"")

    def _on_image_bytes_loaded(self, data: bytes):
        if data:
            px = QPixmap()
            px.loadFromData(data)
            if not px.isNull():
                self._original_pixmap = px
                self._update_scaled_image()
                return
        self._img_lbl.setText("IMAGE RETRIEVAL FAILED - RE-TRY WITH SEARCH")
        self._img_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; font-family: 'Montserrat';")

    def _update_scaled_image(self):
        if hasattr(self, "_original_pixmap") and not self._original_pixmap.isNull():
            w = max(100, self._content_cont.width() - 10)
            h = max(100, self._content_cont.height() - 10)
            scaled = self._original_pixmap.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self._img_lbl.setPixmap(scaled)

    def _setup_video(self, lay: QVBoxLayout):
        try:
            self._vid_player = QMediaPlayer(self)
            self._vid_audio = QAudioOutput(self)
            self._vid_player.setAudioOutput(self._vid_audio)
            self._vid_audio.setMuted(True)

            self._scene = QGraphicsScene(self)
            self._view = QGraphicsView(self._scene, self)
            self._view.setStyleSheet("background: #000000; border: none;")
            self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._vid_item = QGraphicsVideoItem()
            self._scene.addItem(self._vid_item)
            self._vid_player.setVideoOutput(self._vid_item)

            lay.addWidget(self._view, stretch=1)

            c_bar = QHBoxLayout()
            c_bar.setContentsMargins(4, 2, 4, 2)
            c_bar.setSpacing(6)

            self._play_btn = QPushButton("PAUSE")
            self._play_btn.setFont(keena_font(7, bold=True))
            self._play_btn.setFixedSize(48, 20)
            self._play_btn.setStyleSheet(f"background: rgba(24, 4, 8, 220); color: #fff; border: 1px solid {C.PRI}; border-radius: 3px;")
            self._play_btn.clicked.connect(self._toggle_playback)
            c_bar.addWidget(self._play_btn)

            self._mute_btn = QPushButton("SOUND OFF")
            self._mute_btn.setFont(keena_font(7, bold=True))
            self._mute_btn.setFixedSize(68, 20)
            self._mute_btn.setStyleSheet(f"background: rgba(24, 4, 8, 220); color: #fff; border: 1px solid {C.PRI}; border-radius: 3px;")
            self._mute_btn.clicked.connect(self._toggle_sound)
            c_bar.addWidget(self._mute_btn)
            c_bar.addStretch()

            lay.addLayout(c_bar)

            if os.path.exists(self.source):
                self._vid_player.setSource(QUrl.fromLocalFile(self.source))
            else:
                self._vid_player.setSource(QUrl(self.source))
            self._vid_player.play()
        except Exception as e:
            print(f"[JarvisDisplayWindow] Video setup error: {e}")
            self._setup_image(lay)

    def _toggle_playback(self):
        if hasattr(self, "_vid_player"):
            if self._vid_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self._vid_player.pause()
                self._play_btn.setText("PLAY")
            else:
                self._vid_player.play()
                self._play_btn.setText("PAUSE")

    def _toggle_sound(self):
        if hasattr(self, "_vid_audio"):
            is_muted = not self._vid_audio.isMuted()
            self._vid_audio.setMuted(is_muted)
            self._mute_btn.setText("SOUND OFF" if is_muted else "SOUND ON")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_original_pixmap"):
            self._update_scaled_image()
        if hasattr(self, "_view") and hasattr(self, "_vid_item"):
            w = self._view.width()
            h = self._view.height()
            self._scene.setSceneRect(0, 0, w, h)
            self._vid_item.setSize(QSizeF(w, h))

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

    def eventFilter(self, watched, event):
        if watched == getattr(self, "_outer", None):
            if self._filter_child_mouse_event(watched, event):
                return True
        return super().eventFilter(watched, event)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and e.position().toPoint().y() <= 38:
            self.maximize_window()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def eventFilter(self, watched, event):
        if watched == getattr(self, "_outer", None):
            if self._filter_child_mouse_event(watched, event):
                return True
        return super().eventFilter(watched, event)

    def maximize_window(self):
        """Maximize display to fill available screen workspace or restore to previous normal geometry."""
        screen = QApplication.primaryScreen().availableGeometry()
        if not getattr(self, "_is_maximized_disp", False):
            self._normal_geom = self.geometry()
            self._is_maximized_disp = True
            if hasattr(self, "_max_btn"):
                self._max_btn.setText("NORM")
                self._max_btn.setToolTip("Restore display to normal size")
            self.setGeometry(screen)
        else:
            self._is_maximized_disp = False
            if hasattr(self, "_max_btn"):
                self._max_btn.setText("MAX")
                self._max_btn.setToolTip("Maximize display")
            if getattr(self, "_normal_geom", None):
                self.setGeometry(self._normal_geom)
            else:
                self.resize(680, 500)
        self.show()
        self.raise_()
        self.activateWindow()

    def minimize_to_dock(self):
        """Minimize display into the left-edge dock."""
        self._on_min_btn_clicked()

    def restore_from_dock(self):
        """Restore display from left dock and show."""
        self.setWindowState(Qt.WindowState.WindowNoState)
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_min_btn_clicked(self):
        self.hide()
        self.minimized_requested.emit(self.display_id)

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            self.setWindowState(Qt.WindowState.WindowNoState)
            self.hide()
            self.minimized_requested.emit(self.display_id)

    def closeEvent(self, e):
        if hasattr(self, "_vid_player"):
            try:
                self._vid_player.stop()
            except Exception:
                pass
        self.closed.emit(self.display_id)
        super().closeEvent(e)


