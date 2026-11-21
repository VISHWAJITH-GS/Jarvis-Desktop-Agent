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

from gui.widgets import *


class BottomChatDock(_FramelessWindowResizeMixin, QWidget):
    """
    Floating rectangular transcript and command input dock positioned at the
    bottom-center of the screen with wide width and short height.
    """

    def __init__(self, main_win: "MainWindow", parent=None):
        super().__init__(parent)
        self._main_win = main_win
        self.setWindowTitle("JARVIS Comm Link")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(480, 68)
        self._init_frameless_resize()

        screen = QApplication.primaryScreen().availableGeometry()
        dock_w = min(840, max(540, screen.width() - 360))
        dock_h = 168
        dock_x = screen.left() + (screen.width() - dock_w) // 2
        dock_y = screen.bottom() - dock_h - 14
        self.setGeometry(dock_x, dock_y, dock_w, dock_h)

        self._drag_start = None
        self._win_start = None
        self._collapsed = False
        self._build_ui()

    def _build_ui(self):
        self._outer = QFrame(self)
        self._outer.setObjectName("ChatDockOuter")
        self._outer.setStyleSheet(f"""
            QFrame#ChatDockOuter {{
                background: rgba(10, 2, 4, 240);
                border: 1px solid rgba(255, 23, 68, 0.45);
                border-radius: 6px;
            }}
        """)
        self._outer.setMouseTracking(True)
        self._outer.installEventFilter(self)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self._outer)

        lay = QVBoxLayout(self._outer)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(6)

        tag = QLabel("JARVIS COMM LOG  -  LIVE TRANSCRIPT")
        tag.setFont(keena_font(9, bold=True, spacing=1.2))
        tag.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(tag)
        hdr.addStretch()

        self._min_btn = QPushButton("MIN")
        self._min_btn.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        self._min_btn.setFixedSize(30, 18)
        self._min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._min_btn.setToolTip("Minimize/Restore log view")
        self._min_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.15);
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
            }}
            QPushButton:hover {{
                background: rgba(255, 23, 68, 0.35);
                border-color: {C.PRI};
            }}
        """)
        self._min_btn.clicked.connect(self._toggle_collapse)
        hdr.addWidget(self._min_btn)

        close_btn = QPushButton("")
        close_btn.setIcon(get_uicon("cross", color="#ff5252", size=11))
        close_btn.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        close_btn.setFixedSize(22, 18)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setToolTip("Hide Comm Link (reopen via HUD CHAT button)")
        close_btn.setStyleSheet(self._min_btn.styleSheet())
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        self._dock_log = LogWidget()
        self._dock_log.setFixedHeight(82)
        self._dock_log.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._dock_log.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._dock_log.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(8, 2, 8, 190);
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 4px 6px;
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
        self._log = self._dock_log
        lay.addWidget(self._dock_log, stretch=1)

        inp_row = QHBoxLayout()
        inp_row.setContentsMargins(0, 0, 0, 0)
        inp_row.setSpacing(6)

        self._input = QLineEdit()
        self._input.setFixedHeight(28)
        self._input.setPlaceholderText("Type a command or query... [Enter to send]")
        self._input.setFont(keena_font(9))
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(24, 7, 22, 210);
                color: #ffffff;
                border: 1px solid {C.BORDER_B};
                border-radius: 4px;
                padding: 2px 8px;
            }}
            QLineEdit:focus {{
                border: 1px solid {C.PRI};
                background: rgba(35, 10, 32, 230);
            }}
        """)
        self._input.returnPressed.connect(self._on_dock_send)
        inp_row.addWidget(self._input, stretch=1)

        self._attach_btn = QPushButton("FILE")
        self._attach_btn.setFixedHeight(28)
        self._attach_btn.setFont(keena_font(8, bold=True))
        self._attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._attach_btn.setToolTip("Attach or browse file")
        self._attach_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(35, 10, 32, 210);
                color: {C.TEXT_MED};
                border: 1px solid {C.BORDER_B};
                border-radius: 4px;
                padding: 2px 8px;
            }}
            QPushButton:hover {{
                border-color: {C.PRI};
                color: #ffffff;
            }}
        """)
        self._attach_btn.clicked.connect(self._main_win._browse_file)
        inp_row.addWidget(self._attach_btn)

        self._send_btn = QPushButton("SEND")
        self._send_btn.setFixedSize(50, 28)
        self._send_btn.setFont(keena_font(8, bold=True))
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.25);
                color: #ffffff;
                border: 1px solid {C.PRI};
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background: rgba(255, 23, 68, 0.45);
            }}
        """)
        self._send_btn.clicked.connect(self._on_dock_send)
        inp_row.addWidget(self._send_btn)
        lay.addLayout(inp_row)

    def _on_dock_send(self):
        txt = self._input.text().strip()
        if not txt:
            return
        if hasattr(self._main_win, "_input") and hasattr(self._main_win, "_send"):
            self._main_win._input.setText(txt)
            self._main_win._send()
        elif hasattr(self._main_win, "_send_text_command"):
            self._main_win._send_text_command(txt)
        self._input.clear()

    def _toggle_collapse(self):
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._log.hide()
            self.setFixedHeight(68)
            self._min_btn.setText("+")
        else:
            self._log.show()
            self.setFixedHeight(168)
            self._min_btn.setText("—")

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


