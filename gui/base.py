from __future__ import annotations

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

import psutil

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

# Qt's video backend prints the ffmpeg stream banner — codec, bitrate, the
# whole signed googlevideo URL — to the console for every stream it opens. That
# is two screenfuls per video, it looks like an error to anyone reading the log,
# and it puts a URL carrying the viewer's IP address into a terminal people
# paste into bug reports. Silenced here, before Qt initialises its logging.
# An explicit setting from the user is left alone.
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")

from PyQt6.QtCore import (
    QEvent, QEasingCurve, QLineF, QMimeData, QObject, QParallelAnimationGroup, QPointF,
    QPoint, QPropertyAnimation, QRect, QRectF, QSize, QSizeF, Qt, QTimer,
    QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QCursor, QDragEnterEvent, QDropEvent, QFont,
    QFontDatabase, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QPolygonF, QRadialGradient, QShortcut,
)
# Video playback for the HUD. Part of PyQt6, so it costs no new dependency —
# but the multimedia plugins are a separate piece of the Qt install and can be
# absent on a stripped-down system, so a failure here disables one feature
# rather than stopping JARVIS from starting.
try:
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
    HAVE_VIDEO = True
except Exception as _e:            # noqa: BLE001 - reported, never fatal
    QAudioOutput = QMediaPlayer = QGraphicsVideoItem = None
    HAVE_VIDEO = False
    print(f"[Video] playback unavailable ({_e}) — the HUD will not show video.")

from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QGraphicsScene, QGraphicsView, QGraphicsDropShadowEffect,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar, QSlider,
)

try:
    from core.avatar import HoloAvatar
except Exception:      # pragma: no cover — HUD must never die over cosmetics
    HoloAvatar = None

try:
    from gui.icons_manager import get_uicon
except Exception:
    def get_uicon(*args, **kwargs):
        from PyQt6.QtGui import QIcon
        return QIcon()


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"


def _read_full_config() -> dict:
    """Read api_keys.json config dict. Returns {} on any error."""
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# Single source of truth for the release name — the window title, the header
# badge and the readme must never disagree again.
APP_VERSION  = "MARK LVI"
APP_PROTOCOL = APP_VERSION.split()[-1]

_DEFAULT_W, _DEFAULT_H = 980, 700
_MIN_W,     _MIN_H     = 820, 580
_LEFT_W  = 148
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


from gui.theme import *
from gui.theme import _RESIZE_MARGIN, _NO_SCROLLBAR_STYLE, _HUE_LINKED, _PALETTE_DEFAULTS, _lerp_color

class _ResizeEdge:
    NONE = 0
    LEFT = 1
    RIGHT = 2
    TOP = 4
    BOTTOM = 8
    TOP_LEFT = TOP | LEFT
    TOP_RIGHT = TOP | RIGHT
    BOTTOM_LEFT = BOTTOM | LEFT
    BOTTOM_RIGHT = BOTTOM | RIGHT


def _detect_resize_edge(widget: QWidget, pos: QPoint, margin: int = _RESIZE_MARGIN) -> int:
    edge = _ResizeEdge.NONE
    x, y = pos.x(), pos.y()
    w, h = widget.width(), widget.height()

    if x < margin:
        edge |= _ResizeEdge.LEFT
    elif x >= w - margin:
        edge |= _ResizeEdge.RIGHT

    if y < margin:
        edge |= _ResizeEdge.TOP
    elif y >= h - margin:
        edge |= _ResizeEdge.BOTTOM

    return edge


def _cursor_for_edge(edge: int) -> Qt.CursorShape:
    if edge in (_ResizeEdge.TOP_LEFT, _ResizeEdge.BOTTOM_RIGHT):
        return Qt.CursorShape.SizeFDiagCursor
    elif edge in (_ResizeEdge.TOP_RIGHT, _ResizeEdge.BOTTOM_LEFT):
        return Qt.CursorShape.SizeBDiagCursor
    elif edge in (_ResizeEdge.LEFT, _ResizeEdge.RIGHT):
        return Qt.CursorShape.SizeHorCursor
    elif edge in (_ResizeEdge.TOP, _ResizeEdge.BOTTOM):
        return Qt.CursorShape.SizeVerCursor
    return Qt.CursorShape.ArrowCursor


