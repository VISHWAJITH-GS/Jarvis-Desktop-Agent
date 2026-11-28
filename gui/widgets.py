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


class MetricBar(QWidget):
    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0  # 0–100
        self._text = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        v = max(0.0, min(100.0, pct))
        if v == self._value and text == self._text:
            return  # unchanged — skip the repaint
        self._value = v
        self._text = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        bar_h = 4
        bar_y = H - bar_h - 5
        bar_w = W - 12
        bar_x = 6
        fill_w = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(QFont("Montserrat", 9, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

        p.end()


class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.document().setMaximumBlockCount(400)
        self.setFont(QFont("Montserrat", 9))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical, QScrollBar:horizontal {{
                background: transparent;
                width: 0px;
                height: 0px;
                border: none;
            }}
        """)
        self._ai_name_lc = "jarvis"
        self._last_msg = ""
        self._last_time = 0.0
        self._last_greeting = ""
        self._last_greeting_time = 0.0
        self._sig.connect(self._handle_append)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _handle_append(self, text: str):
        clean = str(text or "").strip()
        if not clean:
            return

        now_ts = time.time()
        # Deduplicate consecutive identical messages within 4 seconds
        if clean == self._last_msg and (now_ts - self._last_time < 4.0):
            return

        # Deduplicate startup greetings and identical system status announcements within 12 seconds
        is_greeting = any(
            g in clean.lower()
            for g in ["good morning", "good afternoon", "good evening", "listening for commands", "systems online", "voice session active"]
        )
        if is_greeting and self._last_greeting == clean and (now_ts - self._last_greeting_time < 12.0):
            return

        if is_greeting:
            self._last_greeting = clean
            self._last_greeting_time = now_ts

        self._last_msg = clean
        self._last_time = now_ts

        timestamp_str = time.strftime("%H:%M:%S")
        tl = clean.lower()
        _ai_pfx = f"{self._ai_name_lc}:"

        if tl.startswith("you:"):
            spk = "YOU"
            body = clean[4:].strip()
            bg = "rgba(255, 255, 255, 0.03)"
            border = "3px solid #555555"
            tag_color = "#ffffff"
            text_color = "#ffffff"
        elif tl.startswith(_ai_pfx) or tl.startswith("jarvis:"):
            spk = "JARVIS"
            body = clean.split(":", 1)[1].strip()
            bg = "rgba(255, 30, 66, 0.05)"
            border = "3px solid #FF1E42"
            tag_color = "#FF1E42"
            text_color = C.TEXT
        elif tl.startswith("file:"):
            spk = "FILE"
            body = clean[5:].strip()
            bg = "rgba(0, 230, 118, 0.05)"
            border = "3px solid #00e676"
            tag_color = "#00e676"
            text_color = C.TEXT
        elif "err" in tl or tl.startswith("error:"):
            spk = "ERROR"
            body = clean.split(":", 1)[1].strip() if ":" in clean else clean
            bg = "rgba(255, 23, 68, 0.08)"
            border = "3px solid #ff1744"
            tag_color = "#ff1744"
            text_color = "#ff8a80"
        else:
            spk = "SYS"
            body = clean[4:].strip() if tl.startswith("sys:") else clean
            bg = "rgba(0, 0, 0, 0.22)"
            border = "3px solid rgba(255, 23, 68, 0.35)"
            tag_color = "#d69da4"
            text_color = "#d69da4"

        esc_body = (
            body.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )

        html_block = (
            f'<div style="background: {bg}; border-left: {border}; padding: 4px 8px; margin: 3px 0; border-radius: 2px;">'
            f'<span style="font-family: \'Consolas\', monospace; font-size: 8pt; color: #777777;">[{timestamp_str}]</span>'
            f'<span style="font-family: \'Montserrat\', sans-serif; font-weight: bold; font-size: 8pt; color: {tag_color}; letter-spacing: 1px;"> {spk}:</span>'
            f'<div style="color: {text_color}; font-size: 8.5pt; margin-top: 2px; line-height: 1.35;">{esc_body}</div>'
            f'</div>'
        )

        cur = self.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        cur.insertHtml(html_block)
        self.setTextCursor(cur)
        self.ensureCursorVisible()


_FILE_ICONS = {
    "image": ("🖼", "#00d4ff"),
    "video": ("🎬", "#ff6b00"),
    "audio": ("🎵", "#cc44ff"),
    "pdf": ("📄", "#ff4444"),
    "word": ("📝", "#4488ff"),
    "excel": ("📊", "#44bb44"),
    "code": ("💻", "#ffcc00"),
    "archive": ("📦", "#ff8844"),
    "pptx": ("📊", "#ff6622"),
    "text": ("📃", "#aaaaaa"),
    "data": ("🔧", "#88ddff"),
    "unknown": ("📎", "#888888"),
    "folder": ("📁", "#ffcc00"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg", "ico"], "image"),
    **dict.fromkeys(["mp4", "avi", "mov", "mkv", "wmv", "flv", "webm", "m4v"], "video"),
    **dict.fromkeys(["mp3", "wav", "ogg", "m4a", "aac", "flac", "wma", "opus"], "audio"),
    **dict.fromkeys(["pdf"], "pdf"),
    **dict.fromkeys(["doc", "docx"], "word"),
    **dict.fromkeys(["xls", "xlsx", "ods"], "excel"),
    **dict.fromkeys(["ppt", "pptx"], "pptx"),
    **dict.fromkeys(
        [
            "py",
            "js",
            "ts",
            "jsx",
            "tsx",
            "html",
            "css",
            "java",
            "c",
            "cpp",
            "cs",
            "go",
            "rs",
            "rb",
            "php",
            "swift",
            "kt",
            "sh",
            "sql",
            "lua",
        ],
        "code",
    ),
    **dict.fromkeys(["zip", "rar", "tar", "gz", "7z", "bz2", "xz"], "archive"),
    **dict.fromkeys(["txt", "md", "rst", "log"], "text"),
    **dict.fromkeys(["csv", "tsv", "json", "xml"], "data"),
}


def _file_category(path: Path) -> str:
    if path.is_dir():
        return "folder"
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")


# Folders nobody means when they say "this project": dependencies, build output,
# version control. The same list dev_agent skips, so the count on the card is
# the count of files the coding model will actually look at.
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "venv",
    ".venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".next",
    "target",
    ".jarvis_backup",
}


def _count_files(folder: Path, cap: int = 5000) -> int:
    """Files in a project folder, stopping at `cap` — a whole drive dropped by
    mistake must not freeze the window while it is counted."""
    import os

    n = 0
    for _root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        n += len(files)
        if n >= cap:
            return cap
    return n


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024**2:
        return f"{size / 1024:.1f} KB"
    elif size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    else:
        return f"{size / 1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(28)
        self._current_file: str | None = None
        self._folder_files = 0
        self._hovering = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        if not (self._hovering or self._drag_over):
            return
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True
            self.setFixedHeight(75)
            self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False
        self.setFixedHeight(28)
        self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        self.setFixedHeight(28)
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file() or Path(path).is_dir():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True
        self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False
        self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None
        self._canvas.update()

    def _browse(self):
        # Qt has no single dialog that picks "a file or a folder", so the click
        # asks which — a folder is what the coding model works on.
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {C.DARK}; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER_B}; font-family: 'Montserrat'; }}"
            f"QMenu::item {{ padding: 6px 18px; }}"
            f"QMenu::item:selected {{ background: {C.PRI_GHO}; }}"
        )
        pick_file = menu.addAction("📄  Choose a file…")
        pick_dir = menu.addAction("📁  Choose a folder…  (for coding)")
        chosen = menu.exec(QCursor.pos())
        if chosen is pick_dir:
            path = QFileDialog.getExistingDirectory(self, "Select a project folder for JARVIS", str(Path.home()))
            if path:
                self._set_file(path)
            return
        if chosen is not pick_file:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select a file for JARVIS",
            str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._folder_files = _count_files(Path(path)) if Path(path).is_dir() else 0
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z = self._z
        W, H = self.width(), self.height()
        pad = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = qcol("#001a24" if z._drag_over else ("#001218" if z._hovering else C.PANEL))
        p.setBrush(QBrush(bg_col))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:
            border_col = qcol(C.GREEN, 200)
        elif z._drag_over:
            border_col = qcol(C.PRI, 230)
        elif z._hovering:
            border_col = qcol(C.BORDER_B, 200)
        else:
            border_col = qcol(C.BORDER, 160)

        pen = QPen(border_col, 1.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:
            self._paint_file(p, W, H)
        elif z._drag_over:
            self._paint_drag_over(p, W, H)
        else:
            self._paint_idle(p, W, H, z._hovering)

        p.end()

    def _paint_idle(self, p, W, H, hover):
        if H <= 36:
            p.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
            col = qcol(C.PRI if hover else C.TEXT_DIM)
            p.setPen(QPen(col, 1))
            p.drawText(
                QRectF(0, 0, W, H),
                Qt.AlignmentFlag.AlignCenter,
                "📎  ATTACH FILE / FOLDER  (CLICK TO BROWSE OR DRAG & DROP)",
            )
            return

        cx, cy = W / 2, H / 2
        col = qcol(C.PRI_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
        p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))
        p.setFont(QFont("Montserrat", 8))
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 8, W, 16), Qt.AlignmentFlag.AlignCenter, "Drop a file or folder  or  Click to Browse")
        p.setFont(QFont("Montserrat", 7))
        p.setPen(QPen(qcol("#1a4a5a"), 1))
        p.drawText(
            QRectF(0, cy + 24, W, 14),
            Qt.AlignmentFlag.AlignCenter,
            "Images · Video · Audio · PDF · Docs · Code · Project folders",
        )

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(QFont("Montserrat", 20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        try:
            size_str = _fmt_size(path.stat().st_size)
        except OSError:  # moved or deleted since loading
            size_str = "gone"
        ext_str = path.suffix.upper().lstrip(".") or "FILE"
        if cat == "folder":
            n = self._z._folder_files
            ext_str, size_str = "FOLDER", f"{n}{'+' if n >= 5000 else ''} files"

        if H <= 36:
            p.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
            p.setPen(QPen(qcol(C.WHITE), 1))
            name = path.name if len(path.name) <= 28 else path.name[:25] + "..."
            p.drawText(
                QRectF(10, 0, W - 44, H),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"📎 [{ext_str}] {name}  ·  {size_str}",
            )
            p.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
            p.setPen(QPen(qcol(C.RED, 200), 1))
            p.drawText(QRectF(W - 30, 0, 24, H), Qt.AlignmentFlag.AlignCenter, "✕")
            return

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Montserrat", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(
            QRectF(tx, H * 0.18 + 18, tw, 14),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"{ext_str}  ·  {size_str}",
        )

        p.setFont(QFont("Montserrat", 6))
        p.setPen(QPen(qcol("#1e5c6a"), 1))
        par = str(path.parent)
        if len(par) > 42:
            par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont("Montserrat", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")


class PillToggleSwitch(QWidget):
    """
    Inline pill toggle switch ([ ON | OFF ]) with sleek cyberpunk styling.
    """

    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = False, label_on: str = "ON", label_off: str = "OFF", parent=None):
        super().__init__(parent)
        self._checked = bool(checked)
        self._label_on = label_on
        self._label_off = label_off
        self.setFixedSize(56, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool):
        if self._checked != bool(checked):
            self._checked = bool(checked)
            self.update()
            self.toggled.emit(self._checked)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)
            e.accept()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = (h - 2) / 2.0

        track_path = QPainterPath()
        track_path.addRoundedRect(QRectF(1, 1, w - 2, h - 2), r, r)

        if self._checked:
            bg_col = QColor(255, 23, 68, 70)
            border_col = QColor(255, 23, 68, 220)
            knob_col = QColor(255, 23, 68)
            txt = self._label_on
            txt_col = QColor(255, 255, 255)
            knob_x = w - h + 2
            txt_rect = QRectF(2, 0, w - h, h)
        else:
            bg_col = QColor(14, 4, 10, 200)
            border_col = QColor(80, 20, 30, 180)
            knob_col = QColor(130, 40, 50)
            txt = self._label_off
            txt_col = QColor(180, 140, 150)
            knob_x = 2
            txt_rect = QRectF(h, 0, w - h, h)

        p.fillPath(track_path, QBrush(bg_col))
        p.setPen(QPen(border_col, 1))
        p.drawPath(track_path)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(knob_col))
        p.drawEllipse(QRectF(knob_x, 2, h - 4, h - 4))

        p.setPen(txt_col)
        p.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        p.drawText(txt_rect, Qt.AlignmentFlag.AlignCenter, txt)
        p.end()


class AudioWaveformVisualizer(QWidget):
    """
    Real-time audio waveform / input level meter visualizer reacting to mic input.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(30)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._level = 0.0
        self._bars = [0.05] * 24
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(50)

    def set_level(self, level: float):
        self._level = max(0.0, min(1.0, float(level)))

    def _animate(self):
        self._phase += 0.18
        for i in range(len(self._bars)):
            target = max(0.08, (0.15 + 0.85 * self._level) * abs(math.sin(self._phase + i * 0.35)))
            self._bars[i] = self._bars[i] * 0.65 + target * 0.35
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            p.end()
            return

        n = len(self._bars)
        spacing = 3.0
        bar_w = max(2.0, (w - (n - 1) * spacing) / n)

        for i, val in enumerate(self._bars):
            bh = max(3.0, val * (h - 4))
            x = i * (bar_w + spacing)
            y = (h - bh) / 2.0
            grad = QLinearGradient(x, y, x, y + bh)
            grad.setColorAt(0.0, QColor(255, 82, 82, 230))
            grad.setColorAt(1.0, QColor(255, 23, 68, 160))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(x, y, bar_w, bh), 1.5, 1.5)
        p.end()


    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)