def format_visual_content_html(raw_text: str) -> str:
    """
    Format raw assistant / search output into easily interpretable visual HTML:
    - Tables: Formatted as sleek holographic matrix tables with glowing headers, borders, and alternating rows.
    - Progress / Metrics / Bar Charts: Detected percentages/ratios rendered with glowing horizontal bars.
    - Diagrams / Flowcharts: Step sequences ('A -> B -> C') converted into visual flow badge pipelines.
    - Markdown: Formatted with bold highlights, bullets, code blocks, and structured typography.
    """
    if not raw_text:
        return ""

    import re

    lines = raw_text.replace("\r\n", "\n").split("\n")
    out_html = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            out_html.append("<div style='height: 6px;'></div>")
            i += 1
            continue

        # 1. Check for Markdown Table
        if "|" in stripped and (stripped.startswith("|") or stripped.endswith("|") or stripped.count("|") >= 2):
            table_lines = []
            while i < len(lines) and ("|" in lines[i].strip() and len(lines[i].strip()) > 1):
                table_lines.append(lines[i].strip())
                i += 1

            rows = []
            has_header_sep = False
            for tl in table_lines:
                cells = [c.strip() for c in tl.strip("|").split("|")]
                if all(set(c).issubset({"-", ":", " "}) for c in cells if c):
                    has_header_sep = True
                    continue
                rows.append(cells)

            if rows:
                t_html = [
                    '<table width="100%" cellpadding="6" cellspacing="0" style="border: 1px solid rgba(255,23,68,0.45); background: rgba(14,3,7,0.85); border-radius: 4px; margin: 8px 0;">'
                ]
                start_row = 0
                if has_header_sep and len(rows) > 0:
                    t_html.append('<tr bgcolor="rgba(45,8,16,0.95)">')
                    for h_cell in rows[0]:
                        t_html.append(
                            f'<th align="left" style="color: #ff5252; font-family: \'Montserrat\'; font-size: 11px; border-bottom: 1.5px solid #ff1744; padding: 6px;"><b>{h_cell}</b></th>'
                        )
                    t_html.append("</tr>")
                    start_row = 1

                for r_idx, row in enumerate(rows[start_row:]):
                    bg = "rgba(24,5,11,0.70)" if r_idx % 2 == 0 else "rgba(12,2,5,0.50)"
                    t_html.append(f'<tr bgcolor="{bg}">')
                    for cell in row:
                        pct_match = re.search(r"(\d{1,3})%", cell)
                        bar_html = ""
                        if pct_match:
                            val = min(100, max(0, int(pct_match.group(1))))
                            bar_html = f'<table width="100%" cellpadding="0" cellspacing="0" style="border: 1px solid #ff1744; background: #120306; border-radius: 2px; margin-top: 3px;"><tr><td width="{val}%" bgcolor="#ff1744" height="6"></td><td width="{100 - val}%"></td></tr></table>'
                        t_html.append(
                            f"<td style=\"color: #e0e0e0; font-family: 'Montserrat'; font-size: 11px; border-bottom: 1px solid rgba(255,23,68,0.18); padding: 5px;\">{cell}{bar_html}</td>"
                        )
                    t_html.append("</tr>")
                t_html.append("</table>")
                out_html.append("".join(t_html))
            continue

        # 2. Check for Diagram / Flowchart ('->' or '➔' with >= 2 transitions)
        if any(arrow in line for arrow in ("->", "-->", "➔", "==>")) and not line.startswith("<"):
            tokens = re.split(r"\s*(?:-->|->|➔|==>)\s*", stripped)
            if len(tokens) >= 2 and all(len(t) < 50 for t in tokens):
                f_html = [
                    '<div style="margin: 8px 0; padding: 8px; background: rgba(18, 4, 8, 0.85); border: 1px solid rgba(255,23,68,0.35); border-radius: 4px;">'
                ]
                f_html.append(
                    "<div style=\"color: #ff5252; font-family: 'Montserrat'; font-size: 10px; font-weight: bold; margin-bottom: 6px;\">◈ SYSTEM FLOW DIAGRAM</div>"
                )
                f_html.append('<table cellpadding="4" cellspacing="0"><tr>')
                for s_idx, tok in enumerate(tokens):
                    f_html.append(
                        f"<td style=\"background: rgba(255,23,68,0.22); border: 1px solid #ff1744; color: #ffffff; border-radius: 3px; font-family: 'Montserrat'; font-size: 11px; font-weight: bold; padding: 4px 8px;\">{tok}</td>"
                    )
                    if s_idx < len(tokens) - 1:
                        f_html.append(
                            '<td style="color: #ff1744; font-size: 14px; font-weight: bold; padding: 0 4px;">➔</td>'
                        )
                f_html.append("</tr></table></div>")
                out_html.append("".join(f_html))
                i += 1
                continue

        # 3. Check for Standalone Metric / Bar Chart
        metric_match = re.match(r"^([A-Za-z0-9_\- ]+?)[:=]\s*(\d{1,3})%", stripped)
        if metric_match:
            lbl, pct_str = metric_match.group(1).strip(), metric_match.group(2)
            pct = min(100, max(0, int(pct_str)))
            b_html = f'''
            <div style="margin: 6px 0; padding: 6px 8px; background: rgba(22, 5, 10, 0.7); border: 1px solid rgba(255,23,68,0.3); border-radius: 4px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="left" style="color: #ffffff; font-family: \'Montserrat\'; font-size: 11px; font-weight: bold;">{lbl}</td>
                  <td align="right" style="color: #00e676; font-family: \'Montserrat\'; font-size: 11px; font-weight: bold;">{pct}%</td>
                </tr>
                <tr>
                  <td colspan="2" style="padding-top: 4px;">
                    <table width="100%" cellpadding="0" cellspacing="0" style="border: 1px solid #ff1744; background: #120306; border-radius: 2px;">
                      <tr><td width="{pct}%" bgcolor="#ff1744" height="8"></td><td width="{100 - pct}%"></td></tr>
                    </table>
                  </td>
                </tr>
              </table>
            </div>'''
            out_html.append(b_html)
            i += 1
            continue

        # 4. Regular text & Markdown formatting
        formatted = stripped
        if formatted.startswith("###"):
            formatted = f"<div style=\"color: #ff5252; font-family: 'Montserrat'; font-size: 13px; font-weight: bold; margin: 8px 0 3px 0;\">◈ {formatted[3:].strip()}</div>"
        elif formatted.startswith("##"):
            formatted = f"<div style=\"color: #ff1744; font-family: 'Montserrat'; font-size: 14px; font-weight: bold; margin: 10px 0 4px 0;\">◈ {formatted[2:].strip()}</div>"
        elif formatted.startswith(("* ", "- ")):
            bullet_txt = formatted[2:].strip()
            bullet_txt = re.sub(r"\*\*(.+?)\*\*", r'<b style="color:#ffffff;">\1</b>', bullet_txt)
            bullet_txt = re.sub(
                r"`(.+?)`",
                r'<code style="background:rgba(255,23,68,0.18); color:#80cbc4; padding:1px 4px; border-radius:2px;">\1</code>',
                bullet_txt,
            )
            formatted = f"<div style=\"color: #e0e0e0; font-family: 'Montserrat'; font-size: 11px; padding-left: 8px;\">• {bullet_txt}</div>"
        else:
            formatted = re.sub(r"\*\*(.+?)\*\*", r'<b style="color:#ffffff;">\1</b>', formatted)
            formatted = re.sub(
                r"`(.+?)`",
                r'<code style="background:rgba(255,23,68,0.18); color:#80cbc4; padding:1px 4px; border-radius:2px;">\1</code>',
                formatted,
            )
            formatted = f"<div style=\"color: #d0d0d0; font-family: 'Montserrat'; font-size: 11px; margin: 2px 0;\">{formatted}</div>"

        out_html.append(formatted)
        i += 1

    return "".join(out_html)


