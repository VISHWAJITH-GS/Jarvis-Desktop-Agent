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

class RemindersDisplayWindow(_FramelessWindowResizeMixin, QWidget):
    """
    Dedicated floating display window for effectively managing and interpreting reminders.
    Features:
      - Interactive cards with due time, relative time badges, and recurrence labels
      - Filter scopes: Upcoming, Today, All, Completed
      - Action buttons on each card to Complete or Delete
      - Quick-add input bar to create new reminders on the fly
      - Collapsible to a sleek header bar with minimize/restore toggle
    """
    def __init__(self, main_win: "MainWindow" = None, parent=None):
        super().__init__(parent)
        self._main_win = main_win
        self.setWindowTitle("JARVIS Reminders Matrix")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(420, 360)
        self._init_frameless_resize()

        self._active_scope = "upcoming"
        self._collapsed = False

        screen = QApplication.primaryScreen().availableGeometry()
        w = min(540, max(440, screen.width() // 3))
        h = min(640, screen.height() - 120)
        self._normal_h = h
        x = max(screen.left() + 20, screen.center().x() - w // 2)
        y = screen.top() + (screen.height() - h) // 2
        self.setGeometry(x, y, w, h)

        self._build_ui()

    def _build_ui(self):
        self._outer = QFrame(self)
        self._outer.setObjectName("RemindersOuter")
        self._outer.setStyleSheet(f"""
            QFrame#RemindersOuter {{
                background: rgba(12, 3, 6, 248);
                border: 1px solid rgba(255, 23, 68, 0.45);
                border-radius: 8px;
            }}
        """)
        self._outer.setMouseTracking(True)
        self._outer.installEventFilter(self)
        v_outer = QVBoxLayout(self)
        v_outer.setContentsMargins(0, 0, 0, 0)
        v_outer.addWidget(self._outer)

        self._main_lay = QVBoxLayout(self._outer)
        self._main_lay.setContentsMargins(12, 10, 12, 10)
        self._main_lay.setSpacing(8)

        # ── Header bar ──────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont("Montserrat", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        title_lbl = QLabel("REMINDER MATRIX - SCHEDULE & TASKS")
        title_lbl.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent; letter-spacing: 1px;")
        hdr.addWidget(title_lbl)

        hdr.addStretch()

        self._count_lbl = QLabel("[ 0 ACTIVE ]")
        self._count_lbl.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        self._count_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._count_lbl)

        self._ref_btn = QPushButton("")
        self._ref_btn.setIcon(get_uicon("refresh", color=C.PRI, size=13))
        self._ref_btn.setFont(QFont("Montserrat", 9, QFont.Weight.Bold))
        self._ref_btn.setFixedSize(22, 18)
        self._ref_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ref_btn.setToolTip("Refresh reminders")
        self._ref_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.12); color: {C.TEXT_DIM};
                border: 1px solid rgba(255, 23, 68, 0.35); border-radius: 2px;
            }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.35); border-color: {C.PRI}; }}
        """)
        self._ref_btn.clicked.connect(self.load_reminders)
        hdr.addWidget(self._ref_btn)

        self._min_btn = QPushButton(" MIN")
        self._min_btn.setIcon(get_uicon("minus", color=C.PRI, size=11))
        self._min_btn.setFont(QFont("Montserrat", 7))
        self._min_btn.setFixedHeight(18)
        self._min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._min_btn.setToolTip("Minimize reminders display")
        self._min_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.12); color: {C.TEXT_DIM};
                border: 1px solid rgba(255, 23, 68, 0.35); border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.35); border-color: {C.PRI}; }}
        """)
        self._min_btn.clicked.connect(self.toggle_collapse)
        hdr.addWidget(self._min_btn)

        close_btn = QPushButton(" DISMISS")
        close_btn.setIcon(get_uicon("cross", color=C.TEXT_DIM, size=11))
        close_btn.setFont(QFont("Montserrat", 7))
        close_btn.setFixedHeight(18)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.12); color: {C.TEXT_DIM};
                border: 1px solid rgba(255, 23, 68, 0.35); border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.35); border-color: {C.PRI}; }}
        """)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)

        self._main_lay.addLayout(hdr)

        # ── Body container (hidden when collapsed) ───────────────────────────
        self._body_widget = QWidget()
        self._body_widget.setStyleSheet("background: transparent;")
        b_lay = QVBoxLayout(self._body_widget)
        b_lay.setContentsMargins(0, 0, 0, 0)
        b_lay.setSpacing(8)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};")
        b_lay.addWidget(sep)

        # ── Filter Tabs ──────────────────────────────────────────────────────
        tab_row = QHBoxLayout()
        tab_row.setSpacing(6)

        self._tab_btns = {}
        for scope_name in ("UPCOMING", "TODAY", "ALL", "COMPLETED"):
            btn = QPushButton(scope_name)
            btn.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
            btn.setFixedHeight(22)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, s=scope_name.lower(): self._set_scope(s))
            tab_row.addWidget(btn)
            self._tab_btns[scope_name.lower()] = btn

        b_lay.addLayout(tab_row)

        # ── Scroll Area for Reminder Cards ───────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(f"""
            QScrollArea {{
                background: transparent;
                border: 1px solid {C.BORDER};
                border-radius: 4px;
            }}
            QScrollBar:vertical, QScrollBar:horizontal {{
                background: transparent; width: 0px; height: 0px; border: none;
            }}
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
                background: transparent; width: 0px; height: 0px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                width: 0; height: 0; border: none;
            }}
        """)

        self._cards_widget = QWidget()
        self._cards_widget.setStyleSheet("background: transparent;")
        self._cards_lay = QVBoxLayout(self._cards_widget)
        self._cards_lay.setContentsMargins(6, 6, 6, 6)
        self._cards_lay.setSpacing(6)
        self._cards_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._cards_widget)

        b_lay.addWidget(self._scroll, stretch=1)

        # ── Quick-Add Bar ────────────────────────────────────────────────────
        add_frame = QFrame()
        add_frame.setStyleSheet(f"""
            QFrame {{
                background: rgba(18, 4, 8, 220);
                border: 1px solid rgba(255, 23, 68, 0.3);
                border-radius: 4px;
                padding: 4px;
            }}
        """)
        add_lay = QHBoxLayout(add_frame)
        add_lay.setContentsMargins(4, 2, 4, 2)
        add_lay.setSpacing(6)

        self._in_msg = QLineEdit()
        self._in_msg.setPlaceholderText("Reminder note (e.g. Call client)...")
        self._in_msg.setFont(QFont("Montserrat", 8))
        self._in_msg.setStyleSheet(f"""
            QLineEdit {{
                background: {C.DARK}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
            }}
        """)
        add_lay.addWidget(self._in_msg, stretch=3)

        self._in_when = QLineEdit()
        self._in_when.setPlaceholderText("When (e.g. 5pm, tomorrow 10am)...")
        self._in_when.setFont(QFont("Montserrat", 8))
        self._in_when.setStyleSheet(f"""
            QLineEdit {{
                background: {C.DARK}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
            }}
        """)
        self._in_when.returnPressed.connect(self._on_quick_add)
        self._in_msg.returnPressed.connect(self._on_quick_add)
        add_lay.addWidget(self._in_when, stretch=2)

        self._add_btn = QPushButton("+ ADD")
        self._add_btn.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        self._add_btn.setFixedHeight(26)
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.25); color: #ffffff;
                border: 1px solid {C.PRI}; border-radius: 3px; padding: 0 10px;
            }}
            QPushButton:hover {{ background: rgba(255, 23, 68, 0.55); }}
        """)
        self._add_btn.clicked.connect(self._on_quick_add)
        add_lay.addWidget(self._add_btn)

        b_lay.addWidget(add_frame)

        self._main_lay.addWidget(self._body_widget, stretch=1)

    def _set_scope(self, scope: str):
        self._active_scope = scope
        self.load_reminders()

    def _update_tab_styles(self):
        for s_name, btn in self._tab_btns.items():
            if s_name == self._active_scope:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba(255, 23, 68, 0.35); color: #ffffff;
                        border: 1px solid {C.PRI}; border-radius: 3px;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba(20, 4, 8, 200); color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{
                        color: #ffffff; border-color: {C.PRI};
                    }}
                """)

    def load_reminders(self):
        self._update_tab_styles()

        # Clear existing cards
        while self._cards_lay.count():
            item = self._cards_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        from datetime import datetime, timedelta
        from actions.reminder import _load_db

        reminders = _load_db()
        now = datetime.now()
        today_date = now.date()
        tomorrow_date = today_date + timedelta(days=1)

        filtered = []
        for r in reminders:
            status = r.get("status", "pending")
            if self._active_scope == "completed":
                if status == "completed":
                    filtered.append(r)
                continue

            if status != "pending" and self._active_scope != "all":
                continue

            try:
                due = datetime.strptime(r.get("due_time", ""), "%Y-%m-%dT%H:%M:%S")
            except Exception:
                continue

            if self._active_scope == "today":
                if due.date() == today_date:
                    filtered.append(r)
            elif self._active_scope == "tomorrow":
                if due.date() == tomorrow_date:
                    filtered.append(r)
            elif self._active_scope == "upcoming":
                if due >= now or r.get("recurrence", "none") != "none":
                    filtered.append(r)
            else:  # all
                filtered.append(r)

        filtered.sort(key=lambda x: x.get("due_time", ""))

        active_count = sum(1 for r in reminders if r.get("status") == "pending")
        self._count_lbl.setText(f"[ {active_count} ACTIVE ]")

        if not filtered:
            empty_lbl = QLabel(f"◈ NO REMINDERS IN '{self._active_scope.upper()}' VIEW ◈\nAdd one below or ask JARVIS to schedule.")
            empty_lbl.setFont(QFont("Montserrat", 8))
            empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; padding: 30px 10px;")
            self._cards_lay.addWidget(empty_lbl)
            return

        for r in filtered:
            card = self._create_reminder_card(r)
            self._cards_lay.addWidget(card)

    def _create_reminder_card(self, r: dict) -> QFrame:
        from datetime import datetime, timedelta
        card = QFrame()
        status = r.get("status", "pending")
        recurrence = r.get("recurrence", "none")
        due_iso = r.get("due_time", "")
        msg = r.get("message", "Reminder")
        rem_id = r.get("id", "")

        is_completed = (status == "completed")
        border_accent = "#00e676" if is_completed else ("#ff9100" if recurrence != "none" else "#ff1744")

        card.setStyleSheet(f"""
            QFrame {{
                background: rgba(22, 5, 10, 230);
                border: 1px solid rgba(255, 23, 68, 0.25);
                border-left: 4px solid {border_accent};
                border-radius: 5px;
            }}
        """)

        c_lay = QVBoxLayout(card)
        c_lay.setContentsMargins(10, 8, 10, 8)
        c_lay.setSpacing(4)

        # Top row: Time + badge + countdown
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        try:
            dt = datetime.strptime(due_iso, "%Y-%m-%dT%H:%M:%S")
            now = datetime.now()
            today = now.date()

            if dt.date() == today:
                time_label = f"Today at {dt.strftime('%I:%M %p')}"
            elif dt.date() == today + timedelta(days=1):
                time_label = f"Tomorrow at {dt.strftime('%I:%M %p')}"
            else:
                time_label = dt.strftime("%b %d, %Y at %I:%M %p")

            if is_completed:
                countdown = "(Completed)"
                c_color = "#00e676"
            elif dt < now:
                countdown = "(Overdue)"
                c_color = "#ff1744"
            else:
                diff = dt - now
                if diff.days > 0:
                    countdown = f"(in {diff.days}d {diff.seconds // 3600}h)"
                elif diff.seconds >= 3600:
                    countdown = f"(in {diff.seconds // 3600}h {(diff.seconds % 3600) // 60}m)"
                else:
                    countdown = f"(in {max(1, diff.seconds // 60)}m)"
                c_color = "#ff9100"
        except Exception:
            time_label = due_iso
            countdown = ""
            c_color = C.TEXT_DIM

        t_lbl = QLabel(time_label)
        t_lbl.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        t_lbl.setStyleSheet("color: #ffffff; background: transparent; border: none;")
        top_row.addWidget(t_lbl)

        if countdown:
            cd_lbl = QLabel(countdown)
            cd_lbl.setFont(QFont("Montserrat", 7))
            cd_lbl.setStyleSheet(f"color: {c_color}; background: transparent; border: none;")
            top_row.addWidget(cd_lbl)

        if recurrence != "none":
            rec_lbl = QLabel(f"[{recurrence.upper()}]")
            rec_lbl.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
            rec_lbl.setStyleSheet("color: #ff9100; background: transparent; border: none;")
            top_row.addWidget(rec_lbl)

        top_row.addStretch()

        if not is_completed:
            done_btn = QPushButton(" DONE")
            done_btn.setIcon(get_uicon("check-circle", color="#00e676", size=12))
            done_btn.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
            done_btn.setFixedHeight(18)
            done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            done_btn.setStyleSheet("""
                QPushButton {
                    background: rgba(0, 230, 118, 0.15); color: #00e676;
                    border: 1px solid rgba(0, 230, 118, 0.4); border-radius: 2px; padding: 0 5px;
                }
                QPushButton:hover { background: rgba(0, 230, 118, 0.45); color: #ffffff; }
            """)
            done_btn.clicked.connect(lambda _, rid=rem_id: self._on_complete(rid))
            top_row.addWidget(done_btn)

        del_btn = QPushButton("")
        del_btn.setIcon(get_uicon("cross", color=C.TEXT_DIM, size=11))
        del_btn.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        del_btn.setFixedSize(18, 18)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.12); color: {C.TEXT_DIM};
                border: 1px solid rgba(255, 23, 68, 0.35); border-radius: 2px;
            }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.45); border-color: {C.PRI}; }}
        """)
        del_btn.clicked.connect(lambda _, rid=rem_id: self._on_delete(rid))
        top_row.addWidget(del_btn)

        c_lay.addLayout(top_row)

        m_lbl = QLabel(msg)
        m_lbl.setFont(QFont("Montserrat", 9))
        m_lbl.setStyleSheet(f"color: {'#aaaaaa' if is_completed else '#ffffff'}; background: transparent; border: none;")
        m_lbl.setWordWrap(True)
        c_lay.addWidget(m_lbl)

        return card

    def _on_complete(self, rem_id: str):
        from actions.reminder import complete_reminder
        res = complete_reminder(rem_id)
        if self._main_win and hasattr(self._main_win, "write_log"):
            self._main_win.write_log(f"SYS: Reminder marked completed.")
        self.load_reminders()

    def _on_delete(self, rem_id: str):
        from actions.reminder import delete_reminder
        res = delete_reminder(rem_id)
        if self._main_win and hasattr(self._main_win, "write_log"):
            self._main_win.write_log(f"SYS: Reminder deleted.")
        self.load_reminders()

    def _on_quick_add(self):
        msg = self._in_msg.text().strip()
        when = self._in_when.text().strip()
        if not msg:
            self._in_msg.setPlaceholderText("Please specify what to remember!")
            return
        if not when:
            self._in_when.setPlaceholderText("Please specify when (e.g. 5pm)!")
            return

        from actions.reminder import parse_datetime, create_reminder
        target_dt, _ = parse_datetime(date_str="", time_str=when, natural_text=when)
        if not target_dt:
            self._in_when.clear()
            self._in_when.setPlaceholderText("Could not parse time! Try '5pm' or 'tomorrow 10am'")
            return

        res = create_reminder(message=msg, target_dt=target_dt)
        self._in_msg.clear()
        self._in_when.clear()
        self._in_msg.setPlaceholderText("Reminder note (e.g. Call client)...")
        self._in_when.setPlaceholderText("When (e.g. 5pm, tomorrow 10am)...")

        if self._main_win and hasattr(self._main_win, "write_log"):
            self._main_win.write_log(f"SYS: Reminder added for {target_dt.strftime('%I:%M %p')}.")
        self.load_reminders()

    def popup(self, scope: str = "upcoming"):
        if self._main_win and hasattr(self._main_win, "_reminders_dock_minimized_to_bar"):
            self._main_win._reminders_dock_minimized_to_bar = False
            self._main_win._update_dots_overlay()
        self._active_scope = (scope or "upcoming").lower()
        self._collapsed = False
        self._body_widget.show()
        screen = QApplication.primaryScreen().availableGeometry()
        h = getattr(self, "_normal_h", min(640, screen.height() - 120))
        self.setFixedHeight(h)
        self._min_btn.setText("MIN  —")
        self.load_reminders()
        self.show()
        self.raise_()

    def minimize_to_dock(self):
        self._collapsed = True
        self._body_widget.hide()
        self.setFixedHeight(46)
        self._min_btn.setText(" RESTORE")
        self._min_btn.setIcon(get_uicon("restore", color=C.PRI, size=11))
        self._min_btn.setToolTip("Restore reminders display")
        self.hide()
        if self._main_win:
            self._main_win._reminders_dock_minimized_to_bar = True
            self._main_win._update_dots_overlay()
            if hasattr(self._main_win, "write_log"):
                self._main_win.write_log("SYS: Reminders display minimized to left dock.")

    def restore_from_dock(self):
        self._collapsed = False
        self._body_widget.show()
        screen = QApplication.primaryScreen().availableGeometry()
        h = getattr(self, "_normal_h", min(640, screen.height() - 120))
        self.setFixedHeight(h)
        self._min_btn.setText(" MIN")
        self._min_btn.setIcon(get_uicon("minus", color=C.PRI, size=11))
        self._min_btn.setToolTip("Minimize reminders display")
        if self._main_win:
            self._main_win._reminders_dock_minimized_to_bar = False
            self._main_win._update_dots_overlay()
        self.show()
        self.raise_()
        self.activateWindow()
        if self._main_win and hasattr(self._main_win, "write_log"):
            self._main_win.write_log("SYS: Reminders display restored.")

    def toggle_collapse(self):
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


