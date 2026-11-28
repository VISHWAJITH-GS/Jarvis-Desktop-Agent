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


class HudFadeOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            p.end()
            return

        fade_h = min(14, max(8, int(h * 0.10)))

        # Top blur / gradient fade
        bg_col = QColor(10, 4, 6, 180)
        top_grad = QLinearGradient(0, 0, 0, fade_h)
        top_grad.setColorAt(0.0, bg_col)
        top_grad.setColorAt(0.5, QColor(10, 4, 6, 90))
        top_grad.setColorAt(1.0, QColor(10, 4, 6, 0))
        p.fillRect(QRect(0, 0, w, fade_h), top_grad)

        # Bottom blur / gradient fade
        bot_grad = QLinearGradient(0, h - fade_h, 0, h)
        bot_grad.setColorAt(0.0, QColor(10, 4, 6, 0))
        bot_grad.setColorAt(0.5, QColor(10, 4, 6, 90))
        bot_grad.setColorAt(1.0, bg_col)
        p.fillRect(QRect(0, h - fade_h, w, fade_h), bot_grad)

        p.end()


class HudConversationWidget(QTextEdit):
    _stream_user_sig = pyqtSignal(str)
    _stream_reply_sig = pyqtSignal(str)
    _add_msg_sig = pyqtSignal(str, str)

    def __init__(self, assistant_name: str = "JARVIS", parent=None):
        super().__init__(parent)
        self._asst_name = (assistant_name or "JARVIS").replace(".", "").strip().upper()
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().setMaximumBlockCount(400)
        self.setFont(QFont("Montserrat", 9))
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                color: {C.TEXT};
                border: none;
                padding: 4px 6px 6px 6px;
                selection-background-color: rgba(255, 23, 68, 0.35);
            }}
            QScrollBar:vertical {{
                width: 0px;
                height: 0px;
                background: transparent;
                border: none;
            }}
        """)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setAutoFillBackground(False)

        self._messages: list[tuple[str, str]] = []
        self._streaming_user: str = ""
        self._streaming_reply: str = ""

        # Top & bottom blur/gradient fade overlay
        self._fade_overlay = HudFadeOverlay(self)

        self._stream_user_sig.connect(self._on_stream_user)
        self._stream_reply_sig.connect(self._on_stream_reply)
        self._add_msg_sig.connect(self._on_add_msg)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_fade_overlay") and self._fade_overlay:
            self._fade_overlay.setGeometry(0, 0, self.width(), self.height())
            self._fade_overlay.raise_()

    def append_message(self, speaker: str, text: str):
        self._add_msg_sig.emit(speaker, text)

    def stream_user(self, text: str):
        self._stream_user_sig.emit(text)

    def stream_reply(self, text: str):
        self._stream_reply_sig.emit(text)

    def _on_stream_user(self, text: str):
        self._streaming_user = str(text or "").strip()
        self._render_conversation()

    def _on_stream_reply(self, text: str):
        clean = str(text or "").strip()
        # If the streaming reply text is already committed in the last assistant message, do not duplicate!
        if self._messages and self._messages[-1][0] == "jarvis":
            last_msg = self._messages[-1][1].strip().lower()
            if clean.lower() == last_msg or last_msg.startswith(clean.lower()):
                self._streaming_reply = ""
                self._render_conversation()
                return
        self._streaming_reply = clean
        self._render_conversation()

    def _on_add_msg(self, speaker: str, text: str):
        clean = str(text or "").strip()
        if not clean:
            return
        spk = speaker.lower().strip()
        if spk == "you":
            self._streaming_user = ""
            if not (self._messages and self._messages[-1] == ("you", clean)):
                self._messages.append(("you", clean))
        elif spk in ("ai", "jarvis", self._asst_name.lower()):
            self._streaming_reply = ""
            # Deduplicate identical consecutive assistant messages
            if not (self._messages and self._messages[-1][0] == "jarvis" and self._messages[-1][1].strip().lower() == clean.lower()):
                self._messages.append(("jarvis", clean))
        else:
            self._messages.append(("sys", clean))
        if len(self._messages) > 100:
            self._messages = self._messages[-100:]
        self._render_conversation()

    def _render_conversation(self):
        html_parts = []
        html_parts.append(f"<div style=\"font-family: 'Montserrat', monospace; color: {C.TEXT}; font-size: 11px;\">")

        for idx, (spk, txt) in enumerate(self._messages[-50:]):
            escaped = txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            top_margin = "2px" if idx == 0 else "6px"
            if spk == "you":
                html_parts.append(
                    f'<div style="margin-top: {top_margin}; margin-bottom: 2px;">'
                    f'<span style="color: {C.TEXT_DIM}; font-size: 9px; font-weight: bold; letter-spacing: 1.5px;">YOU</span>'
                    f"</div>"
                    f'<div style="color: #ffffff; margin-bottom: 8px; line-height: 1.35;">{escaped}</div>'
                )
            elif spk == "jarvis":
                html_parts.append(
                    f'<div style="margin-top: {top_margin}; margin-bottom: 2px;">'
                    f'<span style="color: {C.PRI}; font-size: 9px; font-weight: bold; letter-spacing: 1.5px;">{self._asst_name}</span>'
                    f"</div>"
                    f'<div style="color: {C.TEXT}; border-left: 2px solid {C.PRI}; padding-left: 6px; margin-bottom: 8px; line-height: 1.35;">{escaped}</div>'
                )
            else:
                html_parts.append(
                    f'<div style="color: {C.TEXT_DIM}; font-size: 10px; font-style: italic; margin-bottom: 4px;">{escaped}</div>'
                )

        if self._streaming_user:
            esc = (
                self._streaming_user.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>")
            )
            html_parts.append(
                f'<div style="margin-top: 6px; margin-bottom: 2px;">'
                f'<span style="color: {C.TEXT_DIM}; font-size: 9px; font-weight: bold; letter-spacing: 1.5px;">YOU</span>'
                f"</div>"
                f'<div style="color: #ffffff; margin-bottom: 8px; line-height: 1.35;">{esc} <span style="color: {C.PRI};">...</span></div>'
            )

        if self._streaming_reply:
            is_dup = False
            if self._messages and self._messages[-1][0] == "jarvis":
                last_txt = self._messages[-1][1].strip().lower()
                cur_txt = self._streaming_reply.strip().lower()
                if cur_txt == last_txt or last_txt.startswith(cur_txt):
                    is_dup = True
            if not is_dup:
                esc = (
                    self._streaming_reply.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace("\n", "<br>")
                )
                html_parts.append(
                    f'<div style="margin-top: 6px; margin-bottom: 2px;">'
                    f'<span style="color: {C.PRI}; font-size: 9px; font-weight: bold; letter-spacing: 1.5px;">{self._asst_name}</span>'
                    f"</div>"
                    f'<div style="color: {C.TEXT}; border-left: 2px solid {C.PRI}; padding-left: 6px; margin-bottom: 8px; line-height: 1.35;">{esc} <span style="color: {C.PRI}; font-weight: bold;">█</span></div>'
                )

        html_parts.append("</div>")

        bar = self.verticalScrollBar()
        old_val = bar.value()
        was_at_bottom = (bar.maximum() == 0) or (old_val >= bar.maximum() - 35)

        self.setHtml("".join(html_parts))

        if was_at_bottom or self._streaming_reply or self._streaming_user:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(old_val)

    def clear_history(self):
        self._messages.clear()
        self._streaming_user = ""
        self._streaming_reply = ""
        self.setHtml("")


class HudCanvas(QWidget):
    reactor_clicked = pyqtSignal()

    def __init__(self, face_path: str = "", assistant_name: str = "JARVIS", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(40, 40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._assistant_name = assistant_name.replace(".", "").strip() or "JARVIS"
        self.state = "STANDBY"
        self.muted = False
        self.speaking = False
        self.hud_style = "core"
        self._suppress_transcript: bool = False

        # Embedded Conversation History widget
        self.conversation = HudConversationWidget(assistant_name=self._assistant_name, parent=self)

        # Audio & animation dynamics
        self._live_amp = 0.0
        self._amp_disp = 0.0
        self._level_target = 0.0
        self._visemes = None
        self._vis_i = None

        self.spin = 0.0
        self.boot = 1.0
        self.wave = [0.0] * 72
        self.cards = []  # list of (action_text, timestamp)
        self.user_text = ""
        self.reply_text = ""
        self.reply_shown = 0.0
        self.stat = "LIVE"
        self.cpu = 0.0
        self.mem = 0.0
        self.gemini_online = True
        self._big_mode = False
        self._mini_mode = False

        self._active_state = "STANDBY"
        self._prev_state = "STANDBY"
        self._state_start_t = time.time()
        self._prev_accent = COLOR_STATE_LISTENING
        self._current_accent = COLOR_STATE_LISTENING
        self._reduced_motion = False

        self._last_t = time.time()
        self._step_t = time.time()
        self._drag_start = None
        self._win_start = None
        self._is_drag = False

        self._fps_interval = 16
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(self._fps_interval)

    def _update_timer_interval(self):
        w = self.window()
        is_hidden_or_min = not self.isVisible() or (w is not None and w.isMinimized())
        if is_hidden_or_min:
            target_ms = 500  # near-zero (2 FPS) when minimized or hidden
        elif getattr(self, "_active_state", "STANDBY") in ("SPEAKING", "THINKING"):
            target_ms = 16  # 60 FPS during speaking/thinking
        elif getattr(self, "_active_state", "STANDBY") in ("LISTENING", "PROCESSING", "EXECUTING"):
            target_ms = 33  # 30 FPS during listening/executing
        else:
            target_ms = 66  # 15 FPS when idle / STANDBY / SLEEPING / MUTED

        if getattr(self, "_fps_interval", 16) != target_ms:
            self._fps_interval = target_ms
            self._tmr.setInterval(target_ms)

    def on_state_changed(self, new_state: str):
        """Immediately update state, reset phase, and transition colors smoothly."""
        st = (new_state or "STANDBY").upper()
        if st != getattr(self, "_active_state", None):
            self._prev_state = getattr(self, "_active_state", "STANDBY")
            self._prev_accent = getattr(self, "_current_accent", COLOR_STATE_LISTENING)
            self._active_state = st
            self._state_start_t = time.time()
            self.state = st
            self.speaking = st == "SPEAKING"
            self._update_timer_interval()
            self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_conversation_geometry()

    def showEvent(self, e):
        super().showEvent(e)
        self._update_conversation_geometry()
        self._update_timer_interval()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._update_timer_interval()

    def changeEvent(self, e):
        super().changeEvent(e)
        self._update_timer_interval()

    def set_suppress_transcript(self, suppress: bool) -> None:
        """Collapse / hide the mini-transcript in the HUD canvas when chat log is open elsewhere."""
        self._suppress_transcript = bool(suppress)
        self._update_conversation_geometry()

    def _update_conversation_geometry(self):
        if not hasattr(self, "conversation") or not self.conversation:
            return
        if (
            getattr(self, "_mini_mode", False)
            or getattr(self, "_big_mode", False)
            or getattr(self, "_suppress_transcript", False)
        ):
            self.conversation.hide()
            return

        w, h = self.width(), self.height()
        if w <= 120 or h <= 120:
            self.conversation.hide()
            return

        BASE_W = 420.0
        BASE_H = 540.0
        scale = min(w / BASE_W, h / BASE_H)
        off_x = (w - BASE_W * scale) / 2.0
        off_y = (h - BASE_H * scale) / 2.0

        card_count = len([c for c, born in getattr(self, "cards", []) if time.time() - born <= 7])
        top_y = 368.0 if card_count > 0 else 352.0
        bot_y = BASE_H - 12.0
        conv_h = max(50.0, bot_y - top_y)

        c_x = int(off_x + 16.0 * scale)
        c_y = int(off_y + top_y * scale)
        c_w = int((BASE_W - 32.0) * scale)
        c_h = int(conv_h * scale)

        self.conversation.setGeometry(c_x, c_y, c_w, c_h)
        self.conversation.show()

    def glance(self, dx: float, dy: float, hold: float = 1.1) -> None:
        pass

    def push_visemes(self, frames, hop: float, at: float) -> None:
        try:
            if not frames:
                return
            hop = max(1e-3, float(hop))
            at = float(at)
            new = list(frames)
            cur = self._visemes
            if cur is not None:
                old, t0, ohop = cur
                if abs(ohop - hop) < 1e-6:
                    i = int(round((at - t0) / hop))
                    if 0 <= i <= len(old) + 1:
                        merged = old[:i] + new
                        played = int((time.time() - t0) / hop) - 2
                        if played > 60:
                            merged = merged[played:]
                            t0 += played * hop
                            if self._vis_i is not None:
                                self._vis_i = max(0, self._vis_i - played)
                        self._visemes = (merged, t0, hop)
                        return
            self._visemes = (new, at, hop)
            self._vis_i = None
        except Exception:
            pass

    def set_audio_level(self, level: float) -> None:
        try:
            lv = max(0.0, min(1.0, float(level)))
            if lv > self._live_amp:
                self._live_amp = lv
        except (TypeError, ValueError):
            pass

    def set_metrics(self, cpu: float, mem: float, stat: str = "") -> None:
        self.cpu = float(cpu)
        self.mem = float(mem)
        if stat:
            self.stat = str(stat)

    def stream_user_text(self, text: str) -> None:
        self.user_text = str(text or "").strip()
        if hasattr(self, "conversation") and self.conversation:
            self.conversation.stream_user(text)

    def stream_reply_text(self, text: str) -> None:
        self.reply_text = str(text or "").strip()
        if hasattr(self, "conversation") and self.conversation:
            self.conversation.stream_reply(text)

    def set_user_text(self, text: str) -> None:
        self.user_text = str(text or "").strip()
        if hasattr(self, "conversation") and self.conversation:
            self.conversation.append_message("you", self.user_text)

    def set_reply_text(self, text: str) -> None:
        new_text = str(text or "").strip()
        keep = bool(self.reply_text and new_text.startswith(self.reply_text))
        self.reply_text = new_text
        if not keep:
            self.reply_shown = 0.0
        if hasattr(self, "conversation") and self.conversation:
            self.conversation.append_message("jarvis", self.reply_text)

    def add_card(self, card_text: str) -> None:
        clean = str(card_text or "").strip()
        if clean:
            self.cards = (self.cards + [(clean, time.time())])[-3:]

    def set_mini_mode(self, mini: bool) -> None:
        self._mini_mode = bool(mini)
        self._update_conversation_geometry()
        self.update()

    def set_big_mode(self, big: bool) -> None:
        self._big_mode = bool(big)
        self._update_conversation_geometry()
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_start = e.globalPosition().toPoint()
            self._win_start = self.window().pos()
            self._press_pos = e.position()
            self._is_drag = False
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if hasattr(self, "_drag_start") and self._drag_start and e.buttons() == Qt.MouseButton.LeftButton:
            delta = e.globalPosition().toPoint() - self._drag_start
            if delta.manhattanLength() > 4:
                self._is_drag = True
                self.window().move(self._win_start + delta)
                main_w = self.window()
                if hasattr(main_w, "_is_minimized_top") and not main_w._is_minimized_top:
                    main_w._normal_geom = main_w.geometry()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if not getattr(self, "_is_drag", False):
                if getattr(self, "_mini_mode", False):
                    self.reactor_clicked.emit()
                elif self._big_mode and hasattr(self, "_press_pos"):
                    cx = self.width() / 2
                    cy = self.height() * 0.43
                    max_r = self.height() * 0.30
                    if math.hypot(self._press_pos.x() - cx, self._press_pos.y() - cy) <= max_r:
                        self.reactor_clicked.emit()
                elif hasattr(self, "_press_pos"):
                    BASE_W, BASE_H = 420.0, 540.0
                    scale = min(self.width() / BASE_W, self.height() / BASE_H)
                    off_x = (self.width() - BASE_W * scale) / 2.0
                    off_y = (self.height() - BASE_H * scale) / 2.0
                    local_x = (self._press_pos.x() - off_x) / scale
                    local_y = (self._press_pos.y() - off_y) / scale
                    cx, cy = BASE_W / 2, 185.0
                    if math.hypot(local_x - cx, local_y - cy) <= 135:
                        self.reactor_clicked.emit()
            self._drag_start = None
            self._is_drag = False
        super().mouseReleaseEvent(e)

    def _step(self):
        try:
            w = self.window()
            if not self.isVisible() or (w is not None and w.isMinimized()):
                self._update_timer_interval()
                return

            now = time.time()
            prev_t = getattr(self, "_step_t", now)
            dt = min(max(0.0, now - prev_t), 0.1)
            self._step_t = now

            # Visemes / Peak audio
            sched = getattr(self, "_visemes", None)
            if sched is not None and isinstance(sched, (tuple, list)) and len(sched) == 3:
                frames, t0, hop = sched
                if hop > 0:
                    i = int((now - t0) / hop)
                    if 0 <= i < len(frames):
                        v_seq = frames[max(0, self._vis_i or i) : i + 1]
                        self._vis_i = i + 1
                        if v_seq:
                            peak = max((f[0] if isinstance(f, (tuple, list)) else float(f)) for f in v_seq)
                            if peak > self._live_amp:
                                self._live_amp = peak
                    elif i >= len(frames):
                        self._visemes = None
                        self._vis_i = None

            self._live_amp = getattr(self, "_live_amp", 0.0) * 0.88
            self._amp_disp = getattr(self, "_amp_disp", 0.0) + (self._live_amp - getattr(self, "_amp_disp", 0.0)) * 0.45
            amp = self._amp_disp

            st = (self.state or "STANDBY").upper()
            if self.muted:
                st = "MUTED"

            if st != getattr(self, "_active_state", None):
                self._prev_state = getattr(self, "_active_state", "STANDBY")
                self._prev_accent = getattr(self, "_current_accent", COLOR_STATE_LISTENING)
                self._active_state = st
                self._state_start_t = now

            speed = {
                "THINKING": 220,
                "PROCESSING": 180,
                "LISTENING": 28,
                "SPEAKING": 45,
                "EXECUTING": 160,
                "INTERRUPTED": 90,
                "RECONNECTING": 12,
                "MUTED": 10,
                "SLEEPING": 6,
            }.get(st, 14)
            self.spin = (getattr(self, "spin", 0.0) + dt * speed) % 360.0

            if st == "SPEAKING":
                self._level_target = max(amp, 0.35 + 0.65 * abs(math.sin(now * 5.3) * math.sin(now * 2.1 + 1)))
            elif st == "LISTENING":
                # Live sound frequency indication when user talks to JARVIS
                self._level_target = max(amp * 1.5, self._live_amp * 1.5)
            elif st in ("THINKING", "PROCESSING"):
                self._level_target = 0.2 + 0.2 * abs(math.sin(now * 3.5))
            else:
                self._level_target = max(0.0, amp * 0.5)

            cur_lvl = getattr(self, "level", 0.0)
            cur_lvl += (self._level_target - cur_lvl) * min(1.0, dt * 14)
            self.level = cur_lvl

            lvl_sample = self.level * random.uniform(0.55, 1.0) if st == "LISTENING" else 0.0
            if hasattr(self, "wave") and isinstance(self.wave, list) and len(self.wave) > 1:
                self.wave = self.wave[1:] + [lvl_sample]

            if getattr(self, "reply_text", None):
                self.reply_shown = min(len(self.reply_text), getattr(self, "reply_shown", 0) + dt * 45)

            if self.isVisible():
                self.update()
        except Exception:
            pass

    def paintEvent(self, event):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        t = time.time()
        now = t
        w, h = self.width(), self.height()
        st = (self.state or "STANDBY").upper()
        if self.muted:
            st = "MUTED"

        if st != getattr(self, "_active_state", None):
            self._prev_state = getattr(self, "_active_state", "STANDBY")
            self._prev_accent = getattr(self, "_current_accent", COLOR_STATE_LISTENING)
            self._active_state = st
            self._state_start_t = now

        CYAN_ACC = qcol(C.PRI)
        DEEP_ACC = qcol(C.ACC)
        TEXT_COL = qcol(C.TEXT)
        DIM_COL = qcol(C.TEXT_DIM)
        ALERT_COL = qcol(C.MUTED_C)

        if self.muted or st == "MUTED":
            target_accent = QColor(138, 138, 138)  # GRAY when mic is muted
            target_core_mid = QColor(190, 190, 190, 240)
            target_glow_col = QColor(138, 138, 138)
            pulse_speed = 1.0
        elif st == "SLEEPING":
            target_accent = QColor(99, 102, 241)  # CALM INDIGO when low-power sleeping
            target_core_mid = QColor(165, 180, 252, 220)
            target_glow_col = QColor(99, 102, 241)
            pulse_speed = 0.8
        elif self.speaking or st == "SPEAKING":
            target_accent = COLOR_STATE_SPEAKING  # YELLOW when JARVIS is speaking
            target_core_mid = QColor(255, 245, 180, 240)
            target_glow_col = COLOR_STATE_SPEAKING
            pulse_speed = 4.5
        elif st in ("THINKING", "PROCESSING"):
            target_accent = COLOR_BLUE_PRIMARY  # Bright glowing light blue (#38BDF8)
            target_core_mid = COLOR_BLUE_HIGHLIGHT  # Strong highlight (#BAE6FD)
            target_glow_col = COLOR_BLUE_GLOW  # Bright cyan-blue glow (#7DD3FC)
            pulse_speed = 5.0 if st == "THINKING" else 5.5
        elif st in ("ERROR", "INTERRUPTED"):
            target_accent = ALERT_COL
            target_core_mid = QColor(255, 220, 225, 240)
            target_glow_col = ALERT_COL
            pulse_speed = 6.0
        else:
            # Listening / Standby / default -> RED (#ff1744)
            target_accent = COLOR_STATE_LISTENING
            target_core_mid = QColor(255, 225, 230, 245)
            target_glow_col = COLOR_STATE_LISTENING
            pulse_speed = 2.8

        t_state = t - getattr(self, "_state_start_t", t)
        trans_progress = min(1.0, t_state / 0.22)
        prev_accent = getattr(self, "_prev_accent", target_accent)
        accent = _lerp_color(prev_accent, target_accent, trans_progress) if trans_progress < 1.0 else target_accent
        self._current_accent = accent
        core_mid = target_core_mid
        glow_col = target_glow_col

        boot = 1.0

        if self._big_mode:
            self._paint_big(p, accent, boot, t)
            p.end()
            return

        if getattr(self, "_mini_mode", False):
            self._paint_mini_circle(p, accent, core_mid, glow_col, t)
            p.end()
            return

        BASE_W = 420.0
        BASE_H = 540.0
        scale = min(w / BASE_W, h / BASE_H)
        off_x = (w - BASE_W * scale) / 2.0
        off_y = (h - BASE_H * scale) / 2.0

        p.save()
        p.translate(off_x, off_y)
        p.scale(scale, scale)

        w_d, h_d = BASE_W, BASE_H
        cx, cy = w_d / 2, 185

        # ── Frame: cut-corner translucent dark glass
        c = 18
        r = QRectF(6, 6, w_d - 12, h_d - 12)
        panel = QPolygonF(
            [
                QPointF(r.left() + c, r.top()),
                QPointF(r.right() - c, r.top()),
                QPointF(r.right(), r.top() + c),
                QPointF(r.right(), r.bottom() - c),
                QPointF(r.right() - c, r.bottom()),
                QPointF(r.left() + c, r.bottom()),
                QPointF(r.left(), r.bottom() - c),
                QPointF(r.left(), r.top() + c),
            ]
        )
        p.setPen(QPen(rgba(accent, 55), 1))
        p.setBrush(QColor(14, 4, 12, 220))
        p.drawPolygon(panel)

        # Bright angular corner brackets
        p.setPen(QPen(rgba(accent, 210), 2))
        L = 24
        for x, y, dx, dy in (
            (r.left(), r.top(), 1, 1),
            (r.right(), r.top(), -1, 1),
            (r.left(), r.bottom(), 1, -1),
            (r.right(), r.bottom(), -1, -1),
        ):
            p.drawLine(QPointF(x + dx * c, y), QPointF(x + dx * (c + L), y))
            p.drawLine(QPointF(x, y + dy * c), QPointF(x, y + dy * (c + L)))
            p.drawLine(QPointF(x + dx * c, y), QPointF(x, y + dy * c))

        # Title
        title = ".".join(self._assistant_name.upper())
        p.setPen(rgba(TEXT_COL, 235))
        p.setFont(keena_font(11, True, 6))
        p.drawText(QRectF(0, 18, w_d, 22), Qt.AlignmentFlag.AlignCenter, title)
        p.setPen(rgba(accent, 140))
        p.drawLine(QPointF(cx - 65, 42), QPointF(cx + 65, 42))

        # Readouts
        p.setFont(keena_font(8, False, 1.5))
        left = [time.strftime("%I:%M:%S %p"), time.strftime("%a %d %b").upper()]
        right = []
        if self.cpu is not None:
            right += [f"CPU {self.cpu:3.0f}%", f"MEM {self.mem:3.0f}%"]
        gem_txt = "GEMINI LIVE" if self.gemini_online else "OFFLINE"
        right.append(gem_txt)

        for i, s in enumerate(left):
            p.setPen(rgba(TEXT_COL if i == 0 else DIM_COL, 220))
            p.drawText(QRectF(24, 54 + i * 14, 140, 14), Qt.AlignmentFlag.AlignLeft, s)

        for i, s in enumerate(right):
            hot = s.startswith("CPU") and self.cpu >= 90
            p.setPen(rgba(ALERT_COL if hot else DIM_COL, 220))
            p.drawText(QRectF(w_d - 144, 54 + i * 14, 120, 14), Qt.AlignmentFlag.AlignRight, s)

        # Leader lines
        p.setPen(QPen(rgba(accent, 75), 1))
        for side in (-1, 1):
            x0 = cx + side * 175
            p.drawLine(QPointF(x0, 88), QPointF(x0, 102))
            p.drawLine(QPointF(x0, 102), QPointF(cx + side * 108, 118))

        # ── Arc Reactor
        center = QPointF(cx, cy)
        lvl = getattr(self, "level", 0.0)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Soft radial glow with state-based pulsing
        pulse = 0.5 + 0.5 * math.sin(t_state * pulse_speed)
        if getattr(self, "_reduced_motion", False):
            pulse = 0.5

        if st in ("THINKING", "PROCESSING"):
            glow_r = 145 + 32 * pulse + 20 * lvl
            g = QRadialGradient(center, max(glow_r, 1))
            g.setColorAt(0.0, QColor(125, 211, 252, int(155 + 75 * pulse)))  # #7DD3FC bright glow
            g.setColorAt(0.42, QColor(56, 189, 248, int(95 + 71 * pulse)))  # rgba(56, 189, 248, 0.65) outer glow
            g.setColorAt(1.0, QColor(56, 189, 248, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(center, glow_r, glow_r)
            p.setBrush(Qt.BrushStyle.NoBrush)
        elif st == "LISTENING":
            glow_r = 145 + 24 * pulse + 35 * lvl
            g = QRadialGradient(center, max(glow_r, 1))
            g.setColorAt(0.0, rgba(glow_col, int(100 + 60 * pulse)))
            g.setColorAt(0.45, rgba(glow_col, int(35 + 45 * pulse)))
            g.setColorAt(1.0, rgba(glow_col, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(center, glow_r, glow_r)
            p.setBrush(Qt.BrushStyle.NoBrush)
        else:
            glow_r = 145 + 30 * lvl
            g = QRadialGradient(center, max(glow_r, 1))
            g.setColorAt(0.0, rgba(glow_col, 95))
            g.setColorAt(0.45, rgba(glow_col, 30))
            g.setColorAt(1.0, rgba(glow_col, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(center, glow_r, glow_r)
            p.setBrush(Qt.BrushStyle.NoBrush)

        # Outer ring with 4 notch markers
        R = 125
        p.setPen(QPen(rgba(accent, 115), 1.2))
        p.drawEllipse(center, R, R)
        p.setBrush(rgba(accent, 210))
        p.setPen(Qt.PenStyle.NoPen)
        for k in range(4):
            a = math.radians(k * 90 + 45)
            tip = QPointF(cx + math.cos(a) * (R - 7), cy + math.sin(a) * (R - 7))
            side = math.radians(k * 90 + 45 + 90)
            base = QPointF(cx + math.cos(a) * (R + 2), cy + math.sin(a) * (R + 2))
            p.drawPolygon(
                QPolygonF(
                    [
                        tip,
                        base + QPointF(math.cos(side) * 5, math.sin(side) * 5),
                        base - QPointF(math.cos(side) * 5, math.sin(side) * 5),
                    ]
                )
            )
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Radar sweep for thinking
        if st in ("THINKING", "PROCESSING"):
            sweep = QConicalGradient(center, -self.spin * 1.5)
            sweep.setColorAt(0.0, rgba(accent, 115))
            sweep.setColorAt(0.12, rgba(accent, 0))
            sweep.setColorAt(1.0, rgba(accent, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(sweep)
            p.drawEllipse(center, R - 2, R - 2)
            p.setBrush(Qt.BrushStyle.NoBrush)

        # Dial ticks (72 ticks, every 6th long)
        p.save()
        p.translate(center)
        p.rotate(-self.spin * 0.35)
        for k in range(72):
            long_tick = k % 6 == 0
            p.setPen(QPen(rgba(accent, 215 if long_tick else 90), 2 if long_tick else 1))
            r0 = 102 if long_tick else 106
            a = math.radians(k * 5)
            p.drawLine(QPointF(math.cos(a) * r0, math.sin(a) * r0), QPointF(math.cos(a) * 112, math.sin(a) * 112))
        p.restore()

        # Counter-rotating segmented arcs
        for i, (rad, width, segs, span, direction) in enumerate(((92, 2.8, 3, 70, 1), (80, 1.5, 5, 38, -1))):
            rect = QRectF(cx - rad, cy - rad, 2 * rad, 2 * rad)
            p.setPen(QPen(rgba(accent, 230 - i * 70), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            start = self.spin * (1 + i * 0.6) * direction
            for s in range(segs):
                a0 = start + s * 360 / segs
                p.drawArc(rect, int(-a0 * 16), int(-span * 16))

        # Dynamic Voice ring
        base_r = 62
        if st == "LISTENING":
            # Dynamic sound frequency equalizer bars reacting directly to user speech
            bars = 48
            for k in range(bars):
                a = math.radians(k * 360 / bars + self.spin * 0.3)
                w_idx = int(k * len(self.wave) / bars) % len(self.wave)
                w_val = self.wave[w_idx]
                freq = 0.5 + 0.5 * math.sin(t * 11 + k * 1.2) * math.sin(t * 5.1 + k * 0.7)
                length = 3 + (26 * lvl + 20 * w_val) * freq
                alpha = int(140 + 115 * min(1.0, lvl * 2.0))
                p.setPen(QPen(rgba(accent, alpha), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                p.drawLine(
                    QPointF(cx + math.cos(a) * base_r, cy + math.sin(a) * base_r),
                    QPointF(cx + math.cos(a) * (base_r + length), cy + math.sin(a) * (base_r + length)),
                )
            p.setPen(QPen(rgba(accent, 220), 1.5))
            p.drawEllipse(center, base_r - 2, base_r - 2)
        elif st == "SPEAKING":
            bars = 48
            for k in range(bars):
                a = math.radians(k * 360 / bars + self.spin * 0.4)
                wob = 0.5 + 0.5 * math.sin(t * 9 + k * 0.9) * math.sin(t * 4.3 + k * 0.37)
                length = 4 + 22 * lvl * wob
                p.setPen(QPen(rgba(accent, 220), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                p.drawLine(
                    QPointF(cx + math.cos(a) * base_r, cy + math.sin(a) * base_r),
                    QPointF(cx + math.cos(a) * (base_r + length), cy + math.sin(a) * (base_r + length)),
                )
        elif st == "EXECUTING":
            rect = QRectF(cx - base_r, cy - base_r, 2 * base_r, 2 * base_r)
            p.setPen(QPen(rgba(accent, 240), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(rect, int(-self.spin * 3 * 16), int(-90 * 16))
            p.setPen(QPen(rgba(accent, 120), 1.2))
            p.drawEllipse(center, base_r, base_r)
        else:
            p.setPen(QPen(rgba(accent, 130), 1.2))
            p.drawEllipse(center, base_r, base_r)

        # Arc-reactor coil (10 segments)
        p.save()
        p.translate(center)
        p.rotate(self.spin * 0.6)
        coil = QRectF(-46, -46, 92, 92)
        p.setPen(QPen(rgba(accent, 150 + 100 * lvl), 6.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        for k in range(10):
            p.drawArc(coil, int(k * 36 * 16), int(25 * 16))
        p.restore()
        p.setPen(QPen(rgba(accent, 160), 1))
        p.drawEllipse(center, 38, 38)

        # Glowing core: pure white center fading to state color
        if st in ("THINKING", "PROCESSING"):
            core_r = 28 + 6 * pulse + 1.5 * math.sin(t * 3.5)
            cg = QRadialGradient(center, max(core_r, 1))
            cg.setColorAt(0.0, QColor(255, 255, 255, 255))
            cg.setColorAt(0.35, COLOR_BLUE_HIGHLIGHT)  # #BAE6FD Strong highlight
            cg.setColorAt(0.75, COLOR_BLUE_PRIMARY)  # #38BDF8 Primary light blue
            cg.setColorAt(1.0, QColor(56, 189, 248, 35))
        else:
            core_r = 28 + 6 * lvl + 1.5 * math.sin(t * 2.2)
            cg = QRadialGradient(center, max(core_r, 1))
            cg.setColorAt(0.0, QColor(255, 255, 255, 255))
            cg.setColorAt(0.35, core_mid)
            cg.setColorAt(0.75, rgba(accent, 225))
            cg.setColorAt(1.0, rgba(accent, 40))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cg)
        p.drawEllipse(center, core_r, core_r)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Status label under reactor
        label = st
        p.setFont(keena_font(9, True, 4))
        p.setPen(rgba(accent, 235))
        p.drawText(QRectF(0, cy + 138, w_d, 18), Qt.AlignmentFlag.AlignCenter, label)
        dot_x = cx - p.fontMetrics().horizontalAdvance(label) / 2 - 14
        on = not (st in ("THINKING", "PROCESSING", "EXECUTING") and int(t * 3) % 2)
        p.setPen(QPen(rgba(accent, 230), 1.2))
        p.setBrush(rgba(accent, 230) if on else Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(dot_x, cy + 147), 3.5, 3.5)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Action Cards
        y = cy + 150
        now = time.time()
        live = [(text, born) for text, born in self.cards if now - born <= 7]
        p.setFont(keena_font(8, True, 0.8))
        fm = p.fontMetrics()
        max_w = max(140.0, w_d - 24)
        for text, born in live:
            age = now - born
            fade = max(0.0, min(1.0, 7 - age)) * min(1.0, age * 5)
            p.setOpacity(fade)
            upper_txt = text.upper()
            label = f"[ ✓ {upper_txt} ]"
            if fm.horizontalAdvance(label) > max_w:
                bracket_w = fm.horizontalAdvance("[ ✓  ]")
                inner = fm.elidedText(upper_txt, Qt.TextElideMode.ElideRight, max(40, int(max_w - bracket_w)))
                label = f"[ ✓ {inner} ]"
            p.setPen(rgba(accent, 230))
            p.drawText(QRectF(8, y, max(40, w_d - 16), 16), Qt.AlignmentFlag.AlignCenter, label)
            y += 18
        p.setOpacity(1.0)

        p.restore()
        p.end()

    def _paint_big(self, p: QPainter, accent: QColor, boot: float, t: float):
        w, h = self.width(), self.height()
        TEXT_COL = qcol(C.TEXT)
        DIM_COL = qcol(C.TEXT_DIM)

        p.fillRect(QRectF(0, 0, w, h), QColor(10, 3, 10, 245))
        p.setPen(QPen(rgba(accent, 12), 1))
        for x in range(0, w, 64):
            p.drawLine(QPointF(x, 0), QPointF(x, h))
        for y in range(0, h, 64):
            p.drawLine(QPointF(0, y), QPointF(w, y))

        rx, ry = w / 2, h * 0.43
        s = h * 0.30 / 125
        vg = QRadialGradient(QPointF(rx, ry), h * 0.6)
        vg.setColorAt(0.0, rgba(accent, 30))
        vg.setColorAt(1.0, rgba(accent, 0))
        p.fillRect(QRectF(0, 0, w, h), vg)

        m, L = 28, 70
        p.setPen(QPen(rgba(accent, 200 * boot), 2.5))
        for x, y, dx, dy in ((m, m, 1, 1), (w - m, m, -1, 1), (m, h - m, 1, -1), (w - m, h - m, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx * L, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * L))

        title = ".".join(self._assistant_name.upper())
        p.setPen(rgba(TEXT_COL, 235))
        p.setFont(keena_font(26, True, 18))
        p.drawText(QRectF(0, h * 0.04, w, 44), Qt.AlignmentFlag.AlignCenter, title)
        p.setPen(rgba(DIM_COL, 200 * boot))
        p.setFont(keena_font(10, False, 5))
        p.drawText(QRectF(0, h * 0.04 + 46, w, 20), Qt.AlignmentFlag.AlignCenter, "YOUR PERSONAL AI COMPANION")

        p.setPen(rgba(TEXT_COL, 230 * boot))
        p.setFont(keena_font(32, True, 2))
        p.drawText(QRectF(m + 26, m + 18, 400, 48), Qt.AlignmentFlag.AlignLeft, time.strftime("%I:%M:%S %p"))
        p.setPen(rgba(DIM_COL, 220 * boot))
        p.setFont(keena_font(12, False, 4))
        p.drawText(QRectF(m + 28, m + 68, 400, 22), Qt.AlignmentFlag.AlignLeft, time.strftime("%A %d %B %Y").upper())

        rows = []
        if self.cpu is not None:
            rows += [("CPU", f"{self.cpu:.0f}%", self.cpu), ("MEMORY", f"{self.mem:.0f}%", self.mem)]
        rows.append(("STATUS", self.stat, 0))
        p.setFont(keena_font(12, True, 3))
        for i, (name, val, pct) in enumerate(rows):
            y_pos = m + 18 + i * 40
            p.setPen(rgba(qcol(C.MUTED_C) if pct >= 90 else DIM_COL, 220 * boot))
            p.drawText(QRectF(w - m - 330, y_pos, 300, 22), Qt.AlignmentFlag.AlignRight, f"{name}  {val}")

        p.setPen(rgba(DIM_COL, 150 * boot))
        p.setFont(keena_font(9, False, 3))
        p.drawText(
            QRectF(0, h - m - 22, w - m - 26, 18), Qt.AlignmentFlag.AlignRight, "ESC OR CLICK — RETURN TO DESKTOP HUD"
        )

        p.save()
        p.translate(rx, ry)
        p.scale(s, s)
        p.translate(-w / 2, -185)

        # Draw scaled reactor
        center = QPointF(w / 2, 185)
        lvl = getattr(self, "level", 0.0)
        R = 125
        p.setPen(QPen(rgba(accent, 115), 1.2))
        p.drawEllipse(center, R, R)
        p.setBrush(rgba(accent, 210))
        p.setPen(Qt.PenStyle.NoPen)
        for k in range(4):
            a = math.radians(k * 90 + 45)
            tip = QPointF(center.x() + math.cos(a) * (R - 7), center.y() + math.sin(a) * (R - 7))
            side = math.radians(k * 90 + 45 + 90)
            base = QPointF(center.x() + math.cos(a) * (R + 2), center.y() + math.sin(a) * (R + 2))
            p.drawPolygon(
                QPolygonF(
                    [
                        tip,
                        base + QPointF(math.cos(side) * 5, math.sin(side) * 5),
                        base - QPointF(math.cos(side) * 5, math.sin(side) * 5),
                    ]
                )
            )
        p.setBrush(Qt.BrushStyle.NoBrush)

        p.save()
        p.translate(center)
        p.rotate(-self.spin * 0.35)
        for k in range(72):
            long_tick = k % 6 == 0
            p.setPen(QPen(rgba(accent, 215 if long_tick else 90), 2 if long_tick else 1))
            r0 = 102 if long_tick else 106
            a = math.radians(k * 5)
            p.drawLine(QPointF(math.cos(a) * r0, math.sin(a) * r0), QPointF(math.cos(a) * 112, math.sin(a) * 112))
        p.restore()

        core_r = 28 + 6 * lvl + 1.5 * math.sin(t * 2.2)
        cg = QRadialGradient(center, max(core_r, 1))
        cg.setColorAt(0.0, QColor(255, 255, 255, 255))
        cg.setColorAt(0.35, QColor(255, 214, 236, 240))
        cg.setColorAt(0.75, rgba(accent, 225))
        cg.setColorAt(1.0, rgba(accent, 40))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cg)
        p.drawEllipse(center, core_r, core_r)
        p.setBrush(Qt.BrushStyle.NoBrush)

        p.restore()

        # Big screen conversation preview
        tw = min(1100, int(w * 0.65))
        tx = (w - tw) // 2
        ty = int(ry + 140 * s + 10)
        if self.user_text:
            p.setPen(rgba(DIM_COL, 220))
            p.setFont(keena_font(13))
            p.drawText(
                QRectF(tx, ty, tw, 48), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.user_text[-160:]
            )
            ty += 56
        if self.reply_text:
            p.setPen(TEXT_COL)
            p.setFont(keena_font(18, True))
            shown = self.reply_text[: int(self.reply_shown)]
            cursor = "█" if int(self.reply_shown) < len(self.reply_text) else ""
            p.drawText(
                QRectF(tx, ty, tw, max(60, int(h - ty - 60))),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                shown[-220:] + cursor,
            )

    def _paint_mini_circle(self, p: QPainter, accent: QColor, core_mid: QColor, glow_col: QColor, t: float):
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        now = time.time()
        st = (self.state or "STANDBY").upper()
        if self.muted:
            st = "MUTED"

        t_state = now - getattr(self, "_state_start_t", now)
        lvl = getattr(self, "level", 0.0)

        # State-based pulse calculation
        if st in ("THINKING", "PROCESSING"):
            pulse_speed = 5.0 if st == "THINKING" else 5.5
            pulse = 0.5 + 0.5 * math.sin(t_state * pulse_speed)
        elif st == "LISTENING":
            pulse_speed = 2.8
            pulse = 0.5 + 0.5 * math.sin(t_state * pulse_speed)
        elif self.speaking or st == "SPEAKING":
            pulse_speed = 4.5
            pulse = 0.5 + 0.5 * math.sin(t * pulse_speed)
        else:
            pulse = 0.5 + 0.5 * math.sin(t_state * 2.0)

        if getattr(self, "_reduced_motion", False):
            pulse = 0.5

        # Solid non-transparent deep black background disc with state-pulsing border
        p.save()
        disc_r = min(w, h) / 2.0 - 1.0
        border_alpha = int(195 + 60 * pulse) if st != "SPEAKING" else 240
        p.setPen(QPen(rgba(accent, border_alpha), 2.2))
        p.setBrush(QColor(0, 0, 0, 255))
        p.drawEllipse(QPointF(cx, cy), disc_r, disc_r)
        p.restore()

        R_target = min(w, h) / 2.0 - 2.0
        scale = max(0.01, R_target / 125.0)

        p.save()
        p.translate(cx, cy)
        p.scale(scale, scale)

        center = QPointF(0, 0)

        # Inner solid black disc to protect animation contrast
        p.save()
        p.setPen(QPen(rgba(accent, int(180 + 55 * pulse)), 2.5))
        p.setBrush(QColor(0, 0, 0, 255))
        p.drawEllipse(center, 126, 126)
        p.restore()

        # Radial glow based on state
        if st in ("THINKING", "PROCESSING"):
            # Bright glowing light blue continuous pulsing animation
            glow_r = 135 + 40 * pulse
            g = QRadialGradient(center, max(glow_r, 1))
            g.setColorAt(0.0, QColor(125, 211, 252, int(160 + 75 * pulse)))  # #7DD3FC bright glow
            g.setColorAt(0.4, QColor(56, 189, 248, int(100 + 66 * pulse)))  # rgba(56, 189, 248, 0.65) outer glow
            g.setColorAt(1.0, QColor(56, 189, 248, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(center, glow_r, glow_r)
            p.setBrush(Qt.BrushStyle.NoBrush)

            # Conical radar sweep in light blue
            sweep = QConicalGradient(center, -self.spin * 1.5)
            sweep.setColorAt(0.0, QColor(125, 211, 252, int(130 + 70 * pulse)))
            sweep.setColorAt(0.12, QColor(56, 189, 248, 0))
            sweep.setColorAt(1.0, QColor(56, 189, 248, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(sweep)
            p.drawEllipse(center, 125, 125)
            p.setBrush(Qt.BrushStyle.NoBrush)

        elif st == "LISTENING":
            # Gentle red blinking / pulsing effect: periodic increase and decrease
            glow_r = 130 + 35 * pulse + 45 * lvl
            g = QRadialGradient(center, max(glow_r, 1))
            g.setColorAt(0.0, rgba(glow_col, int(115 + 85 * pulse + 40 * lvl)))
            g.setColorAt(0.5, rgba(glow_col, int(30 + 40 * pulse + 20 * lvl)))
            g.setColorAt(1.0, rgba(glow_col, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(center, glow_r, glow_r)
            p.setBrush(Qt.BrushStyle.NoBrush)

        elif self.speaking or st == "SPEAKING":
            # Speaking yellow glow surging with audio
            glow_r = 135 + 75 * lvl
            g = QRadialGradient(center, max(glow_r, 1))
            g.setColorAt(0.0, rgba(glow_col, 130 + int(35 * lvl)))
            g.setColorAt(0.5, rgba(glow_col, 35 + int(25 * lvl)))
            g.setColorAt(1.0, rgba(glow_col, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(center, glow_r, glow_r)
            p.setBrush(Qt.BrushStyle.NoBrush)

        else:
            # Standby / Default subtle glow
            glow_r = 130 + 20 * pulse
            g = QRadialGradient(center, max(glow_r, 1))
            g.setColorAt(0.0, rgba(glow_col, int(90 + 40 * pulse)))
            g.setColorAt(0.5, rgba(glow_col, int(25 + 20 * pulse)))
            g.setColorAt(1.0, rgba(glow_col, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(center, glow_r, glow_r)
            p.setBrush(Qt.BrushStyle.NoBrush)

        # Outer ring
        R = 125
        p.setPen(QPen(rgba(accent, int(190 + 55 * pulse)), 2.5))
        p.drawEllipse(center, R, R)

        # 4 Notch markers
        p.setBrush(rgba(accent, 240))
        for k in range(4):
            a = math.radians(k * 90 + 45)
            p.drawPolygon(
                QPolygonF(
                    [
                        QPointF(math.cos(a) * (R - 5), math.sin(a) * (R - 5)),
                        QPointF(math.cos(a + 0.08) * (R + 6), math.sin(a + 0.08) * (R + 6)),
                        QPointF(math.cos(a - 0.08) * (R + 6), math.sin(a - 0.08) * (R + 6)),
                    ]
                )
            )
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Dial ticks
        p.save()
        p.rotate(-self.spin * 0.35)
        for k in range(36):
            long_tick = k % 6 == 0
            p.setPen(QPen(rgba(accent, 220 if long_tick else 100), 2.5 if long_tick else 1.5))
            r0 = (96 - 12 * lvl) if long_tick else (104 - 6 * lvl)
            a = math.radians(k * 10)
            p.drawLine(QPointF(math.cos(a) * r0, math.sin(a) * r0), QPointF(math.cos(a) * 114, math.sin(a) * 114))
        p.restore()

        # Counter-rotating segmented arcs
        for i, (rad, width, segs, span, direction) in enumerate(((92, 3.5, 3, 70, 1), (80, 2.5, 5, 38, -1))):
            rect = QRectF(-rad, -rad, 2 * rad, 2 * rad)
            p.setPen(QPen(rgba(accent, 240 - i * 60), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            start = self.spin * (1 + i * 0.6) * direction
            for s in range(segs):
                a0 = start + s * 360 / segs
                p.drawArc(rect, int(-a0 * 16), int(-span * 16))

        # Dynamic Voice ring / speech bars / thinking pulse
        base_r = 62
        if st == "SPEAKING" or self.speaking:
            bars = 24
            for k in range(bars):
                a = math.radians(k * 360 / bars + self.spin * 0.4)
                wob = 0.5 + 0.5 * math.sin(t * 9 + k * 0.9)
                length = 5 + 46 * lvl * wob
                p.setPen(
                    QPen(
                        rgba(accent, 220 + int(35 * lvl)),
                        2.8 + 1.2 * lvl,
                        Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.FlatCap,
                    )
                )
                p.drawLine(
                    QPointF(math.cos(a) * base_r, math.sin(a) * base_r),
                    QPointF(math.cos(a) * (base_r + length), math.sin(a) * (base_r + length)),
                )
        elif st == "LISTENING":
            path = QPainterPath()
            n = len(self.wave)
            for k in range(n + 1):
                v = sum(self.wave[(k + d) % n] for d in (-2, -1, 0, 1, 2)) / 5
                a = math.radians(k * 360 / n + self.spin)
                r_wave = base_r + 2 + (40 * lvl + 22) * v
                pt = QPointF(math.cos(a) * r_wave, math.sin(a) * r_wave)
                path.moveTo(pt) if k == 0 else path.lineTo(pt)
            p.setPen(QPen(rgba(accent, 210 + int(45 * lvl)), 2.2 + 1.2 * lvl))
            p.drawPath(path)
        elif st in ("THINKING", "PROCESSING"):
            p.setPen(QPen(rgba(accent, int(190 + 65 * pulse)), 2.4))
            p.drawEllipse(center, base_r, base_r)
        else:
            p.setPen(QPen(rgba(accent, 140), 1.5))
            p.drawEllipse(center, base_r, base_r)

        # Arc-reactor coil (10 segments)
        p.save()
        p.rotate(self.spin * 0.6)
        coil = QRectF(-46, -46, 92, 92)
        coil_alpha = int(170 + 75 * pulse) if st in ("THINKING", "PROCESSING", "LISTENING") else (150 + int(105 * lvl))
        p.setPen(QPen(rgba(accent, coil_alpha), 8.0 + 3.5 * lvl, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        for k in range(10):
            p.drawArc(coil, int(k * 36 * 16), int(25 * 16))
        p.restore()
        p.setPen(QPen(rgba(accent, 180), 1.8))
        p.drawEllipse(center, 38, 38)

        # Glowing core: pure white center fading to state color
        if st in ("THINKING", "PROCESSING"):
            core_r = 26 + 8 * pulse + 1.5 * math.sin(t * 3.5)
            cg = QRadialGradient(center, max(core_r, 1))
            cg.setColorAt(0.0, QColor(255, 255, 255, 255))
            cg.setColorAt(0.35, COLOR_BLUE_HIGHLIGHT)  # #BAE6FD
            cg.setColorAt(0.75, COLOR_BLUE_PRIMARY)  # #38BDF8
            cg.setColorAt(1.0, QColor(56, 189, 248, 40))
        elif st == "LISTENING":
            core_r = 24 + 6 * pulse + 24 * lvl + 2.0 * math.sin(t * 3.0)
            cg = QRadialGradient(center, max(core_r, 1))
            cg.setColorAt(0.0, QColor(255, 255, 255, 255))
            cg.setColorAt(0.35, core_mid)
            cg.setColorAt(0.75, rgba(accent, 235))
            cg.setColorAt(1.0, rgba(accent, 40))
        else:
            core_r = 26 + 26 * lvl + 2.0 * math.sin(t * 3.0)
            cg = QRadialGradient(center, max(core_r, 1))
            cg.setColorAt(0.0, QColor(255, 255, 255, 255))
            cg.setColorAt(0.35, core_mid)
            cg.setColorAt(0.75, rgba(accent, 235))
            cg.setColorAt(1.0, rgba(accent, 40))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(cg)
        p.drawEllipse(center, core_r, core_r)
        p.setBrush(Qt.BrushStyle.NoBrush)

        p.restore()