class ScreenGlowOverlay(QWidget):
    """
    Full-screen transparent HUD overlay that illuminates a glowing red border around
    the edges of the screen whenever JARVIS observes or captures the display.
    """

    def __init__(self, parent=None):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._glow_alpha = 0
        self._fade_timer = QTimer(self)
        self._fade_timer.timeout.connect(self._on_fade_step)
        self._auto_hide_timer = QTimer(self)
        self._auto_hide_timer.setSingleShot(True)
        self._auto_hide_timer.timeout.connect(self.hide_glow)

        self.reposition()

    def reposition(self):
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

    def trigger_glow(self, duration_ms: int = 2500):
        """Trigger glowing red screen edges for duration_ms."""
        self.reposition()
        self._glow_alpha = 255
        self.show()
        self.raise_()
        self.update()
        self._fade_timer.stop()
        self._auto_hide_timer.start(duration_ms)

    def hide_glow(self):
        self._fade_timer.start(30)

    def _on_fade_step(self):
        self._glow_alpha = max(0, self._glow_alpha - 25)
        if self._glow_alpha <= 0:
            self._fade_timer.stop()
            self.hide()
        self.update()

    def paintEvent(self, event):
        if not self.isVisible() or self._glow_alpha <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        w = self.width()
        h = self.height()
        alpha = self._glow_alpha

        # Draw multi-layered glowing border from outer boundary inward
        layers = [
            (0, 3, QColor(255, 23, 68, min(255, int(alpha * 1.0)))),  # crisp core border
            (3, 3, QColor(255, 23, 68, min(255, int(alpha * 0.70)))),  # intense inner glow
            (6, 4, QColor(255, 23, 68, min(255, int(alpha * 0.40)))),  # soft halo
            (10, 6, QColor(255, 23, 68, min(255, int(alpha * 0.18)))),  # diffused atmospheric glow
            (16, 8, QColor(255, 23, 68, min(255, int(alpha * 0.08)))),  # outermost ambient glow
        ]

        for offset, stroke, color in layers:
            pen = QPen(color, stroke)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(offset, offset, w - 2 * offset - 1, h - 2 * offset - 1)

        # Top-center cybernetic HUD indicator pill
        pill_w = 260
        pill_h = 24
        px = (w - pill_w) // 2
        py = 6

        # Draw pill background
        painter.setPen(QPen(QColor(255, 23, 68, min(255, int(alpha * 0.85))), 1))
        painter.setBrush(QColor(18, 3, 6, min(240, int(alpha * 0.95))))
        painter.drawRoundedRect(px, py, pill_w, pill_h, 4, 4)

        # Draw indicator text
        painter.setPen(QColor(255, 255, 255, alpha))
        painter.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        painter.drawText(px, py, pill_w, pill_h, Qt.AlignmentFlag.AlignCenter, "◉ VISION ACTIVE  -  DISPLAY")
        painter.end()