class _FramelessWindowResizeMixin:
    """Provides native edge and corner resizing (shrink & expand) and window moving for frameless windows."""

    def _init_frameless_resize(self):
        self.setMouseTracking(True)
        self._active_resize_edge = _ResizeEdge.NONE
        self._drag_start_pos = None
        self._drag_start_geom = None
        self._drag_start = None
        self._win_start = None

    def _handle_mouse_press(self, e) -> bool:
        if e.button() == Qt.MouseButton.LeftButton:
            pos = e.position().toPoint()
            is_max = getattr(self, "_is_maximized_disp", False) or getattr(self, "_is_fullscreen", False) or getattr(self, "_collapsed", False)
            edge = _detect_resize_edge(self, pos) if not is_max else _ResizeEdge.NONE
            if edge != _ResizeEdge.NONE:
                self._active_resize_edge = edge
                self._drag_start_pos = e.globalPosition().toPoint()
                self._drag_start_geom = self.geometry()
                e.accept()
                return True
            else:
                self._active_resize_edge = _ResizeEdge.NONE
                self._drag_start = e.globalPosition().toPoint()
                self._win_start = self.pos()
        return False

    def _handle_mouse_move(self, e) -> bool:
        is_max = getattr(self, "_is_maximized_disp", False) or getattr(self, "_is_fullscreen", False) or getattr(self, "_collapsed", False)
        if e.buttons() & Qt.MouseButton.LeftButton:
            edge = getattr(self, "_active_resize_edge", _ResizeEdge.NONE)
            if edge != _ResizeEdge.NONE and getattr(self, "_drag_start_geom", None):
                delta = e.globalPosition().toPoint() - self._drag_start_pos
                orig = self._drag_start_geom
                min_w = self.minimumWidth() or 240
                min_h = self.minimumHeight() or 180
                max_w = self.maximumWidth()
                max_h = self.maximumHeight()

                new_x = orig.x()
                new_y = orig.y()
                new_w = orig.width()
                new_h = orig.height()

                if edge & _ResizeEdge.LEFT:
                    candidate_w = orig.width() - delta.x()
                    new_w = max(min_w, min(max_w, candidate_w))
                    new_x = orig.x() + (orig.width() - new_w)
                elif edge & _ResizeEdge.RIGHT:
                    candidate_w = orig.width() + delta.x()
                    new_w = max(min_w, min(max_w, candidate_w))

                if edge & _ResizeEdge.TOP:
                    candidate_h = orig.height() - delta.y()
                    new_h = max(min_h, min(max_h, candidate_h))
                    new_y = orig.y() + (orig.height() - new_h)
                elif edge & _ResizeEdge.BOTTOM:
                    candidate_h = orig.height() + delta.y()
                    new_h = max(min_h, min(max_h, candidate_h))

                self.setGeometry(new_x, new_y, new_w, new_h)
                e.accept()
                return True

            if getattr(self, "_drag_start", None):
                delta = e.globalPosition().toPoint() - self._drag_start
                self.move(self._win_start + delta)
                e.accept()
                return True
        else:
            if not is_max:
                edge = _detect_resize_edge(self, e.position().toPoint())
                if edge != _ResizeEdge.NONE:
                    self.setCursor(_cursor_for_edge(edge))
                    return True
                else:
                    self.unsetCursor()
        return False

    def _handle_mouse_release(self, e) -> bool:
        self._active_resize_edge = _ResizeEdge.NONE
        self._drag_start = None
        self._drag_start_geom = None
        self._win_start = None
        self.unsetCursor()
        return False

    def _filter_child_mouse_event(self, watched, event) -> bool:
        et = event.type()
        is_max = (
            getattr(self, "_is_maximized_disp", False)
            or getattr(self, "_is_fullscreen", False)
            or getattr(self, "_is_minimized_top", False)
            or getattr(self, "_collapsed", False)
        )
        if et == QEvent.Type.MouseMove:
            if getattr(self, "_active_resize_edge", _ResizeEdge.NONE) != _ResizeEdge.NONE and (event.buttons() & Qt.MouseButton.LeftButton):
                self._handle_mouse_move(event)
                return True
            if not (event.buttons() & Qt.MouseButton.LeftButton):
                if not is_max:
                    pos = watched.mapTo(self, event.position().toPoint())
                    edge = _detect_resize_edge(self, pos)
                    if edge != _ResizeEdge.NONE:
                        self.setCursor(_cursor_for_edge(edge))
                        return False
                    else:
                        if self.cursor().shape() != Qt.CursorShape.ArrowCursor:
                            self.unsetCursor()
        elif et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            pos = watched.mapTo(self, event.position().toPoint())
            edge = _detect_resize_edge(self, pos) if not is_max else _ResizeEdge.NONE
            if edge != _ResizeEdge.NONE:
                self._active_resize_edge = edge
                self._drag_start_pos = event.globalPosition().toPoint()
                self._drag_start_geom = self.geometry()
                return True
        elif et == QEvent.Type.MouseButtonRelease and getattr(self, "_active_resize_edge", _ResizeEdge.NONE) != _ResizeEdge.NONE:
            self._handle_mouse_release(event)
            return True
        return False


