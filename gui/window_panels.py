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


class WindowPanelsMixin:
    """Interactive content, quiz, code job, and review panels for MainWindow."""

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(5)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(QFont("Montserrat", 9))
        self._input.setFixedHeight(30)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d14; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 3px 7px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("▸")
        send.setFixedSize(30, 30)
        send.setFont(QFont("Montserrat", 11, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_content_panel(self) -> QWidget:
        """
        Collapsible panel below the HUD — shows search results, news, briefings.
        Hidden by default; appears when show_content() is called.
        """
        w = QWidget()
        w.setObjectName("ContentPanel")
        w.setStyleSheet("""
            QWidget#ContentPanel {
                background: transparent;
                border: none;
            }
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(5)

        # ── header row ───────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont("Montserrat", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        self._content_title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent; letter-spacing: 1px;")
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(QFont("Montserrat", 7))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._content_ts_lbl)

        self._content_min_btn = QPushButton(" MIN")
        self._content_min_btn.setIcon(get_uicon("minus", color=C.PRI, size=11))
        self._content_min_btn.setFont(QFont("Montserrat", 7))
        self._content_min_btn.setFixedHeight(18)
        self._content_min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._content_min_btn.setToolTip("Minimize search display")
        self._content_min_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.12); color: {C.TEXT_DIM};
                border: 1px solid rgba(255, 23, 68, 0.35); border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.35); border-color: {C.PRI}; }}
        """)
        self._content_min_btn.clicked.connect(self._toggle_search_dock_minimize)
        hdr.addWidget(self._content_min_btn)

        dismiss = QPushButton(" DISMISS")
        dismiss.setIcon(get_uicon("cross", color=C.TEXT_DIM, size=11))
        dismiss.setFont(QFont("Montserrat", 7))
        dismiss.setFixedHeight(18)
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.12); color: {C.TEXT_DIM};
                border: 1px solid rgba(255, 23, 68, 0.35); border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.35); border-color: {C.PRI}; }}
        """)
        dismiss.clicked.connect(
            lambda: (
                w.hide(),
                self.left_search_dock.hide() if hasattr(self, "left_search_dock") and self.left_search_dock else None,
            )
        )
        hdr.addWidget(dismiss)
        lay.addLayout(hdr)

        # ── separator ─────────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};")
        lay.addWidget(sep)
        self._content_sep = sep

        # ── text display ──────────────────────────────────────────────────────
        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content_display.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content_display.setFont(QFont("Montserrat", 8))
        self._content_display.setMinimumHeight(60)
        self._content_display.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
                padding: 6px 8px;
                selection-background-color: {C.PRI_GHO};
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
        lay.addWidget(self._content_display)

        return w

    def _show_content(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel with visual HTML interpretation."""
        import time as _time

        self.hud.glance(0.0, -0.85, hold=1.3)
        self._content_title_lbl.setText(title.replace("//", "-").upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%I:%M:%S %p"))
        visual_html = format_visual_content_html(text)
        self._content_display.setHtml(visual_html)
        self._content_display.moveCursor(self._content_display.textCursor().MoveOperation.Start)
        self._content_panel.show()
        if hasattr(self, "left_search_dock"):
            self.left_search_dock.popup(title, _time.strftime("%I:%M:%S %p"))

    def _toggle_search_dock_minimize(self):
        if hasattr(self, "left_search_dock") and self.left_search_dock:
            self.left_search_dock.toggle_minimize()

    def _on_jarvis_window_control(self, action: str):
        action = (action or "").lower().strip()
        if action == "minimize":
            if not getattr(self, "_is_minimized_top", False):
                self.toggle_minimize_top()
            else:
                self.showMinimized()
        elif action in ("maximize", "restore"):
            if getattr(self, "_is_minimized_top", False):
                self.toggle_minimize_top()
            if self.isMinimized():
                self.showNormal()
            self.raise_()
            self.activateWindow()

    def _show_slide_alert(self, title: str, message: str):
        if getattr(self, "_slide_alert", None) is None:
            self._slide_alert = SlideDownAlert(self)
        self._slide_alert.popup_alert(title, message)

    def _show_reminders(self, scope: str = "upcoming"):
        if getattr(self, "_reminders_dock", None) is None:
            self._reminders_dock = RemindersDisplayWindow(self)
        self._reminders_dock.popup(scope)

    def _show_tasks(self, scope: str = "all"):
        if getattr(self, "_tasks_dock", None) is None:
            self._tasks_dock = TasksDisplayWindow(self)
        self._tasks_dock.popup(scope)

    def _show_memory_actions(self, tab: str = "memory"):
        if getattr(self, "_memory_actions_dock", None) is None:
            self._memory_actions_dock = MemoryActionsDisplayWindow(self)
        self._memory_actions_dock.popup(tab)

    def _show_telemetry(self):
        if getattr(self, "_telemetry_win", None) is None:
            from gui.display_windows import TelemetryDisplayWindow
            self._telemetry_win = TelemetryDisplayWindow(self)
        self._telemetry_win.show()
        self._telemetry_win.raise_()
        self._telemetry_win.activateWindow()

    # ── the coding agent, live ───────────────────────────────────────────────
    # The panel under the head shows a folder job while it runs: what was asked,
    # the plan, every file with its state, and the steps as they happen. Drawn
    # from a snapshot the job publishes on every step, so this slot never reads
    # anything a worker thread is still changing.
    _JOB_STATES = {
        "waiting": ("TEXT_DIM", "○"),
        "writing": ("PRI", "✎"),
        "done": ("GREEN", "✓"),
        "fixing": ("ACC2", "↻"),
        "skipped": ("TEXT_DIM", "–"),
        "failed": ("RED", "✕"),
        "broken": ("RED", "!"),
    }
    _JOB_PHASES = ("reading", "planning", "writing", "checking")

    def _show_code_job(self, job: dict):
        """Slot — Qt main thread. Draws the coding agent's progress in dedicated Task Display Window."""
        import time as _time

        if not job:
            return

        folder = str(job.get("folder") or "TASK")
        task_id = folder

        # Find or open dedicated task progress display window
        task_win = None
        for w in list(self._displays.values()):
            if getattr(w, "content_type", "") == "task" and getattr(w, "_task_id", "") == task_id:
                task_win = w
                break

        if task_win is not None:
            task_win.update_task_data(job)
        else:
            disp_id = self._on_open_display("task", job, f"TASK: {task_id}")
            task_win = self._displays.get(disp_id)
            if task_win:
                task_win._task_id = task_id
        e = self._esc
        col = lambda name: getattr(C, name, C.TEXT)
        status = job.get("status", "")
        finished = job.get("finished")
        took = (finished or _time.time()) - job.get("started", _time.time())

        # Phase strip: where in the job it is, readable at a glance.
        strip = []
        phases = list(job.get("phases") or self._JOB_PHASES)
        here = phases.index(status) if status in phases else (len(phases) if finished else -1)
        for i, ph in enumerate(phases):
            c = C.GREEN if i < here else (C.PRI if i == here else C.TEXT_DIM)
            mark = "●" if i <= here else "○"
            strip.append(f'<span style="color:{c};">{mark} {ph.upper()}</span>')
        if finished:
            fin = C.GREEN if status == "done" else (C.RED if status == "failed" else C.ACC2)
            strip.append(f'<span style="color:{fin};"><b>● {e(status.upper())}</b></span>')

        parts = [
            f'<div style="color:{C.TEXT}; font-family:Montserrat;">',
            f'<div style="color:{C.TEXT_DIM};">{"  ".join(strip)}&nbsp;&nbsp;·&nbsp;&nbsp;{took:.0f}s</div>',
            f'<div style="margin-top:6px;"><span style="color:{C.TEXT_DIM};">'
            f"REQUEST&nbsp;</span>{e(job.get('request', ''))}</div>",
        ]
        if job.get("summary"):
            parts.append(
                f'<div style="color:{C.WHITE}; border-left:2px solid {C.PRI};'
                f' padding-left:8px; margin:8px 0;">{e(job["summary"])}</div>'
            )
        files = job.get("files") or {}
        if files:
            parts.append(f'<div style="color:{C.TEXT_DIM}; margin-top:4px;">FILES</div>')
            for path, f in files.items():
                cname, mark = self._JOB_STATES.get(f.get("state", ""), ("TEXT_DIM", "·"))
                verb = "new " if f.get("action") == "create" else ""
                parts.append(
                    f'<div><span style="color:{col(cname)};">{mark}</span>&nbsp;'
                    f'<span style="color:{C.WHITE};">{e(path)}</span>&nbsp;'
                    f'<span style="color:{C.TEXT_DIM};">{verb}{e(f.get("what", ""))[:110]}'
                    f"</span></div>"
                )
        # Newest first, so the latest step sits under the plan and the whole
        # state of the job is visible without scrolling in a 220-pixel panel.
        steps = job.get("steps") or []
        if steps:
            parts.append(f'<div style="color:{C.TEXT_DIM}; margin-top:8px;">STEPS — newest first</div>')
            for t, msg in reversed(steps[-14:]):
                parts.append(f'<div><span style="color:{C.TEXT_DIM};">{float(t):6.1f}s</span>&nbsp;{e(msg)}</div>')
        parts.append("</div>")

        title = f"CODE AGENT — {job.get('folder', '')} · {job.get('model', '')}"
        first = self._content_title_lbl.text() != title.upper()[:48]
        self._content_title_lbl.setText(title.replace("//", "-").upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%I:%M:%S %p"))
        bar = self._content_display.verticalScrollBar()
        keep = 0 if first else bar.value()
        self._content_display.setHtml("".join(parts))
        # setHtml jumps to the top; put the reader back where they were.
        bar.setValue(keep)
        if task_win is None and not self._content_panel.isVisible():
            self.hud.glance(0.0, -0.85, hold=1.3)
            self._content_panel.show()
            if hasattr(self, "left_search_dock") and self.left_search_dock:
                self.left_search_dock.popup(title, _time.strftime("%I:%M:%S %p"))
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 260, 120), 260])

    # ── document review ──────────────────────────────────────────────────────
    # Rendered as rich text into the content panel that already exists, rather
    # than into a panel of its own. A review is read, not clicked, so QTextEdit
    # gives scrolling, selection and copy for nothing, and the HUD gains no
    # widget it has to lay out. Severity decides colour and order here because
    # that is presentation; the plugin supplies no styling and knows no palette,
    # which is also what lets a re-theme repaint a review correctly.

    # Severity is marked by a symbol and a colour, not by a word. The findings
    # themselves are in the user's language, and "[SERIOUS]" sitting inside a
    # Turkish sentence is the kind of seam this project tries not to have —
    # while translating the tag would mean a table per language, which is worse.
    # A shape carries it in every language, and shape plus colour still reads
    # for someone who cannot separate red from amber. What the marks mean
    # arrives the way everything else does: JARVIS says it out loud.
    _REVIEW_MARKS = {"serious": ("RED", "▲"), "caution": ("ACC2", "●"), "note": ("PRI_DIM", "·")}

    @staticmethod
    def _esc(s) -> str:
        return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

    def _show_review(self, title: str, summary: str, findings, unclear):
        """Slot — Qt main thread. Lays a document review into the content panel."""
        if hasattr(self, "left_search_dock"):
            self.left_search_dock.popup("REVIEW: " + title, time.strftime("%I:%M:%S %p"))
        e = self._esc
        parts = [f'<div style="color:{C.TEXT}; font-family:Montserrat;">']

        if summary:
            parts.append(
                f'<div style="color:{C.WHITE}; border-left:2px solid {C.PRI};'
                f' padding-left:8px; margin-bottom:10px;">{e(summary)}</div>'
            )

        for f in findings or []:
            key, mark = self._REVIEW_MARKS.get(f.get("severity"), ("PRI_DIM", "·"))
            colour = getattr(C, key)
            parts.append(f'<div style="margin-bottom:11px;">')
            parts.append(
                f'<span style="color:{colour}; font-weight:bold;">{mark}</span> '
                f'<span style="color:{C.WHITE}; font-weight:bold;">'
                f"{e(f.get('heading'))}</span>"
            )
            if f.get("detail"):
                parts.append(f'<div style="margin-left:12px;">{e(f["detail"])}</div>')
            if f.get("quote"):
                # The document's own wording, visually separated from the
                # explanation so the two are never mistaken for each other.
                parts.append(
                    f'<div style="margin-left:12px; color:{C.TEXT_DIM};'
                    f' border-left:1px solid {C.BORDER}; padding-left:7px;">'
                    f"&ldquo;{e(f['quote'])}&rdquo;</div>"
                )
            if f.get("suggestion"):
                parts.append(f'<div style="margin-left:12px; color:{C.PRI};">&rarr; {e(f["suggestion"])}</div>')
            parts.append("</div>")

        if unclear:
            parts.append(
                f'<div style="margin-top:6px; border-top:1px solid {C.BORDER};'
                f' padding-top:7px; color:{C.TEXT_MED};">'
                "The document does not settle:</div>"
            )
            for u in unclear:
                parts.append(f'<div style="margin-left:12px; color:{C.TEXT_MED};">&middot; {e(u)}</div>')
        parts.append("</div>")

        import time as _time

        self.hud.glance(0.0, -0.85, hold=1.3)
        # Left as written, not upper-cased. The other content-panel titles are
        # the app's own English labels, but this one is the document's name in
        # the user's language, and str.upper() applies English casing rules to
        # it: Turkish "Sözleşmesi" comes back "SÖZLEŞMESI", having lost the
        # dotted capital İ. Python has no locale-aware upper to reach for, and
        # imposing one language's rules on all of them is the bug, not the fix.
        self._content_title_lbl.setText((title or "Document")[:48])
        self._content_ts_lbl.setText(_time.strftime("%I:%M:%S %p"))
        self._content_display.setHtml("".join(parts))
        self._content_display.moveCursor(self._content_display.textCursor().MoveOperation.Start)
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 260, 120), 260, 0])

    # ── quiz panel ───────────────────────────────────────────────────────────
    # An interactive twin of the content panel. The plugin only ever hands over
    # questions; everything about asking, marking and reporting happens here,
    # and the finished result is pushed back into the conversation the same way
    # a dropped file is — as a message JARVIS reads and responds to. That keeps
    # the tool call short (it returns the moment the board is up) and leaves the
    # talking to the assistant, in the user's own language.

    def _quiz_btn(self, text: str, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setFont(QFont("Montserrat", 8))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumHeight(24)
        edge = C.BORDER_B if primary else C.BORDER
        col = C.PRI if primary else C.TEXT_MED
        b.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {col};
                border: 1px solid {edge}; border-radius: 2px;
                padding: 3px 9px; text-align: left;
            }}
            QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI_DIM}; }}
            QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: {C.BORDER}; }}
        """)
        return b

    def _build_quiz_panel(self) -> QWidget:
        w = QWidget()
        w.setObjectName("QuizPanel")
        w.setStyleSheet(f"""
            QWidget#QuizPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        dot = QLabel("◈")
        dot.setFont(QFont("Montserrat", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._quiz_title_lbl = QLabel("QUIZ")
        self._quiz_title_lbl.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        self._quiz_title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent; letter-spacing: 1px;")
        hdr.addWidget(self._quiz_title_lbl)
        hdr.addStretch()

        self._quiz_count_lbl = QLabel("")
        self._quiz_count_lbl.setFont(QFont("Montserrat", 7))
        self._quiz_count_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._quiz_count_lbl)

        quit_btn = QPushButton("DISMISS  ✕")
        quit_btn.setFont(QFont("Montserrat", 7))
        quit_btn.setFixedHeight(18)
        quit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        quit_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 23, 68, 0.12); color: {C.TEXT_DIM};
                border: 1px solid rgba(255, 23, 68, 0.35); border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: #ffffff; background: rgba(255, 23, 68, 0.35); border-color: {C.PRI}; }}
        """)
        quit_btn.clicked.connect(
            lambda: (
                self._hide_quiz(),
                self.left_search_dock.hide() if hasattr(self, "left_search_dock") and self.left_search_dock else None,
            )
        )
        hdr.addWidget(quit_btn)
        lay.addLayout(hdr)

        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {C.BORDER};")
        lay.addWidget(rule)

        self._quiz_q_lbl = QLabel("")
        self._quiz_q_lbl.setWordWrap(True)
        self._quiz_q_lbl.setFont(QFont("Montserrat", 9))
        self._quiz_q_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        lay.addWidget(self._quiz_q_lbl)

        self._quiz_answers = QWidget()
        self._quiz_answers.setStyleSheet("background: transparent;")
        self._quiz_answers_lay = QVBoxLayout(self._quiz_answers)
        self._quiz_answers_lay.setContentsMargins(0, 2, 0, 0)
        self._quiz_answers_lay.setSpacing(4)
        lay.addWidget(self._quiz_answers)

        self._quiz_note_lbl = QLabel("")
        self._quiz_note_lbl.setWordWrap(True)
        self._quiz_note_lbl.setFont(QFont("Montserrat", 8))
        self._quiz_note_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._quiz_note_lbl.hide()
        lay.addWidget(self._quiz_note_lbl)

        foot = QHBoxLayout()
        foot.addStretch()
        self._quiz_next_btn = self._quiz_btn("NEXT  →", primary=True)
        self._quiz_next_btn.setFixedWidth(110)
        self._quiz_next_btn.clicked.connect(self._quiz_next)
        self._quiz_next_btn.hide()
        foot.addWidget(self._quiz_next_btn)
        lay.addLayout(foot)

        self._quiz = None
        return w

    def _show_quiz(self, topic: str, questions, grader=None):
        """Slot — Qt main thread. Puts a fresh quiz on the board."""
        if not questions:
            return
        if hasattr(self, "left_search_dock"):
            self.left_search_dock.popup("QUIZ: " + (topic or "QUIZ"), time.strftime("%I:%M:%S %p"))
        self._quiz = {
            "topic": topic or "",
            "questions": list(questions),
            "grader": grader,
            "i": 0,
            "results": [],
            "answered": False,
        }
        self._quiz_title_lbl.setText((topic or "quiz").upper()[:48])
        self.hud.glance(0.0, -0.85, hold=1.3)
        first_show = not self._quiz_panel.isVisible()
        self._quiz_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 250, 120), 0, 250])
        self._quiz_render()

    def _hide_quiz(self):
        self._quiz = None
        self._quiz_panel.hide()

    def _quiz_clear_answers(self):
        while self._quiz_answers_lay.count():
            item = self._quiz_answers_lay.takeAt(0)
            child = item.widget()
            if child is not None:
                child.setParent(None)
                child.deleteLater()

    def _quiz_render(self):
        q = self._quiz["questions"][self._quiz["i"]]
        n, total = self._quiz["i"] + 1, len(self._quiz["questions"])
        self._quiz_count_lbl.setText(f"{n} / {total}")
        self._quiz_q_lbl.setText(q.get("question", ""))
        self._quiz_note_lbl.hide()
        self._quiz_next_btn.hide()
        self._quiz["answered"] = False
        self._quiz_clear_answers()

        opts = q.get("options") or []
        if opts:
            for text in opts:
                b = self._quiz_btn("   " + text)
                b.clicked.connect(lambda _=False, t=text: self._quiz_submit(t))
                self._quiz_answers_lay.addWidget(b)
        else:
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
            field = QLineEdit()
            field.setFont(QFont("Montserrat", 9))
            field.setPlaceholderText("your answer")
            field.setStyleSheet(f"""
                QLineEdit {{
                    background: {C.PANEL2}; color: {C.WHITE};
                    border: 1px solid {C.BORDER}; border-radius: 2px; padding: 4px 7px;
                }}
                QLineEdit:focus {{ border-color: {C.PRI_DIM}; }}
            """)
            send = self._quiz_btn("ANSWER", primary=True)
            send.setFixedWidth(90)
            field.returnPressed.connect(lambda: self._quiz_submit(field.text()))
            send.clicked.connect(lambda: self._quiz_submit(field.text()))
            h.addWidget(field, stretch=1)
            h.addWidget(send)
            self._quiz_answers_lay.addWidget(row)
            field.setFocus()

    def _quiz_submit(self, given: str):
        if self._quiz is None or self._quiz["answered"]:
            return
        self._quiz["answered"] = True
        q = self._quiz["questions"][self._quiz["i"]]
        grader = self._quiz.get("grader")
        verdict = None
        if callable(grader):
            try:
                verdict = grader(q, given)
            except Exception:
                verdict = None
        self._quiz["results"].append(
            {
                "question": q.get("question", ""),
                "type": q.get("type", ""),
                "given": str(given or "").strip(),
                "answer": q.get("answer", ""),
                "correct": verdict,
            }
        )

        for i in range(self._quiz_answers_lay.count()):
            wdg = self._quiz_answers_lay.itemAt(i).widget()
            if wdg is not None:
                wdg.setEnabled(False)

        if verdict is True:
            mark, colour = "✓  correct", C.GREEN
        elif verdict is False:
            mark, colour = "✕  " + str(q.get("answer", "")), C.RED
        else:
            # Open answers and near-miss gap-fills are JARVIS's to judge. Saying
            # so is honest; marking it wrong here would be a guess.
            mark, colour = "…  noted — I'll go over this one with you", C.ACC2
        note = q.get("note") or ""
        self._quiz_note_lbl.setText(mark + (("\n" + note) if note else ""))
        self._quiz_note_lbl.setStyleSheet(f"color: {colour}; background: transparent;")
        self._quiz_note_lbl.show()

        last = self._quiz["i"] >= len(self._quiz["questions"]) - 1
        self._quiz_next_btn.setText("FINISH  →" if last else "NEXT  →")
        self._quiz_next_btn.show()
        self._quiz_next_btn.setFocus()

    def _quiz_next(self):
        if self._quiz is None:
            return
        if self._quiz["i"] >= len(self._quiz["questions"]) - 1:
            self._quiz_finish()
        else:
            self._quiz["i"] += 1
            self._quiz_render()

    def _quiz_finish(self):
        if self._quiz is None:
            return
        topic = self._quiz["topic"]
        results = self._quiz["results"]
        right = sum(1 for r in results if r["correct"] is True)
        unsure = sum(1 for r in results if r["correct"] is None)
        total = len(results)
        self._quiz_panel.hide()
        self._quiz = None

        self._log.append_log(f"QUIZ: {topic or 'quiz'} — {right}/{total} correct")

        # Hand it back to JARVIS as a message, not as a tool return: the tool
        # call ended minutes ago. This is the same channel a dropped file uses.
        lines = [
            f"[QUIZ_DONE] topic={topic or 'general'} | "
            f"auto-marked {right}/{total} correct" + (f", {unsure} still need your marking" if unsure else "")
        ]
        for i, r in enumerate(results, 1):
            state = "correct" if r["correct"] is True else "wrong" if r["correct"] is False else "NEEDS MARKING"
            lines.append(
                f"{i}. [{r['type']}] {r['question']} | they answered: "
                f"{r['given'] or '(blank)'} | expected: {r['answer']} | {state}"
            )
        lines.append(
            "Mark every question flagged NEEDS MARKING yourself — accept an answer "
            "that means the same thing. Then tell them how they did in their own "
            "language: the score, what they got wrong and why, in a couple of "
            "sentences. Offer another round only if it fits. "
            "Remember something only if it would still matter next week — that they "
            "are working through a subject, or keep missing the same thing. A score "
            "from one session is not worth a memory, and a memory per quiz would "
            "bury the things that are."
        )
        msg = "\n".join(lines)
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()
