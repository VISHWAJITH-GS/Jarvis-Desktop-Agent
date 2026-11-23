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

from gui.overlays import _HudOverlay


class ClipboardPanel(QWidget):
    """Floating panel shown when text is copied — offers quick Jarvis actions."""

    action_requested = pyqtSignal(str)
    _W, _H = 340, 114

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ClipboardPanel {{
                background: rgba(14, 4, 18, 248);
                border: 1px solid {C.PRI};
                border-radius: 8px;
            }}
        """)
        self.setFixedWidth(self._W)
        self._clip_text = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        icon_lbl = QLabel("◈  CLIPBOARD DETECTED")
        icon_lbl.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(icon_lbl)
        hdr.addStretch()
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(16, 16)
        x_btn.setFont(QFont("Montserrat", 8))
        x_btn.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        self._preview = QLabel()
        self._preview.setFont(QFont("Montserrat", 8))
        self._preview.setStyleSheet(f"""
            color: {C.TEXT}; background: {C.PANEL2};
            border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
        """)
        self._preview.setWordWrap(False)
        self._preview.setFixedHeight(28)
        lay.addWidget(self._preview)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        _bs = (
            f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
            f"border: 1px solid {C.BORDER}; border-radius: 2px; }}"
            f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}"
        )
        for label, cmd_fmt in [
            ("TRANSLATE", "Translate this text to English: {text}"),
            ("SUMMARISE", "Summarise this: {text}"),
            ("EXPLAIN", "Explain this: {text}"),
            ("FIX", "Fix grammar and spelling: {text}"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_bs)
            b.clicked.connect(lambda _, c=cmd_fmt: self._trigger(c))
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.hide)
        self.hide()

    def _trigger(self, cmd_fmt: str):
        if self._clip_text:
            self.action_requested.emit(cmd_fmt.format(text=self._clip_text[:800]))
        self.hide()

    def show_clipboard(self, text: str):
        self._clip_text = text
        preview = text[:58].replace("\n", " ")
        if len(text) > 58:
            preview += "…"
        self._preview.setText(f'"{preview}"')
        self.show()
        self.raise_()
        self.activateWindow()
        self._dismiss_timer.start(8000)


class PluginSettingsOverlay(QWidget):
    """Floating overlay — renders per-plugin settings forms.

    Fully generic: it iterates the settings schemas a plugin declared via its
    PLUGIN_SETTINGS constant (delivered by PluginRegistry.settings_schemas) and
    builds a form for each. It knows NOTHING about any specific plugin, so the
    core stays clean and plugins remain pure drop-in — install a plugin that
    declares fields (e.g. the 3D-printer suite) and its section appears here;
    install none and this panel simply says there's nothing to configure.
    """

    _test_done = pyqtSignal(str, bool, str)  # namespace, ok, message
    _OW = 460

    def __init__(self, sections: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginSettingsOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self._sections = sections or []
        self._widgets: dict[tuple, object] = {}  # (namespace, key) -> input widget
        self._types: dict[tuple, str] = {}  # (namespace, key) -> field type
        self._status_labels: dict[str, QLabel] = {}  # namespace -> status QLabel
        self._test_done.connect(self._on_test_done)

        self._fs = (
            f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
            f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(8)

        root.addWidget(self._lbl("⚙  PLUGIN SETTINGS", 12, True))
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        root.addWidget(sep)

        if not self._sections:
            root.addWidget(
                self._lbl(
                    "No configurable plugins are installed.\nDrop a plugin that needs "
                    "settings (like the 3D-printer suite) into the plugins folder and "
                    "it will show up here.",
                    9,
                    color=C.TEXT_DIM,
                )
            )
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setStyleSheet("QScrollArea { background: transparent; }")
            inner = QWidget()
            inner.setStyleSheet("background: transparent;")
            form = QVBoxLayout(inner)
            form.setContentsMargins(0, 0, 6, 0)
            form.setSpacing(6)
            for sec in self._sections:
                self._build_section(form, sec)
            form.addStretch(1)
            scroll.setWidget(inner)
            root.addWidget(scroll, 1)

        # ── bottom buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        if self._sections:
            save_btn = QPushButton("▸  SAVE")
            save_btn.setFixedHeight(34)
            save_btn.setFont(QFont("Montserrat", 9, QFont.Weight.Bold))
            save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            save_btn.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
            """)
            save_btn.clicked.connect(self._save_all)
            btn_row.addWidget(save_btn)

        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(34)
        close_btn.setFont(QFont("Montserrat", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _lbl(self, txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignLeft):
        w = QLabel(txt)
        w.setAlignment(align)
        w.setWordWrap(True)
        w.setFont(QFont("Montserrat", fs, QFont.Weight.Bold if bold else QFont.Weight.Normal))
        w.setStyleSheet(f"color: {color}; background: transparent;")
        return w

    def _build_section(self, form: QVBoxLayout, sec: dict):
        ns = sec.get("namespace") or sec.get("plugin") or "plugin"
        title = sec.get("title") or ns
        fields = sec.get("fields") or []
        values = sec.get("values") or {}

        form.addSpacing(4)
        form.addWidget(self._lbl(title, 10, True, C.PRI))

        for field in fields:
            if not isinstance(field, dict) or not field.get("key"):
                continue
            key = field["key"]
            ftype = (field.get("type") or "text").lower()
            label = field.get("label") or key
            default = field.get("default")
            stored = values.get(key, default)

            form.addWidget(self._lbl(label.upper(), 8, color=C.TEXT_DIM))

            if ftype == "choice":
                w = QComboBox()
                w.addItems([str(o) for o in field.get("options", [])])
                w.setFont(QFont("Montserrat", 9))
                w.setFixedHeight(30)
                w.setStyleSheet(
                    f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px; }}"
                    f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
                    f"selection-background-color: {C.PRI_GHO}; }}"
                )
                if stored is not None:
                    w.setCurrentText(str(stored))
            elif ftype == "toggle":
                w = QPushButton()
                w.setCheckable(True)
                w.setChecked(bool(stored))
                w.setFixedHeight(28)
                w.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
                w.setCursor(Qt.CursorShape.PointingHandCursor)
                self._style_toggle(w)
                w.toggled.connect(lambda _=False, b=w: self._style_toggle(b))
            else:  # text / password
                w = QLineEdit("" if stored is None else str(stored))
                w.setFont(QFont("Montserrat", 10))
                w.setFixedHeight(30)
                w.setStyleSheet(self._fs)
                if field.get("placeholder"):
                    w.setPlaceholderText(str(field["placeholder"]))
                if ftype == "password":
                    w.setEchoMode(QLineEdit.EchoMode.Password)

            self._widgets[(ns, key)] = w
            self._types[(ns, key)] = ftype
            form.addWidget(w)

        # optional test/connect action button + status line
        action = sec.get("action")
        if isinstance(action, dict) and callable(action.get("run")):
            form.addSpacing(2)
            ab = QPushButton(str(action.get("label") or "TEST"))
            ab.setFixedHeight(30)
            ab.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
            ab.setCursor(Qt.CursorShape.PointingHandCursor)
            ab.setStyleSheet(f"""
                QPushButton {{ background: #00091a; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
            """)
            ab.clicked.connect(lambda _=False, n=ns: self._run_action(n))
            form.addWidget(ab)

        status = self._lbl("", 8, color=C.TEXT_DIM)
        self._status_labels[ns] = status
        form.addWidget(status)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {C.BORDER}; margin: 4px 0;")
        form.addWidget(line)

    def _style_toggle(self, btn: QPushButton):
        on = btn.isChecked()
        btn.setText("ON" if on else "OFF")
        if on:
            btn.setStyleSheet(
                f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; "
                f"border: 1px solid {C.PRI}; border-radius: 3px; }}"
            )
        else:
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; "
                f"border: 1px solid {C.BORDER}; border-radius: 3px; }}"
            )

    # ── data ──────────────────────────────────────────────────────────────────
    def _gather(self, ns: str) -> dict:
        out = {}
        for (n, key), w in self._widgets.items():
            if n != ns:
                continue
            t = self._types.get((n, key), "text")
            if t == "choice":
                out[key] = w.currentText()
            elif t == "toggle":
                out[key] = w.isChecked()
            else:
                out[key] = w.text().strip()
        return out

    def _save_ns(self, ns: str):
        from memory.config_manager import save_plugin_config

        save_plugin_config(ns, self._gather(ns))

    def _save_all(self):
        for sec in self._sections:
            ns = sec.get("namespace") or sec.get("plugin")
            if ns:
                self._save_ns(ns)
                lbl = self._status_labels.get(ns)
                if lbl:
                    lbl.setText("Saved ✓")
                    lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")

    def _run_action(self, ns: str):
        sec = next((s for s in self._sections if (s.get("namespace") or s.get("plugin")) == ns), None)
        if not sec:
            return
        run_fn = (sec.get("action") or {}).get("run")
        if not callable(run_fn):
            return
        self._save_ns(ns)  # persist what the user typed before testing
        values = self._gather(ns)
        lbl = self._status_labels.get(ns)
        if lbl:
            lbl.setText("Testing…")
            lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

        def worker():
            try:
                res = run_fn(values)
                if isinstance(res, tuple) and len(res) == 2:
                    ok, msg = bool(res[0]), str(res[1])
                else:
                    ok, msg = bool(res), str(res)
            except Exception as e:
                ok, msg = False, str(e)
            self._test_done.emit(ns, ok, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, ns: str, ok: bool, msg: str):
        lbl = self._status_labels.get(ns)
        if not lbl:
            return
        lbl.setText(msg)
        color = C.PRI if ok else "#ff6b6b"
        lbl.setStyleSheet(f"color: {color}; background: transparent;")