class LeftSearchDock(_FramelessWindowResizeMixin, QWidget):
    """
    Floating pop-up panel for search results, briefings, document reviews,
    and quizzes, positioned along the left edge of the screen.
    """

    def __init__(self, main_win: "MainWindow", parent=None):
        super().__init__(parent)
        self._main_win = main_win
        self.setWindowTitle("JARVIS Search & Intel")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(380, 400)
        self._init_frameless_resize()

        screen = QApplication.primaryScreen().availableGeometry()
        dock_w = min(480, max(380, screen.width() // 3))
        dock_h = min(580, screen.height() - 140)
        dock_x = screen.left() + 16
        dock_y = screen.top() + (screen.height() - dock_h) // 2
        self.setGeometry(dock_x, dock_y, dock_w, dock_h)

        self._build_ui()

    def _build_ui(self):
        self._outer = QFrame(self)
        self._outer.setObjectName("SearchDockOuter")
        self._outer.setStyleSheet(f"""
            QFrame#SearchDockOuter {{
                background: rgba(10, 2, 4, 245);
                border: 1px solid rgba(255, 23, 68, 0.45);
                border-radius: 6px;
            }}
        """)
        self._outer.setMouseTracking(True)
        self._outer.installEventFilter(self)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self._outer)

        lay = QVBoxLayout(self._outer)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        # Unified container: contained panels (_content_panel, _quiz_panel) manage their own clean headers
        self._panel_cont = QWidget()
        self._panel_cont.setStyleSheet("background: transparent;")
        p_lay = QVBoxLayout(self._panel_cont)
        p_lay.setContentsMargins(0, 0, 0, 0)
        p_lay.setSpacing(0)
        p_lay.addWidget(self._main_win._content_panel)
        p_lay.addWidget(self._main_win._quiz_panel)
        lay.addWidget(self._panel_cont, stretch=1)

        # Backward compatibility properties pointing to the active content title / timestamp
        self._title_lbl = getattr(self._main_win, "_content_title_lbl", None)
        self._ts_lbl = getattr(self._main_win, "_content_ts_lbl", None)

    def popup(self, title: str = "", timestamp: str = ""):
        if title and hasattr(self._main_win, "_content_title_lbl"):
            clean_title = title.replace("//", "-").strip()
            # Clean out duplicate prefixes
            if clean_title.upper().startswith("SEARCH — ") or clean_title.upper().startswith("SEARCH - "):
                clean_title = clean_title[9:].strip()
            elif clean_title.upper().startswith("SEARCH:"):
                clean_title = clean_title[7:].strip()

            if not any(
                clean_title.upper().startswith(p)
                for p in ("SEARCH", "REVIEW", "QUIZ", "CODE AGENT", "BRIEFING", "TASK")
            ):
                display_title = f"SEARCH — {clean_title.upper()[:40]}"
            else:
                display_title = clean_title.upper()[:48]
            self._main_win._content_title_lbl.setText(display_title)

        if timestamp and hasattr(self._main_win, "_content_ts_lbl"):
            self._main_win._content_ts_lbl.setText(timestamp)

        screen = QApplication.primaryScreen().availableGeometry()
        dock_w = min(480, max(380, screen.width() // 3))
        dock_h = min(580, screen.height() - 140)
        self._dock_h = dock_h
        self._collapsed = False
        if hasattr(self._main_win, "_content_display"):
            self._main_win._content_display.show()
        if hasattr(self._main_win, "_content_sep"):
            self._main_win._content_sep.show()
        if hasattr(self._main_win, "_content_min_btn"):
            self._main_win._content_min_btn.setText(" MIN")
            self._main_win._content_min_btn.setIcon(get_uicon("minus", color=C.PRI, size=11))
            self._main_win._content_min_btn.setToolTip("Minimize search display")

        if self._main_win and hasattr(self._main_win, "_search_dock_minimized_to_bar"):
            self._main_win._search_dock_minimized_to_bar = False
            self._main_win._update_dots_overlay()

        dock_x = screen.left() + 16
        dock_y = screen.top() + (screen.height() - dock_h) // 2
        self.setGeometry(dock_x, dock_y, dock_w, dock_h)
        self.show()
        self.raise_()

    def minimize_to_dock(self):
        self._collapsed = True
        self.setFixedHeight(46)
        if hasattr(self._main_win, "_content_display"):
            self._main_win._content_display.hide()
        if hasattr(self._main_win, "_content_sep"):
            self._main_win._content_sep.hide()
        if hasattr(self._main_win, "_content_min_btn"):
            self._main_win._content_min_btn.setText(" RESTORE")
            self._main_win._content_min_btn.setIcon(get_uicon("restore", color=C.PRI, size=11))
            self._main_win._content_min_btn.setToolTip("Restore search display")
        self.hide()
        if self._main_win:
            self._main_win._search_dock_minimized_to_bar = True
            self._main_win._update_dots_overlay()
            if hasattr(self._main_win, "write_log"):
                self._main_win.write_log("SYS: Search display minimized to left dock.")

    def restore_from_dock(self):
        self._collapsed = False
        if hasattr(self._main_win, "_content_display"):
            self._main_win._content_display.show()
        if hasattr(self._main_win, "_content_sep"):
            self._main_win._content_sep.show()
        screen = QApplication.primaryScreen().availableGeometry()
        dock_h = getattr(self, "_dock_h", min(580, screen.height() - 140))
        self.setFixedHeight(dock_h)
        if hasattr(self._main_win, "_content_min_btn"):
            self._main_win._content_min_btn.setText(" MIN")
            self._main_win._content_min_btn.setIcon(get_uicon("minus", color=C.PRI, size=11))
            self._main_win._content_min_btn.setToolTip("Minimize search display")
        if self._main_win:
            self._main_win._search_dock_minimized_to_bar = False
            self._main_win._update_dots_overlay()
        self.show()
        self.raise_()
        self.activateWindow()
        if self._main_win and hasattr(self._main_win, "write_log"):
            self._main_win.write_log("SYS: Search display restored.")

    def toggle_minimize(self):
        if self.isVisible():
            self.minimize_to_dock()
        else:
            self.restore_from_dock()

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


class SlideDownAlert(QWidget):
    """
    Top-center slide-down system alert pop-up message.
    Animates down from the top edge of the screen with a glowing JARVIS red background
    and bold black text. Automatically slides back up after timeout or when dismissed.
    """

    def __init__(self, parent=None):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._pos_anim = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._slide_up)

        self._build_ui()

    def _build_ui(self):
        self._frame = QFrame(self)
        self._frame.setObjectName("SlideAlertFrame")
        self._frame.setStyleSheet("""
            QFrame#SlideAlertFrame {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #ff1744, stop:0.4 #d50000, stop:1.0 #b71c1c);
                border: 1.5px solid #ff5252;
                border-radius: 6px;
            }
        """)

        glow = QGraphicsDropShadowEffect(self._frame)
        glow.setBlurRadius(12)
        glow.setColor(QColor(255, 23, 68, 180))
        glow.setOffset(0, 1)
        self._frame.setGraphicsEffect(glow)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(5, 5, 5, 5)
        main_lay.addWidget(self._frame)

        lay = QHBoxLayout(self._frame)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(6)

        icon_lbl = QLabel("⚠")
        icon_lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        icon_lbl.setStyleSheet("color: #000000; background: transparent;")
        lay.addWidget(icon_lbl)

        v_text = QVBoxLayout()
        v_text.setSpacing(0)

        self._title_lbl = QLabel("SYSTEM ALERT")
        self._title_lbl.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet("color: #000000; background: transparent; letter-spacing: 0.5px;")
        v_text.addWidget(self._title_lbl)

        self._msg_lbl = QLabel("")
        self._msg_lbl.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        self._msg_lbl.setStyleSheet("color: #000000; background: transparent;")
        self._msg_lbl.setWordWrap(True)
        v_text.addWidget(self._msg_lbl)
        lay.addLayout(v_text, stretch=1)

        close_btn = QPushButton("✕")
        close_btn.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        close_btn.setFixedSize(14, 14)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background: rgba(0, 0, 0, 0.18);
                color: #000000;
                border: 1px solid rgba(0, 0, 0, 0.4);
                border-radius: 2px;
            }
            QPushButton:hover {
                background: rgba(0, 0, 0, 0.45);
                color: #ffffff;
            }
        """)
        close_btn.clicked.connect(self._slide_up)
        lay.addWidget(close_btn)

    def popup_alert(self, title: str, message: str, duration_ms: int = 8500):
        self._timer.stop()
        clean_title = (title or "SYSTEM ALERT").replace("//", "-").strip()
        clean_msg = str(message or "").replace("//", "-").strip()
        self._title_lbl.setText(clean_title.upper())
        self._msg_lbl.setText(clean_msg)

        screen = QApplication.primaryScreen().availableGeometry()
        fm_msg = self._msg_lbl.fontMetrics()
        fm_title = self._title_lbl.fontMetrics()
        text_w = max(fm_title.horizontalAdvance(self._title_lbl.text()), fm_msg.horizontalAdvance(clean_msg))
        inner_w = text_w + 48
        w = min(340, max(190, inner_w))

        max_text_w = max(130, w - 48)
        text_rect = fm_msg.boundingRect(QRect(0, 0, max_text_w, 1000), int(Qt.TextFlag.TextWordWrap), clean_msg)
        title_h = fm_title.height()
        msg_h = text_rect.height()
        h = max(26, title_h + msg_h + 10)

        self.resize(w + 10, h + 10)

        center_x = screen.left() + (screen.width() - self.width()) // 2
        start_y = screen.top() - self.height() - 20
        target_y = screen.top() + 16

        self.move(center_x, start_y)
        self.show()
        self.raise_()

        self._pos_anim = QPropertyAnimation(self, b"pos")
        self._pos_anim.setDuration(360)
        self._pos_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._pos_anim.setStartValue(QPoint(center_x, start_y))
        self._pos_anim.setEndValue(QPoint(center_x, target_y))
        self._pos_anim.start()

        self._timer.start(duration_ms)

    def _slide_up(self):
        self._timer.stop()
        screen = QApplication.primaryScreen().availableGeometry()
        center_x = self.x()
        start_y = self.y()
        target_y = screen.top() - self.height() - 20

        self._pos_anim = QPropertyAnimation(self, b"pos")
        self._pos_anim.setDuration(280)
        self._pos_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._pos_anim.setStartValue(QPoint(center_x, start_y))
        self._pos_anim.setEndValue(QPoint(center_x, target_y))
        self._pos_anim.finished.connect(self.hide)
        self._pos_anim.start()