class StandardTitleBarControls(QWidget):
    """
    Standardized, uniform top-right title bar controls for all floating displays & modals:
      — (Minimize) | □ / ❐ (Maximize / Restore Toggle) | ✕ (Close)
    """

    def __init__(
        self,
        target_window: QWidget,
        on_minimize=None,
        on_maximize=None,
        on_close=None,
        parent=None,
    ):
        super().__init__(parent or target_window)
        self.target = target_window
        self._on_minimize_cb = on_minimize
        self._on_maximize_cb = on_maximize
        self._on_close_cb = on_close

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        btn_style = f"""
            QPushButton {{
                background: rgba(24, 4, 8, 200);
                color: {C.TEXT_DIM};
                border: 1px solid rgba(255, 23, 68, 0.35);
                border-radius: 3px;
                font-family: 'Montserrat', sans-serif;
                font-size: 11px;
                font-weight: bold;
                min-width: 24px;
                height: 20px;
                padding: 0 4px;
            }}
            QPushButton:hover {{
                color: #ffffff;
                background: rgba(255, 23, 68, 0.35);
                border-color: {C.PRI};
            }}
            QPushButton#CloseBtn:hover {{
                background: rgba(255, 23, 68, 0.85);
                color: #ffffff;
                border-color: #ff1744;
            }}
        """

        self._min_btn = QPushButton("—")
        self._min_btn.setToolTip("Minimize")
        self._min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._min_btn.setStyleSheet(btn_style)
        self._min_btn.clicked.connect(self._handle_min)
        lay.addWidget(self._min_btn)

        self._max_btn = QPushButton("□")
        self._max_btn.setToolTip("Maximize")
        self._max_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._max_btn.setStyleSheet(btn_style)
        self._max_btn.clicked.connect(self._handle_max)
        lay.addWidget(self._max_btn)

        self._close_btn = QPushButton("✕")
        self._close_btn.setObjectName("CloseBtn")
        self._close_btn.setToolTip("Close")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet(btn_style)
        self._close_btn.clicked.connect(self._handle_close)
        lay.addWidget(self._close_btn)

    def _handle_min(self):
        if self._on_minimize_cb:
            self._on_minimize_cb()
            return
        if hasattr(self.target, "toggle_collapse"):
            self.target.toggle_collapse()
        elif hasattr(self.target, "minimize_to_dock"):
            self.target.minimize_to_dock()
        else:
            self.target.showMinimized()

    def _handle_max(self):
        if self._on_maximize_cb:
            self._on_maximize_cb()
            return
        is_max = getattr(self.target, "_is_maximized_disp", False) or self.target.isMaximized()
        if is_max:
            norm = getattr(self.target, "_normal_geom", None)
            if norm:
                self.target.setGeometry(norm)
            else:
                self.target.showNormal()
            self._max_btn.setText("□")
            self._max_btn.setToolTip("Maximize")
            self.target._is_maximized_disp = False
        else:
            self.target._normal_geom = self.target.geometry()
            scr = QApplication.screenAt(self.target.pos()) or QApplication.primaryScreen()
            if scr:
                self.target.setGeometry(scr.availableGeometry())
            else:
                self.target.showMaximized()
            self._max_btn.setText("❐")
            self._max_btn.setToolTip("Restore")
            self.target._is_maximized_disp = True

    def _handle_close(self):
        if self._on_close_cb:
            self._on_close_cb()
            return
        if hasattr(self.target, "close_display"):
            self.target.close_display()
        else:
            self.target.hide()