class RemoteKeyOverlay(QWidget):
    """Floating overlay — QR code for instant phone pairing + manual key fallback."""

    closed = pyqtSignal()

    _OW, _OH = 400, 465

    def __init__(
        self,
        url: str,
        key: str,
        auto_login_url: str = "",
        manual_url: str = "",
        expiry_secs: int = 600,
        parent=None,
        bind_error: str = "",
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(0, 4, 12, 0.95);
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        self._expiry = time.time() + expiry_secs
        self._on_new_key = None
        self._auto_login_url = auto_login_url
        self._manual_url = manual_url or url

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(5)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Montserrat", fs, QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("◈  REMOTE ACCESS", 12, True))
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── QR code ───────────────────────────────────────────────────────────
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(176, 176)
        self._qr_label.setStyleSheet("background: white; border-radius: 10px; padding: 4px;")
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._update_qr(auto_login_url)

        lay.addWidget(_lbl("Scan with phone camera to connect instantly", 8, color=C.TEXT_DIM))

        # THE ONE FAILURE THAT LOOKS LIKE THE SERVER BEING OFF.
        #
        # Windows writes a BLOCK rule for an executable when its "Allow access?"
        # prompt is dismissed, and a block overrides every allow rule there is —
        # including the port rule this app carefully creates. The phone then
        # reports "refused to connect" while the server is running perfectly.
        #
        # It is invisible from the HUD, which is where the person scanning the
        # QR code is looking, and it is per-EXECUTABLE: the shortcut runs
        # pythonw.exe while an editor runs python.exe, so it can block one launch
        # method and not the other on the same machine. That difference is
        # unguessable without being told, so it is said here.
        # Whatever is stopping this from working, say it HERE — beside the QR
        # code, where the person about to scan it is looking. A dashboard that
        # cannot be reached and does not explain why is the single most
        # frustrating failure this app has.
        if bind_error:
            lay.addWidget(_lbl("⚠  " + bind_error, 8, color="#f4b942"))

        try:
            from dashboard.server import blocking_firewall_rules

            _blocked = blocking_firewall_rules()
        except Exception:
            _blocked = []
        if _blocked:
            warn = _lbl(
                "⚠  Windows is blocking incoming connections to this copy of "
                "Python, so your phone cannot reach this page. Restart JARVIS "
                "and accept the Windows prompt, or remove the rules named "
                + ", ".join(sorted({n for n, _ in _blocked}))
                + " under Windows Defender Firewall → Inbound Rules.",
                8,
                color="#f4b942",
            )
            lay.addWidget(warn)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Or enter manually:", 7, color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(QFont("Montserrat", 8))
        self._url_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(QFont("Montserrat", 28, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.ACC};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_B};
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 10px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(QFont("Montserrat", 8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(32)
        new_btn.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(32)
        close_btn.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self._ctimer.start(1000)
        self._tick()

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("—")
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO

            qr = _qrmod.QRCode(
                box_size=5,
                border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(170, 170, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(QFont("Montserrat", 8))
            self._qr_label.setStyleSheet("color: #888; background: white; border-radius: 10px; padding: 4px;")
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont("Montserrat", 7))
            self._qr_label.setStyleSheet(f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;")

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        """Call from any thread when a phone successfully connects."""
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont("Montserrat", 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet("color: #00ff88; background: #001a0d; border-radius: 10px;")
        self._timer_lbl.setText("Phone connected — JARVIS ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url = result[0]
                key = result[1]
                auto = result[2] if len(result) >= 3 else ""
                manual = result[3] if len(result) >= 4 else url
                self._manual_url = manual or url
                self._url_lbl.setText(self._manual_url)
                self._key_lbl.setText(key)
                self._auto_login_url = auto
                self._update_qr(auto or url)
                self._expiry = time.time() + 600
                self._key_lbl.setStyleSheet(f"""
                    color: {C.ACC};
                    background: {C.PANEL2};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 8px;
                    padding: 6px 4px;
                    letter-spacing: 10px;
                """)
                self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
                self._ctimer.start(1000)
                self._tick()

    def _do_close(self):
        self._ctimer.stop()
        self.hide()
        self.closed.emit()


class ConfirmationTiersOverlay(QWidget):
    """Floating overlay — configure action confirmation risk tiers (LOW, MEDIUM, HIGH)."""

    _OW = 540

    CONFIGURABLE_ITEMS = [
        (
            "COMMUNICATION & MESSAGING",
            [
                ("whatsapp_send", "WhatsApp Send Message", "Sending messages via WhatsApp Web", "MEDIUM", False),
                ("send_message", "General Messaging", "Sending messages via Telegram, SMS, or Email", "MEDIUM", False),
                ("browser_control:chatgpt", "Web AI Chatbots", "Typing/submitting prompts to ChatGPT or web LLMs", "MEDIUM", False),
                ("browser_control:whatsapp", "WhatsApp Web Composer", "Submitting via web WhatsApp composer directly", "MEDIUM", False),
            ],
        ),
        (
            "WINDOW & APPLICATION MANAGEMENT",
            [
                ("close_app", "Close External Application", "Terminating external running application processes", "MEDIUM", False),
                ("close_display", "Close Jarvis Display", "Closing an active Jarvis display window", "MEDIUM", False),
                ("close_all_displays", "Close All Displays", "Closing all open Jarvis extra display windows", "MEDIUM", False),
                ("shutdown_jarvis", "Shutdown Jarvis", "Orderly exit of Jarvis assistant background session", "MEDIUM", False),
                ("jarvis_control:mute", "Mute Audio / Microphone", "Muting speaker or microphone", "LOW", False),
                ("jarvis_control:sleep", "Sleep Standby", "Putting Jarvis into standby (wake word only)", "LOW", False),
            ],
        ),
        (
            "FILES & SYSTEM STORAGE",
            [
                ("create_file", "Create / Write Files", "Writing new files or updating code on disk", "MEDIUM", False),
                ("download_file", "Download Files", "Downloading files from the web", "MEDIUM", False),
                ("delete_file", "Delete Files & Folders", "Permanently deleting files, folders or emptying trash", "HIGH", True),
            ],
        ),
        (
            "CRITICAL / SYSTEM SECURITY",
            [
                ("shutdown_pc", "Shut Down / Reboot PC", "Powering down, rebooting, or locking the operating system", "HIGH", True),
                ("make_payment", "Payments & Purchases", "Online checkout, transactions, or orders", "HIGH", True),
                ("shell_exec", "Execute Shell / Code", "Running terminal commands or scripts", "HIGH", True),
                ("accept_all", "IDE Accept / Reject All", "Accepting or discarding all code edits in workspace", "HIGH", True),
            ],
        ),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ConfirmationTiersOverlay {{
                background: rgba(0, 6, 10, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self._combos: dict[str, QComboBox] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(8)

        # Header
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        title_lbl = QLabel("◈  ACTION CONFIRMATION TIERS")
        title_lbl.setFont(QFont("Montserrat", 11, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title_lbl)
        hdr.addStretch()
        close_x = QPushButton("✕")
        close_x.setFixedSize(20, 20)
        close_x.setFont(QFont("Montserrat", 9))
        close_x.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        close_x.setCursor(Qt.CursorShape.PointingHandCursor)
        close_x.clicked.connect(self.hide)
        hdr.addWidget(close_x)
        root.addLayout(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        root.addWidget(sep)

        desc = QLabel(
            "Configure confirmation requirements for Jarvis actions:\n"
            "• LOW: Executes immediately without confirmation.\n"
            "• MEDIUM: Requires spoken voice confirmation (yes / confirm).\n"
            "• HIGH: Dual confirmation required (both voice AND on-screen button).\n"
            "Protected destructive operations cannot be downgraded to LOW."
        )
        desc.setFont(QFont("Montserrat", 8))
        desc.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; line-height: 130%;")
        desc.setWordWrap(True)
        root.addWidget(desc)

        # Scroll area for items
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        form = QVBoxLayout(inner)
        form.setContentsMargins(0, 4, 8, 4)
        form.setSpacing(6)

        from memory.config_manager import get_confirmation_tiers
        configured = get_confirmation_tiers() or {}

        for category, items in self.CONFIGURABLE_ITEMS:
            cat_lbl = QLabel(f"━  {category}")
            cat_lbl.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
            cat_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent; margin-top: 8px; margin-bottom: 2px;")
            form.addWidget(cat_lbl)

            for key, label, note, default_tier, is_protected in items:
                row_widget = QWidget()
                row_widget.setStyleSheet(f"""
                    QWidget {{
                        background: {C.PANEL2};
                        border: 1px solid {C.BORDER};
                        border-radius: 4px;
                    }}
                """)
                row_lay = QHBoxLayout(row_widget)
                row_lay.setContentsMargins(8, 4, 8, 4)
                row_lay.setSpacing(8)

                info_lay = QVBoxLayout()
                info_lay.setSpacing(1)

                name_lbl = QLabel(label)
                name_lbl.setFont(QFont("Montserrat", 9, QFont.Weight.Bold))
                name_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
                info_lay.addWidget(name_lbl)

                note_lbl = QLabel(note)
                note_lbl.setFont(QFont("Montserrat", 7))
                note_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
                info_lay.addWidget(note_lbl)

                row_lay.addLayout(info_lay, stretch=1)

                if is_protected:
                    prot_lbl = QLabel("[PROTECTED]")
                    prot_lbl.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
                    prot_lbl.setStyleSheet(f"color: {C.ACC}; background: transparent; margin-right: 4px;")
                    row_lay.addWidget(prot_lbl)

                cb = QComboBox()
                if is_protected:
                    cb.addItems(["HIGH", "MEDIUM"])
                else:
                    cb.addItems(["LOW", "MEDIUM", "HIGH"])

                cb.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
                cb.setFixedWidth(90)
                cb.setFixedHeight(26)
                cb.setStyleSheet(f"""
                    QComboBox {{
                        background: #000d12;
                        color: {C.TEXT};
                        border: 1px solid {C.BORDER_B};
                        border-radius: 3px;
                        padding: 1px 6px;
                    }}
                    QComboBox QAbstractItemView {{
                        background: #000d12;
                        color: {C.TEXT};
                        selection-background-color: {C.PRI_GHO};
                    }}
                """)

                # Set current value
                curr_tier = str(configured.get(key, default_tier)).upper()
                if is_protected and curr_tier == "LOW":
                    curr_tier = "HIGH"
                idx = cb.findText(curr_tier)
                if idx >= 0:
                    cb.setCurrentIndex(idx)
                else:
                    cb.setCurrentText(default_tier)

                self._combos[key] = cb
                row_lay.addWidget(cb)
                form.addWidget(row_widget)

        form.addStretch(1)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        # Status label
        self._status_lbl = QLabel("")
        self._status_lbl.setFont(QFont("Montserrat", 8))
        self._status_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        root.addWidget(self._status_lbl)

        # Bottom buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        save_btn = QPushButton("▸  SAVE CHANGES")
        save_btn.setFixedHeight(32)
        save_btn.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px; padding: 0 12px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        save_btn.clicked.connect(self._save_tiers)
        btn_row.addWidget(save_btn)

        reset_btn = QPushButton("RESET DEFAULTS")
        reset_btn.setFixedHeight(32)
        reset_btn.setFont(QFont("Montserrat", 8))
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 10px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        reset_btn.clicked.connect(self._reset_defaults)
        btn_row.addWidget(reset_btn)

        btn_row.addStretch()

        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(32)
        close_btn.setFont(QFont("Montserrat", 8))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 12px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        btn_row.addWidget(close_btn)

        root.addLayout(btn_row)

    def _save_tiers(self):
        from memory.config_manager import save_confirmation_tiers
        tiers = {}
        for key, cb in self._combos.items():
            val = cb.currentText().strip().upper()
            tiers[key] = val
        try:
            save_confirmation_tiers(tiers)
            self._status_lbl.setText("✓ Confirmation tier preferences saved.")
            self._status_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        except Exception as exc:
            self._status_lbl.setText(f"Error saving: {exc}")
            self._status_lbl.setStyleSheet(f"color: {C.ACC}; background: transparent;")

    def _reset_defaults(self):
        from memory.config_manager import save_confirmation_tiers
        try:
            save_confirmation_tiers({})
            for category, items in self.CONFIGURABLE_ITEMS:
                for key, label, note, default_tier, is_protected in items:
                    cb = self._combos.get(key)
                    if cb:
                        idx = cb.findText(default_tier)
                        if idx >= 0:
                            cb.setCurrentIndex(idx)
            self._status_lbl.setText("✓ Reset to factory confirmation tiers.")
            self._status_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        except Exception as exc:
            self._status_lbl.setText(f"Error resetting: {exc}")
            self._status_lbl.setStyleSheet(f"color: {C.ACC}; background: transparent;")


class PermissionsDrawerOverlay(QWidget):
    """Floating cyberpunk drawer — manage action permission policies (ASK, ALLOW, DENY) and decision audit log."""

    _OW = 640
    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PermissionsDrawerOverlay {{
                background: rgba(0, 6, 10, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self._combos: dict[str, QComboBox] = {}
        self._item_rows: list[tuple[QWidget, str]] = []  # (row_widget, search_text)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)

        # Header
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        title_lbl = QLabel("◈  PERMISSIONS DRAWER")
        title_lbl.setFont(QFont("Montserrat", 11, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title_lbl)
        hdr.addStretch()

        close_x = QPushButton("✕")
        close_x.setFixedSize(20, 20)
        close_x.setFont(QFont("Montserrat", 9))
        close_x.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        close_x.setCursor(Qt.CursorShape.PointingHandCursor)
        close_x.clicked.connect(self._on_close)
        hdr.addWidget(close_x)
        root.addLayout(hdr)

        # Navigation / Mode buttons: "POLICIES" & "DECISIONS LOG"
        tab_bar = QHBoxLayout()
        tab_bar.setSpacing(6)
        self._btn_policies = QPushButton("⚙  POLICIES")
        self._btn_policies.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        self._btn_policies.setCheckable(True)
        self._btn_policies.setChecked(True)
        self._btn_policies.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_policies.setStyleSheet(self._tab_btn_style(True))
        self._btn_policies.clicked.connect(lambda: self._set_view(0))
        tab_bar.addWidget(self._btn_policies)

        self._btn_decisions = QPushButton("📋  RECENT DECISIONS")
        self._btn_decisions.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        self._btn_decisions.setCheckable(True)
        self._btn_decisions.setChecked(False)
        self._btn_decisions.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_decisions.setStyleSheet(self._tab_btn_style(False))
        self._btn_decisions.clicked.connect(lambda: self._set_view(1))
        tab_bar.addWidget(self._btn_decisions)
        tab_bar.addStretch()
        root.addLayout(tab_bar)

        # Stacked view: Page 0 = Policies, Page 1 = Decisions Log
        self._pages = QStackedWidget()
        root.addWidget(self._pages, stretch=1)

        # ── Page 0: Policies ──
        p0 = QWidget()
        p0.setStyleSheet("background: transparent;")
        p0_lay = QVBoxLayout(p0)
        p0_lay.setContentsMargins(0, 4, 0, 0)
        p0_lay.setSpacing(8)

        # Search box
        search_box = QHBoxLayout()
        search_box.setSpacing(6)
        search_icon = QLabel("🔍")
        search_icon.setFont(QFont("Montserrat", 9))
        search_icon.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        search_box.addWidget(search_icon)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search permissions by name, tier, owner...")
        self._search_input.setFont(QFont("Montserrat", 8))
        self._search_input.setFixedHeight(28)
        self._search_input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d12;
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 0 8px;
            }}
            QLineEdit:focus {{
                border-color: {C.PRI};
            }}
        """)
        self._search_input.textChanged.connect(self._filter_items)
        search_box.addWidget(self._search_input, stretch=1)
        p0_lay.addLayout(search_box)

        # Scroll area for items
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        self._form = QVBoxLayout(inner)
        self._form.setContentsMargins(0, 4, 8, 4)
        self._form.setSpacing(6)

        self._populate_policies()
        self._form.addStretch(1)
        scroll.setWidget(inner)
        p0_lay.addWidget(scroll, 1)
        self._pages.addWidget(p0)

        # ── Page 1: Decision Log ──
        p1 = QWidget()
        p1.setStyleSheet("background: transparent;")
        p1_lay = QVBoxLayout(p1)
        p1_lay.setContentsMargins(0, 4, 0, 0)
        p1_lay.setSpacing(8)

        log_hdr = QHBoxLayout()
        log_desc = QLabel("Audited decisions made by the central permission gate:")
        log_desc.setFont(QFont("Montserrat", 8))
        log_desc.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        log_hdr.addWidget(log_desc)
        log_hdr.addStretch()

        ref_btn = QPushButton("↺  REFRESH LOG")
        ref_btn.setFont(QFont("Montserrat", 7, QFont.Weight.Bold))
        ref_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ref_btn.setStyleSheet(f"""
            QPushButton {{ background: {C.PANEL2}; color: {C.PRI};
                border: 1px solid {C.BORDER_B}; border-radius: 3px; padding: 4px 8px; }}
            QPushButton:hover {{ border-color: {C.PRI}; color: #ffffff; }}
        """)
        ref_btn.clicked.connect(self._refresh_decisions_log)
        log_hdr.addWidget(ref_btn)
        p1_lay.addLayout(log_hdr)

        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setFont(QFont("Consolas", 8))
        self._log_text.setStyleSheet(f"""
            QTextEdit {{
                background: #000910;
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px;
            }}
        """)
        p1_lay.addWidget(self._log_text, stretch=1)
        self._pages.addWidget(p1)

        # Bottom Bar: Status Label, Reset Button, Close Button
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._status_lbl = QLabel("")
        self._status_lbl.setFont(QFont("Montserrat", 8))
        self._status_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        btn_row.addWidget(self._status_lbl, stretch=1)

        reset_btn = QPushButton("↺  RESET TO DEFAULTS")
        reset_btn.setFixedHeight(30)
        reset_btn.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.ACC2};
                border: 1px solid {C.ACC2}; border-radius: 3px; padding: 0 10px; }}
            QPushButton:hover {{ background: rgba(255, 145, 0, 0.15); color: #ffffff; }}
        """)
        reset_btn.clicked.connect(self._reset_defaults)
        btn_row.addWidget(reset_btn)

        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(30)
        close_btn.setFont(QFont("Montserrat", 8))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 12px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._on_close)
        btn_row.addWidget(close_btn)

        root.addLayout(btn_row)

    def _tab_btn_style(self, active: bool) -> str:
        if active:
            return f"""
                QPushButton {{
                    background: {C.PRI_GHO}; color: {C.PRI};
                    border: 1px solid {C.PRI}; border-radius: 3px; padding: 4px 10px;
                }}
            """
        return f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 10px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """

    def _set_view(self, index: int):
        self._pages.setCurrentIndex(index)
        self._btn_policies.setChecked(index == 0)
        self._btn_policies.setStyleSheet(self._tab_btn_style(index == 0))
        self._btn_decisions.setChecked(index == 1)
        self._btn_decisions.setStyleSheet(self._tab_btn_style(index == 1))
        if index == 1:
            self._refresh_decisions_log()

    def _populate_policies(self):
        from core.permissions import (
            get_permission_registry,
            resolve_permission_policy,
            DESTRUCTIVE,
            SENSITIVE,
            NORMAL,
            READ_ONLY,
            POLICY_ALLOW,
            POLICY_ASK,
            POLICY_DENY,
        )

        reg = get_permission_registry()
        by_tier = reg.by_tier()

        tier_info = [
            (DESTRUCTIVE, "DESTRUCTIVE (HIGH-RISK)", "#ff1744"),
            (SENSITIVE, "SENSITIVE (MEDIUM-RISK)", "#ff9100"),
            (NORMAL, "NORMAL (SAFE INTERACTIVE)", C.PRI),
            (READ_ONLY, "READ-ONLY (INFORMATIONAL)", "#00e676"),
        ]

        for tier_id, tier_title, tier_color in tier_info:
            entries = by_tier.get(tier_id, [])
            if not entries:
                continue

            section_hdr = QLabel(f"━  {tier_title}")
            section_hdr.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
            section_hdr.setStyleSheet(f"color: {tier_color}; background: transparent; margin-top: 10px; margin-bottom: 2px;")
            self._form.addWidget(section_hdr)

            for entry in sorted(entries, key=lambda e: e.label):
                row = QWidget()
                row.setStyleSheet(f"""
                    QWidget {{
                        background: {C.PANEL2};
                        border: 1px solid {C.BORDER};
                        border-radius: 4px;
                    }}
                """)
                row_lay = QHBoxLayout(row)
                row_lay.setContentsMargins(8, 6, 8, 6)
                row_lay.setSpacing(8)

                info_lay = QVBoxLayout()
                info_lay.setSpacing(1)

                title_row = QHBoxLayout()
                title_row.setSpacing(6)

                lbl = QLabel(entry.label)
                lbl.setFont(QFont("Montserrat", 9, QFont.Weight.Bold))
                lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
                title_row.addWidget(lbl)

                id_badge = QLabel(f"[{entry.id}]")
                id_badge.setFont(QFont("Consolas", 7))
                id_badge.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
                title_row.addWidget(id_badge)
                title_row.addStretch()
                info_lay.addLayout(title_row)

                desc_lbl = QLabel(entry.description or entry.id)
                desc_lbl.setFont(QFont("Montserrat", 7))
                desc_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
                desc_lbl.setWordWrap(True)
                info_lay.addWidget(desc_lbl)

                row_lay.addLayout(info_lay, stretch=1)

                cb = QComboBox()
                cb.addItems([POLICY_ASK, POLICY_ALLOW, POLICY_DENY])
                cb.setFont(QFont("Montserrat", 8, QFont.Weight.Bold))
                cb.setFixedWidth(85)
                cb.setFixedHeight(26)
                cb.setStyleSheet(f"""
                    QComboBox {{
                        background: #000d12;
                        color: {C.TEXT};
                        border: 1px solid {C.BORDER_B};
                        border-radius: 3px;
                        padding: 1px 6px;
                    }}
                    QComboBox QAbstractItemView {{
                        background: #000d12;
                        color: {C.TEXT};
                        selection-background-color: {C.PRI_GHO};
                    }}
                """)

                # Pre-fill current effective policy
                current_pol = resolve_permission_policy(entry.id)
                idx = cb.findText(current_pol)
                if idx >= 0:
                    cb.setCurrentIndex(idx)
                else:
                    cb.setCurrentText(entry.default_policy)

                def _make_handler(p_entry=entry, combo=cb):
                    def _on_change(new_val):
                        self._on_policy_changed(p_entry, new_val)
                    return _on_change

                cb.currentTextChanged.connect(_make_handler(entry, cb))
                self._combos[entry.id] = cb
                row_lay.addWidget(cb)

                self._form.addWidget(row)
                search_text = f"{entry.label} {entry.id} {entry.description} {entry.owner} {entry.tier}".lower()
                self._item_rows.append((row, search_text))

    def _on_policy_changed(self, entry, new_val: str):
        from core.permissions import (
            set_permission_policy,
            DESTRUCTIVE,
            POLICY_ALLOW,
        )

        clean_val = str(new_val).strip().upper()

        # Warning when setting DESTRUCTIVE to ALLOW
        if entry.tier == DESTRUCTIVE and clean_val == POLICY_ALLOW:
            warn_msg = (
                f"⚠️ CAUTION: You have set '{entry.label}' to ALLOW.\n\n"
                "This will permit the assistant to execute high-impact or destructive operations "
                "(such as process termination, file deletions, or system shutdown) WITHOUT asking for confirmation!\n\n"
                "Are you sure you want to proceed?"
            )
            ret = QMessageBox.warning(
                self,
                "Destructive Permission Warning",
                warn_msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if ret != QMessageBox.StandardButton.Yes:
                # Revert combo
                cb = self._combos.get(entry.id)
                if cb:
                    cb.blockSignals(True)
                    idx = cb.findText(entry.default_policy)
                    if idx >= 0:
                        cb.setCurrentIndex(idx)
                    cb.blockSignals(False)
                return

        try:
            set_permission_policy(entry.id, clean_val, from_ui=True)
            self._status_lbl.setText(f"✓ Saved policy '{clean_val}' for '{entry.label}'.")
            self._status_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        except Exception as exc:
            self._status_lbl.setText(f"Error saving: {exc}")
            self._status_lbl.setStyleSheet(f"color: {C.ACC}; background: transparent;")

    def _filter_items(self, text: str):
        q = str(text or "").strip().lower()
        for row_widget, search_text in self._item_rows:
            row_widget.setVisible(q in search_text if q else True)

    def _refresh_decisions_log(self):
        from core.permissions import get_recent_permission_decisions
        decisions = get_recent_permission_decisions(limit=60)
        if not decisions:
            self._log_text.setPlainText("No recent permission decisions logged.")
            return

        lines = []
        for d in decisions:
            ts = d.get("timestamp", "").replace("T", " ")[:19]
            dec = d.get("decision", "ASK")
            color = "#00e676" if dec == "ALLOWED" else ("#ff1744" if dec == "DENIED" else "#ff9100")
            act = d.get("action", "")
            pid = d.get("permission_id", "")
            reason = d.get("reason", "")
            lines.append(f"[{ts}] <font color='{color}'><b>[{dec}]</b></font> <b>{act}</b> ({pid}) — {reason}")

        self._log_text.setHtml("<br>".join(lines))

    def _reset_defaults(self):
        from core.permissions import (
            reset_permissions_to_defaults,
            get_permission_registry,
        )
        try:
            reset_permissions_to_defaults(from_ui=True)
            reg = get_permission_registry()
            for entry in reg.all():
                cb = self._combos.get(entry.id)
                if cb:
                    cb.blockSignals(True)
                    idx = cb.findText(entry.default_policy)
                    if idx >= 0:
                        cb.setCurrentIndex(idx)
                    cb.blockSignals(False)
            self._status_lbl.setText("✓ Reset all permission policies to system defaults.")
            self._status_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        except Exception as exc:
            self._status_lbl.setText(f"Error resetting defaults: {exc}")
            self._status_lbl.setStyleSheet(f"color: {C.ACC}; background: transparent;")

    def _on_close(self):
        self.hide()
        self.closed.emit()


# Export alias
PermissionsDrawer = PermissionsDrawerOverlay