class MemoryActionsDisplayWindow(_FramelessWindowResizeMixin, QWidget):
    """
    Dedicated interactive management platform for inspecting, managing, editing,
    and deleting JARVIS's long-term memory and viewing/controlling registered actions and permissions.
    """
    def __init__(self, main_win: "MainWindow" = None, parent=None):
        super().__init__(parent)
        self._main_win = main_win
        self.setWindowTitle("JARVIS Memory & Actions Platform")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(560, 500)
        self._init_frameless_resize()

        self._active_tab = "memory"          # "memory" or "actions"
        self._active_category = "all"        # "all" or specific category
        self._active_risk = "all"            # "all" or specific risk level
        self._collapsed = False

        screen = QApplication.primaryScreen().availableGeometry()
        w = min(700, max(560, screen.width() // 2))
        h = min(740, screen.height() - 100)
        self._normal_h = h
        x = max(screen.left() + 20, screen.center().x() - w // 2)
        y = screen.top() + (screen.height() - h) // 2
        self.setGeometry(x, y, w, h)

        self._build_ui()

    def _build_ui(self):
        self._outer = QFrame(self)
        self._outer.setObjectName("MemActOuter")
        self._outer.setStyleSheet(f"""
            QFrame#MemActOuter {{
                background: rgba(12, 3, 6, 250);
                border: 1px solid rgba(255, 23, 68, 0.45);
                border-radius: 8px;
            }}
        """)
        self._outer.setMouseTracking(True)
        self._outer.installEventFilter(self)
        v_outer = QVBoxLayout(self)
        v_outer.setContentsMargins(0, 0, 0, 0)
        v_outer.addWidget(self._outer)

        self._main_lay = QVBoxLayout(self._outer)
        self._main_lay.setContentsMargins(14, 10, 14, 10)
        self._main_lay.setSpacing(8)

        # ── Header bar ──────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont("Montserrat", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        title_lbl = QLabel("CORE MATRIX - MEMORY & ACTIONS PLATFORM")
        title_lbl.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent; letter-spacing: 1px;")
        hdr.addWidget(title_lbl)

        hdr.addStretch()

        self._count_lbl = QLabel("[ READY ]")
        self._count_lbl.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        self._count_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._count_lbl)

        self._ref_btn = QPushButton("")
        self._ref_btn.setIcon(get_uicon("refresh", color=C.PRI, size=13))
        self._ref_btn.setFont(QFont("Montserrat", 9, QFont.Weight.Bold))
        self._ref_btn.setFixedSize(22, 18)
        self._ref_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ref_btn.setToolTip("Refresh data")
        self._ref_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.12); color: {C.TEXT_DIM};
                border: 1px solid rgba(255, 23, 68, 0.35); border-radius: 2px;
            }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.35); border-color: {C.PRI}; }}
        """)
        self._ref_btn.clicked.connect(self.load_data)
        hdr.addWidget(self._ref_btn)

        self._min_btn = QPushButton(" MIN")
        self._min_btn.setIcon(get_uicon("minus", color=C.PRI, size=11))
        self._min_btn.setFont(QFont("Montserrat", 7))
        self._min_btn.setFixedHeight(18)
        self._min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._min_btn.setToolTip("Minimize platform display")
        self._min_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.12); color: {C.TEXT_DIM};
                border: 1px solid rgba(255, 23, 68, 0.35); border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.35); border-color: {C.PRI}; }}
        """)
        self._min_btn.clicked.connect(self.toggle_collapse)
        hdr.addWidget(self._min_btn)

        close_btn = QPushButton(" DISMISS")
        close_btn.setIcon(get_uicon("cross", color=C.TEXT_DIM, size=11))
        close_btn.setFont(QFont("Montserrat", 7))
        close_btn.setFixedHeight(18)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.12); color: {C.TEXT_DIM};
                border: 1px solid rgba(255, 23, 68, 0.35); border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.35); border-color: {C.PRI}; }}
        """)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)

        self._main_lay.addLayout(hdr)

        # ── Body container (hidden when collapsed) ───────────────────────────
        self._body_widget = QWidget()
        self._body_widget.setStyleSheet("background: transparent;")
        b_lay = QVBoxLayout(self._body_widget)
        b_lay.setContentsMargins(0, 0, 0, 0)
        b_lay.setSpacing(8)

        # ── Top Tab Switcher ────────────────────────────────────────────────
        top_tabs = QHBoxLayout()
        top_tabs.setSpacing(8)

        self._btn_tab_mem = QPushButton(" MEMORY MATRIX")
        self._btn_tab_mem.setIcon(get_uicon("database", color=C.PRI, size=14))
        self._btn_tab_mem.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        self._btn_tab_mem.setFixedHeight(26)
        self._btn_tab_mem.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_tab_mem.clicked.connect(lambda: self._switch_tab("memory"))
        top_tabs.addWidget(self._btn_tab_mem, stretch=1)

        self._btn_tab_act = QPushButton(" ACTION REGISTRY & PERMISSIONS")
        self._btn_tab_act.setIcon(get_uicon("shield", color=C.PRI, size=14))
        self._btn_tab_act.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        self._btn_tab_act.setFixedHeight(26)
        self._btn_tab_act.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_tab_act.clicked.connect(lambda: self._switch_tab("actions"))
        top_tabs.addWidget(self._btn_tab_act, stretch=1)

        b_lay.addLayout(top_tabs)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};")
        b_lay.addWidget(sep)

        # ── Stacked Pages ───────────────────────────────────────────────────
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent;")

        # Page 0: Memory Platform
        self._mem_page = QWidget()
        self._mem_page.setStyleSheet("background: transparent;")
        m_page_lay = QVBoxLayout(self._mem_page)
        m_page_lay.setContentsMargins(0, 0, 0, 0)
        m_page_lay.setSpacing(8)

        # Memory Search and Category Filters
        m_filter_lay = QVBoxLayout()
        m_filter_lay.setSpacing(6)

        self._mem_search = QLineEdit()
        self._mem_search.setPlaceholderText("Search memory (e.g. name, preferences, project, exam)...")
        self._mem_search.setFont(QFont("Montserrat", 8))
        self._mem_search.setStyleSheet(f"""
            QLineEdit {{
                background: {C.DARK}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border-color: {C.PRI}; }}
        """)
        self._mem_search.textChanged.connect(self.load_memory)
        m_filter_lay.addWidget(self._mem_search)

        m_cats_lay = QHBoxLayout()
        m_cats_lay.setSpacing(4)
        self._cat_btns = {}
        for c in ("ALL", "IDENTITY", "PREFERENCES", "PROJECTS", "RELATIONSHIPS", "WISHES", "NOTES"):
            btn = QPushButton(c)
            btn.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
            btn.setFixedHeight(20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, cat=c.lower(): self._set_category(cat))
            m_cats_lay.addWidget(btn)
            self._cat_btns[c.lower()] = btn
        m_filter_lay.addLayout(m_cats_lay)
        m_page_lay.addLayout(m_filter_lay)

        # Memory Cards Scroll Area
        self._mem_scroll = QScrollArea()
        self._mem_scroll.setWidgetResizable(True)
        self._mem_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._mem_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._mem_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._mem_scroll.setStyleSheet(f"""
            QScrollArea {{
                background: transparent;
                border: 1px solid {C.BORDER};
                border-radius: 4px;
            }}
            QScrollBar:vertical, QScrollBar:horizontal {{
                background: transparent; width: 0px; height: 0px; border: none;
            }}
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
                background: transparent; width: 0px; height: 0px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                width: 0; height: 0; border: none;
            }}
        """)
        self._mem_cards_widget = QWidget()
        self._mem_cards_widget.setStyleSheet("background: transparent;")
        self._mem_cards_lay = QVBoxLayout(self._mem_cards_widget)
        self._mem_cards_lay.setContentsMargins(6, 6, 6, 6)
        self._mem_cards_lay.setSpacing(6)
        self._mem_cards_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._mem_scroll.setWidget(self._mem_cards_widget)
        m_page_lay.addWidget(self._mem_scroll, stretch=1)

        # Memory Quick Add Bar
        add_frame = QFrame()
        add_frame.setStyleSheet(f"""
            QFrame {{
                background: rgba(18, 4, 8, 220);
                border: 1px solid rgba(255, 23, 68, 0.3);
                border-radius: 4px;
                padding: 4px;
            }}
        """)
        add_lay = QHBoxLayout(add_frame)
        add_lay.setContentsMargins(4, 2, 4, 2)
        add_lay.setSpacing(6)

        self._add_cat_combo = QComboBox()
        self._add_cat_combo.addItems(["notes", "preferences", "projects", "relationships", "wishes", "identity"])
        self._add_cat_combo.setFont(QFont("Montserrat", 8))
        self._add_cat_combo.setFixedWidth(115)
        self._add_cat_combo.setStyleSheet(f"""
            QComboBox {{
                background: {C.DARK}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 4px;
            }}
            QComboBox QAbstractItemView {{
                background: #0d0205; color: #ffffff; selection-background-color: {C.PRI};
            }}
        """)
        add_lay.addWidget(self._add_cat_combo)

        self._add_key_in = QLineEdit()
        self._add_key_in.setPlaceholderText("key (e.g. favorite_ide)...")
        self._add_key_in.setFont(QFont("Montserrat", 8))
        self._add_key_in.setStyleSheet(f"""
            QLineEdit {{
                background: {C.DARK}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
            }}
        """)
        add_lay.addWidget(self._add_key_in, stretch=2)

        self._add_val_in = QLineEdit()
        self._add_val_in.setPlaceholderText("value (e.g. Antigravity IDE)...")
        self._add_val_in.setFont(QFont("Montserrat", 8))
        self._add_val_in.setStyleSheet(f"""
            QLineEdit {{
                background: {C.DARK}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
            }}
        """)
        self._add_key_in.returnPressed.connect(self._on_add_memory)
        self._add_val_in.returnPressed.connect(self._on_add_memory)
        add_lay.addWidget(self._add_val_in, stretch=3)

        self._add_mem_btn = QPushButton("+ REMEMBER")
        self._add_mem_btn.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        self._add_mem_btn.setFixedHeight(26)
        self._add_mem_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_mem_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.25); color: #ffffff;
                border: 1px solid {C.PRI}; border-radius: 3px; padding: 0 8px;
            }}
            QPushButton:hover {{ background: rgba(255, 23, 68, 0.55); }}
        """)
        self._add_mem_btn.clicked.connect(self._on_add_memory)
        add_lay.addWidget(self._add_mem_btn)

        m_page_lay.addWidget(add_frame)
        self._stack.addWidget(self._mem_page)

        # Page 1: Action Registry Platform
        self._act_page = QWidget()
        self._act_page.setStyleSheet("background: transparent;")
        a_page_lay = QVBoxLayout(self._act_page)
        a_page_lay.setContentsMargins(0, 0, 0, 0)
        a_page_lay.setSpacing(8)

        # Action Filters
        a_filter_lay = QVBoxLayout()
        a_filter_lay.setSpacing(6)

        self._act_search = QLineEdit()
        self._act_search.setPlaceholderText("Filter actions by name, keyword, or tool...")
        self._act_search.setFont(QFont("Montserrat", 8))
        self._act_search.setStyleSheet(f"""
            QLineEdit {{
                background: {C.DARK}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border-color: {C.PRI}; }}
        """)
        self._act_search.textChanged.connect(self.load_actions)
        a_filter_lay.addWidget(self._act_search)

        a_risk_lay = QHBoxLayout()
        a_risk_lay.setSpacing(4)
        self._risk_btns = {}
        for r in ("ALL", "IMMEDIATE", "CONFIRMATION", "HIGH_IMPACT", "FORBIDDEN"):
            btn = QPushButton(r.replace("_", " "))
            btn.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
            btn.setFixedHeight(20)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, risk=r.lower(): self._set_risk(risk))
            a_risk_lay.addWidget(btn)
            self._risk_btns[r.lower()] = btn
        a_filter_lay.addLayout(a_risk_lay)
        a_page_lay.addLayout(a_filter_lay)

        # Action Cards Scroll Area
        self._act_scroll = QScrollArea()
        self._act_scroll.setWidgetResizable(True)
        self._act_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._act_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._act_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._act_scroll.setStyleSheet(f"""
            QScrollArea {{
                background: transparent;
                border: 1px solid {C.BORDER};
                border-radius: 4px;
            }}
            QScrollBar:vertical, QScrollBar:horizontal {{
                background: transparent; width: 0px; height: 0px; border: none;
            }}
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
                background: transparent; width: 0px; height: 0px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                width: 0; height: 0; border: none;
            }}
        """)
        self._act_cards_widget = QWidget()
        self._act_cards_widget.setStyleSheet("background: transparent;")
        self._act_cards_lay = QVBoxLayout(self._act_cards_widget)
        self._act_cards_lay.setContentsMargins(6, 6, 6, 6)
        self._act_cards_lay.setSpacing(6)
        self._act_cards_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._act_scroll.setWidget(self._act_cards_widget)
        a_page_lay.addWidget(self._act_scroll, stretch=1)

        self._stack.addWidget(self._act_page)

        b_lay.addWidget(self._stack, stretch=1)
        self._main_lay.addWidget(self._body_widget, stretch=1)

        self._switch_tab("memory")

    def _switch_tab(self, tab: str):
        self._active_tab = tab.lower()
        if self._active_tab == "actions":
            self._stack.setCurrentIndex(1)
            self._btn_tab_act.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(255, 23, 68, 0.4); color: #ffffff;
                    border: 1px solid {C.PRI}; border-radius: 3px;
                }}
            """)
            self._btn_tab_mem.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(255, 23, 68, 0.1); color: {C.TEXT_DIM};
                    border: 1px solid rgba(255, 23, 68, 0.25); border-radius: 3px;
                }}
                QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.25); }}
            """)
            self.load_actions()
        else:
            self._stack.setCurrentIndex(0)
            self._btn_tab_mem.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(255, 23, 68, 0.4); color: #ffffff;
                    border: 1px solid {C.PRI}; border-radius: 3px;
                }}
            """)
            self._btn_tab_act.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(255, 23, 68, 0.1); color: {C.TEXT_DIM};
                    border: 1px solid rgba(255, 23, 68, 0.25); border-radius: 3px;
                }}
                QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.25); }}
            """)
            self.load_memory()

    def _set_category(self, cat: str):
        self._active_category = cat.lower()
        self._update_category_buttons()
        self.load_memory()

    def _update_category_buttons(self):
        for c_name, btn in self._cat_btns.items():
            if c_name == self._active_category:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba(255, 23, 68, 0.35); color: #ffffff;
                        border: 1px solid {C.PRI}; border-radius: 2px; padding: 0 4px;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba(255, 23, 68, 0.08); color: {C.TEXT_DIM};
                        border: 1px solid rgba(255, 23, 68, 0.25); border-radius: 2px; padding: 0 4px;
                    }}
                    QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.25); }}
                """)

    def _set_risk(self, risk: str):
        self._active_risk = risk.lower()
        self._update_risk_buttons()
        self.load_actions()

    def _update_risk_buttons(self):
        for r_name, btn in self._risk_btns.items():
            if r_name == self._active_risk:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba(255, 23, 68, 0.35); color: #ffffff;
                        border: 1px solid {C.PRI}; border-radius: 2px; padding: 0 4px;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba(255, 23, 68, 0.08); color: {C.TEXT_DIM};
                        border: 1px solid rgba(255, 23, 68, 0.25); border-radius: 2px; padding: 0 4px;
                    }}
                    QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.25); }}
                """)

    def load_data(self):
        if self._active_tab == "actions":
            self.load_actions()
        else:
            self.load_memory()

    def load_memory(self):
        from memory.memory_manager import all_entries_for_ui

        # Clear existing cards
        while self._mem_cards_lay.count():
            item = self._mem_cards_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        self._update_category_buttons()
        entries = all_entries_for_ui()
        search_q = self._mem_search.text().strip().lower()
        cat_filter = self._active_category.lower()

        displayed = 0
        for e in entries:
            cat = str(e.get("category", "notes")).lower()
            key = str(e.get("key", ""))
            val = str(e.get("value", ""))
            upd = str(e.get("updated", ""))

            if cat_filter != "all" and cat != cat_filter:
                continue

            if search_q:
                hay = f"{cat} {key} {val}".lower()
                if search_q not in hay:
                    continue

            card = self._create_memory_card(cat, key, val, upd)
            self._mem_cards_lay.addWidget(card)
            displayed += 1

        self._count_lbl.setText(f"[ {displayed} MEMORY ENTRIES ]")
        if displayed == 0:
            empty = QLabel("No memory items found matching the filter.")
            empty.setFont(QFont("Montserrat", 9))
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; padding: 16px;")
            self._mem_cards_lay.addWidget(empty)

    def _create_memory_card(self, cat: str, key: str, val: str, updated: str) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: rgba(20, 5, 10, 210);
                border: 1px solid rgba(255, 23, 68, 0.25);
                border-radius: 4px;
                padding: 6px;
            }}
            QFrame:hover {{
                border-color: rgba(255, 23, 68, 0.6);
            }}
        """)
        c_lay = QVBoxLayout(card)
        c_lay.setContentsMargins(8, 6, 8, 6)
        c_lay.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        cat_badge = QLabel(f"[{cat.upper()}]")
        cat_badge.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        cat_badge.setStyleSheet("color: #ff5252; background: transparent; border: none;")
        top_row.addWidget(cat_badge)

        k_lbl = QLabel(key)
        k_lbl.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        k_lbl.setStyleSheet("color: #ffffff; background: transparent; border: none;")
        top_row.addWidget(k_lbl)

        if updated:
            u_lbl = QLabel(f"({updated})")
            u_lbl.setFont(QFont("Montserrat", 7))
            u_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
            top_row.addWidget(u_lbl)

        top_row.addStretch()

        edit_btn = QPushButton("EDIT")
        edit_btn.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        edit_btn.setFixedHeight(18)
        edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_btn.setStyleSheet("""
            QPushButton {
                background: rgba(0, 230, 118, 0.15); color: #00e676;
                border: 1px solid rgba(0, 230, 118, 0.4); border-radius: 2px; padding: 0 5px;
            }
            QPushButton:hover { background: rgba(0, 230, 118, 0.45); color: #ffffff; }
        """)
        edit_btn.clicked.connect(lambda _, c=cat, k=key, v=val: self._on_edit_memory(c, k, v))
        top_row.addWidget(edit_btn)

        del_btn = QPushButton("")
        del_btn.setIcon(get_uicon("cross", color=C.TEXT_DIM, size=11))
        del_btn.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        del_btn.setFixedSize(18, 18)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.12); color: {C.TEXT_DIM};
                border: 1px solid rgba(255, 23, 68, 0.35); border-radius: 2px;
            }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.45); border-color: {C.PRI}; }}
        """)
        del_btn.clicked.connect(lambda _, c=cat, k=key: self._on_forget_memory(c, k))
        top_row.addWidget(del_btn)

        c_lay.addLayout(top_row)

        v_lbl = QLabel(val)
        v_lbl.setFont(QFont("Montserrat", 8))
        v_lbl.setStyleSheet("color: #e0e0e0; background: transparent; border: none;")
        v_lbl.setWordWrap(True)
        c_lay.addWidget(v_lbl)

        return card

    def _on_edit_memory(self, cat: str, key: str, cur_val: str):
        from memory.memory_manager import remember
        new_val, ok = QInputDialog.getText(
            self,
            "Edit Memory Entry",
            f"Update memory for [{cat.upper()}] {key}:",
            text=cur_val
        )
        if ok and new_val.strip():
            remember(key, new_val.strip(), cat)
            if self._main_win and hasattr(self._main_win, "write_log"):
                self._main_win.write_log(f"SYS: Updated memory '{key}'.")
            self.load_memory()

    def _on_forget_memory(self, cat: str, key: str):
        from memory.memory_manager import forget
        forget(key, cat)
        if self._main_win and hasattr(self._main_win, "write_log"):
            self._main_win.write_log(f"SYS: Forgotten memory '{key}'.")
        self.load_memory()

    def _on_add_memory(self):
        cat = self._add_cat_combo.currentText().strip().lower()
        key = self._add_key_in.text().strip().replace(" ", "_").lower()
        val = self._add_val_in.text().strip()

        if not key:
            self._add_key_in.setPlaceholderText("Specify key name!")
            return
        if not val:
            self._add_val_in.setPlaceholderText("Specify value!")
            return

        from memory.memory_manager import remember
        remember(key, val, cat)
        self._add_key_in.clear()
        self._add_val_in.clear()
        if self._main_win and hasattr(self._main_win, "write_log"):
            self._main_win.write_log(f"SYS: Remembered {cat}/{key}.")
        self.load_memory()

    def load_actions(self):
        from pathlib import Path
        from core.action_loader import discover_actions
        from core.permissions import ActionPermissionManager, ActionRiskLevel

        while self._act_cards_lay.count():
            item = self._act_cards_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        self._update_risk_buttons()

        try:
            registry = discover_actions(Path("actions"))
            actions_dict = registry._actions if hasattr(registry, "_actions") else {}
        except Exception:
            actions_dict = {}

        try:
            from core.plugin_loader import discover_plugins
            p_reg = discover_plugins()
            plugins_dict = p_reg._plugins if hasattr(p_reg, "_plugins") else {}
        except Exception:
            plugins_dict = {}

        search_q = self._act_search.text().strip().lower()
        risk_filter = self._active_risk.lower()

        all_actions = []
        for name, rec in actions_dict.items():
            all_actions.append((name, rec.description, rec.parameters, rec.file, "Builtin Action"))
        for name, prec in plugins_dict.items():
            all_actions.append((name, prec.description, prec.parameters, prec.file, "Plugin"))

        all_actions.sort(key=lambda x: x[0])

        displayed = 0
        for name, desc, params, src_file, kind in all_actions:
            risk = ActionPermissionManager.get_risk_level(name)
            risk_val = risk.value.lower()

            if risk_filter != "all" and risk_filter not in risk_val:
                continue

            if search_q:
                hay = f"{name} {desc} {src_file} {kind} {risk_val}".lower()
                if search_q not in hay:
                    continue

            card = self._create_action_card(name, desc, params, src_file, kind, risk)
            self._act_cards_lay.addWidget(card)
            displayed += 1

        self._count_lbl.setText(f"[ {displayed} ACTIONS & PLUGINS ]")
        if displayed == 0:
            empty = QLabel("No actions matching the active filter.")
            empty.setFont(QFont("Montserrat", 9))
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; padding: 16px;")
            self._act_cards_lay.addWidget(empty)

    def _create_action_card(self, name: str, desc: str, params: dict, src_file: str, kind: str, risk) -> QFrame:
        from core.permissions import ActionRiskLevel

        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: rgba(20, 5, 10, 210);
                border: 1px solid rgba(255, 23, 68, 0.25);
                border-radius: 4px;
                padding: 6px;
            }}
            QFrame:hover {{
                border-color: rgba(255, 23, 68, 0.6);
            }}
        """)
        c_lay = QVBoxLayout(card)
        c_lay.setContentsMargins(8, 6, 8, 6)
        c_lay.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        name_lbl = QLabel(name)
        name_lbl.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        name_lbl.setStyleSheet("color: #ff5252; background: transparent; border: none;")
        top_row.addWidget(name_lbl)

        # Risk badge
        if risk == ActionRiskLevel.IMMEDIATE:
            r_text, r_color, r_bg = "⚡ LEVEL 1: IMMEDIATE", "#00e676", "rgba(0, 230, 118, 0.15)"
        elif risk == ActionRiskLevel.CONFIRMATION_REQUIRED:
            r_text, r_color, r_bg = "🛡️ LEVEL 2: CONFIRMATION", "#ffd600", "rgba(255, 214, 0, 0.15)"
        elif risk == ActionRiskLevel.HIGH_IMPACT:
            r_text, r_color, r_bg = "⚠️ LEVEL 3: HIGH IMPACT", "#ff6d00", "rgba(255, 109, 0, 0.18)"
        else:
            r_text, r_color, r_bg = "⛔ FORBIDDEN", "#d50000", "rgba(213, 0, 0, 0.22)"

        risk_badge = QLabel(r_text)
        risk_badge.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        risk_badge.setStyleSheet(f"color: {r_color}; background: {r_bg}; border-radius: 2px; padding: 2px 5px;")
        top_row.addWidget(risk_badge)

        top_row.addStretch()

        src_lbl = QLabel(f"{kind} ({src_file or 'core'})")
        src_lbl.setFont(QFont("Montserrat", 7))
        src_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        top_row.addWidget(src_lbl)

        c_lay.addLayout(top_row)

        d_lbl = QLabel(desc or "No description provided.")
        d_lbl.setFont(QFont("Montserrat", 8))
        d_lbl.setStyleSheet("color: #d0d0d0; background: transparent; border: none;")
        d_lbl.setWordWrap(True)
        c_lay.addWidget(d_lbl)

        # Parameters summary
        props = (params or {}).get("properties", {})
        req = (params or {}).get("required", [])
        if props:
            param_list = [f"{p}{'*' if p in req else ''}" for p in props.keys()]
            p_summary = f"params: {', '.join(param_list)}"
        else:
            p_summary = "params: none"

        p_lbl = QLabel(p_summary)
        p_lbl.setFont(QFont("Montserrat", 7))
        p_lbl.setStyleSheet("color: #80cbc4; background: transparent; border: none;")
        c_lay.addWidget(p_lbl)

        return card

    def popup(self, tab: str = "memory"):
        self._collapsed = False
        self._body_widget.show()
        screen = QApplication.primaryScreen().availableGeometry()
        h = getattr(self, "_normal_h", min(740, screen.height() - 100))
        self.setFixedHeight(h)
        self._min_btn.setText("MIN")
        self._min_btn.setIcon(get_uicon("minus", color=C.PRI, size=11))
        self._switch_tab(tab or "memory")
        if self._main_win and hasattr(self._main_win, "_memory_actions_dock_minimized_to_bar"):
            self._main_win._memory_actions_dock_minimized_to_bar = False
            self._main_win._update_dots_overlay()
        self.show()
        self.raise_()

    def minimize_to_dock(self):
        self._collapsed = True
        self._body_widget.hide()
        self.setFixedHeight(46)
        self._min_btn.setText(" RESTORE")
        self._min_btn.setIcon(get_uicon("restore", color=C.PRI, size=11))
        self._min_btn.setToolTip("Restore platform display")
        self.hide()
        if self._main_win:
            self._main_win._memory_actions_dock_minimized_to_bar = True
            self._main_win._update_dots_overlay()
            if hasattr(self._main_win, "write_log"):
                self._main_win.write_log("SYS: Memory & Actions platform minimized to left dock.")

    def restore_from_dock(self):
        self._collapsed = False
        self._body_widget.show()
        screen = QApplication.primaryScreen().availableGeometry()
        h = getattr(self, "_normal_h", min(740, screen.height() - 100))
        self.setFixedHeight(h)
        self._min_btn.setText(" MIN")
        self._min_btn.setIcon(get_uicon("minus", color=C.PRI, size=11))
        self._min_btn.setToolTip("Minimize platform display")
        if self._main_win:
            self._main_win._memory_actions_dock_minimized_to_bar = False
            self._main_win._update_dots_overlay()
        self.show()
        self.raise_()
        self.activateWindow()
        if self._main_win and hasattr(self._main_win, "write_log"):
            self._main_win.write_log("SYS: Memory & Actions platform restored.")

    def toggle_collapse(self):
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