class DetachedTabWindow(_FramelessWindowResizeMixin, QWidget):
    """
    Independent floating frameless window for detached Jarvis Matrix tabs.
    Supports interactive resizing, draggable custom title bar, z-order focus,
    and docking back to the intelligence matrix.
    """

    re_dock_requested = pyqtSignal(int, object)  # (tab_index, tab_widget)

    def __init__(self, tab_index: int, title: str, content_widget: QWidget, parent=None):
        super().__init__(None)
        self.tab_index = tab_index
        self.title_text = title
        self.content_widget = content_widget
        self.setWindowTitle(f"JARVIS MATRIX — {title}")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(420, 340)
        self._init_frameless_resize()
        self._is_maximized_disp = False
        self._normal_geom = None

        screen = QApplication.primaryScreen().availableGeometry()
        w = min(680, max(460, screen.width() // 2))
        h = min(620, screen.height() - 100)
        x = max(screen.left() + 40, screen.center().x() - w // 2)
        y = screen.top() + (screen.height() - h) // 2
        self.setGeometry(x, y, w, h)

        self._build_ui()

    def _build_ui(self):
        self._outer = QFrame(self)
        self._outer.setObjectName("DetachedTabOuter")
        self._outer.setStyleSheet(f"""
            QFrame#DetachedTabOuter {{
                background: {getattr(C, 'PANEL_BG_ALPHA', 'rgba(10, 4, 6, 0.94)')};
                border: 1px solid rgba(255, 23, 68, 0.55);
                border-radius: 8px;
            }}
        """)
        self._outer.setMouseTracking(True)
        self._outer.installEventFilter(self)

        v_outer = QVBoxLayout(self)
        v_outer.setContentsMargins(0, 0, 0, 0)
        v_outer.addWidget(self._outer)

        lay = QVBoxLayout(self._outer)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        # Header bar
        hdr = QHBoxLayout()
        hdr.setSpacing(8)

        dot = QLabel("◈")
        dot.setFont(QFont("Montserrat", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._title_lbl = QLabel(f"JARVIS MATRIX — {self.title_text.upper()}")
        self._title_lbl.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent; letter-spacing: 1.2px;")
        hdr.addWidget(self._title_lbl, stretch=1)

        # Dock back button
        dock_btn = QPushButton("↙ DOCK")
        dock_btn.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        dock_btn.setFixedHeight(20)
        dock_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        dock_btn.setToolTip("Dock tab back into Intelligence Matrix")
        dock_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.15);
                color: {C.PRI};
                border: 1px solid rgba(255, 23, 68, 0.4);
                border-radius: 3px;
                padding: 0 6px;
            }}
            QPushButton:hover {{
                background: rgba(255, 23, 68, 0.4);
                color: #ffffff;
                border-color: {C.PRI};
            }}
        """)
        dock_btn.clicked.connect(self.dock_back)
        hdr.addWidget(dock_btn)

        # Standard window controls
        self.controls = StandardTitleBarControls(
            self,
            on_minimize=self.showMinimized,
            on_close=self.dock_back,
        )
        hdr.addWidget(self.controls)
        lay.addLayout(hdr)

        # Content container
        if self.content_widget is not None:
            self.content_widget.setParent(self._outer)
            lay.addWidget(self.content_widget, stretch=1)
            self.content_widget.show()

    def dock_back(self):
        self.re_dock_requested.emit(self.tab_index, self.content_widget)
        self.close()

    def closeEvent(self, e):
        self.re_dock_requested.emit(self.tab_index, self.content_widget)
        super().closeEvent(e)

    def mousePressEvent(self, e):
        if not self._handle_mouse_press(e):
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if not self._handle_mouse_move(e):
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if not self._handle_mouse_release(e):
            super().mouseReleaseEvent(e)

    def eventFilter(self, watched, event):
        if self._filter_child_mouse_event(watched, event):
            return True
        return super().eventFilter(watched, event)



def current_palette() -> dict[str, str]:
    """A snapshot of the accent-linked colours currently on class C."""
    return {k: getattr(C, k) for k in _HUE_LINKED}


def retheme_all_widgets(old: dict[str, str], new: dict[str, str]) -> None:
    """
    LIVE full theme change. Replaces the old palette colours with the new ones
    in EVERY widget's stylesheet across the app and repaints them. This way the
    colour change applies INSTANTLY across the whole interface — panels, buttons,
    borders included — not just the painted elements. No restart needed.
    """
    mapping = {old[k].lower(): new[k].lower()
               for k in old if old[k].lower() != new.get(k, old[k]).lower()}
    if not mapping:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                s2 = ss
                for o, n in mapping.items():
                    if o in s2:
                        s2 = s2.replace(o, n)
                if s2 != ss:
                    w.setStyleSheet(s2)
            w.update()
        except Exception:
            pass


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


# ── Windows GPU via NVML DLL (no subprocess, no console window) ──────────────
_nvml_lib: object = None   # cached ctypes DLL
_nvml_ok:  object = None   # None=untested, True=works, False=unavailable


def _nvml_gpu_windows() -> float:
    """Return NVIDIA GPU utilisation % using nvml.dll directly — zero subprocess."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        # Probe caches — GPU (NVML) and temperature (WMI) are the expensive
        # queries; initialise their handles once and reuse them instead of
        # rebuilding a connection on every poll.
        self._slow_tick = 0            # gpu/temp refreshed every 3rd cycle
        self._pynvml    = None         # cached pynvml module + device handle
        self._pynvml_h  = None
        self._pynvml_ok = None         # None=untested, False=unavailable here
        self._nv_unix   = None         # cached (lib, dev) for Linux/macOS NVML
        self._wmi_conn  = None         # cached WMI connection (creating one is slow)
        self._wmi_ok    = None         # None=untested, False=unavailable here
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(2.0)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        # GPU and temperature change slowly and are the most expensive probes
        # (NVML / WMI) — refresh them every 3rd cycle (~6 s) instead of every
        # cycle, reusing the previous reading in between.
        self._slow_tick = (self._slow_tick + 1) % 3
        if self._slow_tick == 1:
            gpu = self._get_gpu()
            tmp = self._get_temp()
        else:
            gpu = self.gpu
            tmp = self.tmp

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        # pynvml — subprocess-free; initialise once and reuse the handle.
        # Re-initialising NVML on every poll is slow, so cache it and stop
        # retrying pynvml entirely once it proves unavailable here.
        if self._pynvml_ok is not False:
            try:
                if self._pynvml_h is None:
                    import pynvml  # type: ignore
                    pynvml.nvmlInit()
                    self._pynvml    = pynvml
                    self._pynvml_h  = pynvml.nvmlDeviceGetHandleByIndex(0)
                    self._pynvml_ok = True
                return float(self._pynvml.nvmlDeviceGetUtilizationRates(self._pynvml_h).gpu)
            except Exception:
                self._pynvml_ok = False

        # Windows: nvml.dll via ctypes (already cached in _nvml_gpu_windows)
        if _OS == "Windows":
            return _nvml_gpu_windows()

        # Linux / macOS: libnvidia-ml shared lib via ctypes — init once, reuse
        try:
            import ctypes

            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

            if self._nv_unix is None:
                _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"
                nv = ctypes.CDLL(_lib)
                nv.nvmlInit_v2()
                dev = ctypes.c_void_p()
                nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
                self._nv_unix = (nv, dev)

            nv, dev = self._nv_unix
            u = _Util()
            nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
            return float(u.gpu)
        except Exception:
            pass

        return -1.0   # N/A — zero subprocess on all platforms

    def _get_temp(self) -> float:
        # psutil — works on Linux; occasionally Windows with driver support
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        # Windows: wmi module (pure Python COM, zero subprocess). Reuse a single
        # connection — building a fresh wmi.WMI() on every poll spins up a COM
        # connection each time and is very slow. Give up after one failure.
        if _OS == "Windows" and self._wmi_ok is not False:
            try:
                if self._wmi_conn is None:
                    import wmi  # type: ignore
                    self._wmi_conn = wmi.WMI(namespace="root/wmi")
                tz = self._wmi_conn.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                self._wmi_ok   = False
                self._wmi_conn = None

        return -1.0   # N/A — zero subprocess on all platforms

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()

def rgba(c: QColor, a: float | int) -> QColor:
    return QColor(c.red(), c.green(), c.blue(), max(0, min(255, int(a))))


_FONTS_LOADED = False


def _load_app_fonts():
    """Register custom bundled fonts (Montserrat) from the root fonts/ directory into QFontDatabase."""
    global _FONTS_LOADED
    if _FONTS_LOADED:
        return
    app = QApplication.instance()
    if app is None:
        return
    try:
        fonts_dir = BASE_DIR / "fonts"
        if fonts_dir.is_dir():
            for font_file in sorted(fonts_dir.glob("*.ttf")):
                QFontDatabase.addApplicationFont(str(font_file))
            _FONTS_LOADED = True
    except Exception as e:
        print(f"[Fonts] Warning: Could not register custom fonts: {e}")


def keena_font(size: int, bold: bool = False, spacing: float = 0.0) -> QFont:
    f = QFont("Bahnschrift SemiCondensed", size)
    f.setBold(bold)
    if spacing > 0.0:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f



__all__ = [
    "_WIN_HIDE",
    "_base_dir",
    "BASE_DIR",
    "CONFIG_DIR",
    "API_FILE",
    "_read_full_config",
    "APP_VERSION",
    "APP_PROTOCOL",
    "_DEFAULT_W",
    "_DEFAULT_H",
    "_MIN_W",
    "_MIN_H",
    "_LEFT_W",
    "_RIGHT_W",
    "_OS",
    "_ResizeEdge",
    "_detect_resize_edge",
    "_cursor_for_edge",
    "_FramelessWindowResizeMixin",
    "current_palette",
    "retheme_all_widgets",
    "qcol",
    "_nvml_gpu_windows",
    "_SysMetrics",
    "_metrics",
    "rgba",
    "_load_app_fonts",
    "keena_font",
    "HAVE_VIDEO",
    "QGraphicsVideoItem",
    "QAudioOutput",
    "QMediaPlayer",
    "HoloAvatar",
    "get_uicon",
]
