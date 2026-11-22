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
from gui.widgets import _FILE_ICONS, _file_category, _fmt_size
from gui.overlays import *
from gui.providers_panel import *
from gui.settings_overlays import *
from gui.docks import *
from gui.display_windows import *
from gui.telemetry_and_displays import *


class WindowControlsMixin:
    """Settings, PTT, wake word, overlays, and system controls for MainWindow."""

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(22)
        w.setStyleSheet(f"background: {C.DARK}; border-top: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(14, 0, 14, 0)

        def _fl(txt, color=C.TEXT_MED):
            l = QLabel(txt)
            l.setFont(QFont("Montserrat", 7))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [F11] Fullscreen"))
        lay.addStretch()
        lay.addWidget(_fl("By FatihMakes", C.PRI_DIM))
        return w

    def _browse_file(self):
        if hasattr(self, "_drop_zone") and self._drop_zone:
            self._drop_zone._browse()

    def _on_file_selected(self, path: str):
        self._current_file = path
        p = Path(path)
        cat = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        if cat == "folder":
            n = self._drop_zone._folder_files
            self._file_hint.setText(
                f"{icon}  {p.name}  ·  {n} files  ·  Tell {self._assistant_name} what to change in it"
            )
            self._log.append_log(f"FOLDER: {p.name} ({n} files) loaded")
            if self.on_text_command:
                msg = (
                    f"[FOLDER_UPLOADED] path={path} | name={p.name} | files={n} | "
                    f"Briefly tell the user the project folder '{p.name}' is loaded "
                    f"and that you can improve, fix or extend it with the coding "
                    f"model — ask what they want changed. When they say, call "
                    f"dev_agent with mode='improve' and their request as the "
                    f"description."
                )
                threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()
            return
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell {self._assistant_name} what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url = result[0]
        key = result[1]
        auto = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        berr = result[4] if len(result) >= 5 else ""
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov = RemoteKeyOverlay(
            url, key, auto_login_url=auto, manual_url=manual, expiry_secs=600, parent=cw, bind_error=berr
        )
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width() - ow) // 2,
            (cw.height() - oh) // 2,
            ow,
            oh,
        )
        ov.closed.connect(lambda: setattr(self, "_remote_overlay", None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    # ── Auto-start ──────────────────────────────────────────────────────────────

    def _check_autostart(self) -> bool:
        """Returns True if auto-start is currently registered on this OS."""
        try:
            if _OS == "Windows":
                import winreg

                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ
                )
                try:
                    winreg.QueryValueEx(key, "JARVIS_AI")
                    return True
                except FileNotFoundError:
                    return False
                finally:
                    winreg.CloseKey(key)
            elif _OS == "Darwin":
                return (Path.home() / "Library" / "LaunchAgents" / "com.jarvis.assistant.plist").exists()
            else:
                return (Path.home() / ".config" / "autostart" / "jarvis.desktop").exists()
        except Exception:
            return False

    def _toggle_autostart(self):
        currently_on = self._check_autostart()
        try:
            script = str(BASE_DIR / "main.py")
            if _OS == "Windows":
                import winreg

                reg = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS
                )
                if currently_on:
                    winreg.DeleteValue(reg, "JARVIS_AI")
                else:
                    pythonw = Path(sys.executable).parent / "pythonw.exe"
                    exe = str(pythonw if pythonw.exists() else sys.executable)
                    winreg.SetValueEx(reg, "JARVIS_AI", 0, winreg.REG_SZ, f'"{exe}" "{script}"')
                winreg.CloseKey(reg)
            elif _OS == "Darwin":
                plist_dir = Path.home() / "Library" / "LaunchAgents"
                plist_dir.mkdir(parents=True, exist_ok=True)
                plist = plist_dir / "com.jarvis.assistant.plist"
                if currently_on:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_text(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                        '<plist version="1.0"><dict>\n'
                        "  <key>Label</key><string>com.jarvis.assistant</string>\n"
                        "  <key>ProgramArguments</key><array>\n"
                        f"    <string>{sys.executable}</string>\n"
                        f"    <string>{script}</string>\n"
                        "  </array>\n"
                        "  <key>RunAtLoad</key><true/>\n"
                        "</dict></plist>\n"
                    )
            else:
                desk_dir = Path.home() / ".config" / "autostart"
                desk_dir.mkdir(parents=True, exist_ok=True)
                desk = desk_dir / "jarvis.desktop"
                if currently_on:
                    desk.unlink(missing_ok=True)
                else:
                    desk.write_text(
                        "[Desktop Entry]\n"
                        f"Name={self._assistant_name}\n"
                        f"Exec={sys.executable} {script}\n"
                        "Type=Application\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
            enabled = not currently_on
            self._update_autostart_btn(enabled)
            self._log.append_log(f"SYS: Auto-start {'enabled' if enabled else 'disabled'}.")
        except Exception as e:
            self._log.append_log(f"ERR: Auto-start failed — {e}")

    def _update_autostart_btn(self, enabled: bool):
        if not hasattr(self, "_autostart_btn"):
            return
        if enabled:
            self._autostart_btn.setText("◉  AUTO-START: ON")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._autostart_btn.setText("◉  AUTO-START: OFF")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _toggle_brief(self):
        from memory.config_manager import get_brief_enabled, save_brief_enabled

        new_val = not get_brief_enabled()
        save_brief_enabled(new_val)
        self._update_brief_btn(new_val)

    # ── Wake word settings ───────────────────────────────────────────────────

    def _wake_state(self) -> dict:
        """Combined state for the two wake-word buttons. Readiness is a cheap,
        deterministic on-disk check now (see core.wake_word.is_ready), so there
        is nothing to cache — the button never flickers to a stale value."""
        if self.wake_get_state:
            try:
                s = self.wake_get_state()
                return {"ready": bool(s.get("ready")), "enabled": bool(s.get("enabled")), "awake": bool(s.get("awake"))}
            except Exception:
                pass
        # Before JarvisLive has wired its callback (drawer built at startup).
        ready, enabled = False, False
        try:
            from core.wake_word import is_ready
            from memory.config_manager import get_wake_word_enabled

            ready, enabled = is_ready(), get_wake_word_enabled()
        except Exception:
            pass
        return {"ready": ready, "enabled": enabled, "awake": True}

    def _refresh_wake_btns(self):
        if not hasattr(self, "_wake_btn"):
            return
        st = self._wake_state()
        _on = f"""
            QPushButton {{ background: #001a08; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ background: #002010; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""
        self._wake_btn.setEnabled(True)
        if not st["ready"]:
            self._wake_btn.setText("⬇  WAKE WORD: DOWNLOAD")
            self._wake_btn.setStyleSheet(_off)
            if not st["awake"]:
                self._wake_sleep_btn.show()
                self._wake_sleep_btn.setText("👂  WAKE NOW")
                self._wake_sleep_btn.setStyleSheet(_off)
            else:
                self._wake_sleep_btn.hide()
        elif st["enabled"]:
            self._wake_btn.setText("🎙  WAKE WORD: ON")
            self._wake_btn.setStyleSheet(_on)
            self._wake_sleep_btn.show()
            self._wake_sleep_btn.setText("😴  SLEEP NOW" if st["awake"] else "👂  WAKE NOW")
            self._wake_sleep_btn.setStyleSheet(_off)
        else:
            self._wake_btn.setText("🎙  WAKE WORD: OFF")
            self._wake_btn.setStyleSheet(_off)
            if not st["awake"]:
                self._wake_sleep_btn.show()
                self._wake_sleep_btn.setText("👂  WAKE NOW")
                self._wake_sleep_btn.setStyleSheet(_off)
            else:
                self._wake_sleep_btn.hide()

        if hasattr(self, "_auto_sleep_btn"):
            sleep_enabled = self.auto_sleep_is_enabled() if self.auto_sleep_is_enabled else True
            if sleep_enabled:
                self._auto_sleep_btn.setText("💤  AUTO-SLEEP: ON")
                self._auto_sleep_btn.setStyleSheet(_on)
            else:
                self._auto_sleep_btn.setText("💤  AUTO-SLEEP: OFF")
                self._auto_sleep_btn.setStyleSheet(_off)

        if hasattr(self, "_auto_sleep_toggle_btn") and self._auto_sleep_toggle_btn:
            sleep_enabled = self.auto_sleep_is_enabled() if self.auto_sleep_is_enabled else True
            self._auto_sleep_toggle_btn.setText(
                f"AUTO-SLEEP: {'ENABLED' if sleep_enabled else 'DISABLED'} (CLICK TO TOGGLE)"
            )
            self._auto_sleep_toggle_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {C.PANEL2}; color: {C.PRI if sleep_enabled else C.TEXT_DIM};
                    border: 1px solid {C.PRI if sleep_enabled else C.BORDER_A}; border-radius: 3px; padding: 6px;
                }}
                QPushButton:hover {{ border-color: {C.PRI}; color: {C.TEXT}; }}
            """)

    def _toggle_auto_sleep(self):
        if self.on_auto_sleep_toggle:
            curr = self.auto_sleep_is_enabled() if self.auto_sleep_is_enabled else True
            try:
                self.on_auto_sleep_toggle(not curr)
            except Exception:
                pass
        self._refresh_wake_btns()

    _ui_toggle_auto_sleep = _toggle_auto_sleep

    def _cycle_routing_mode(self):
        from memory.config_manager import (
            get_audio_routing_mode,
            save_audio_routing_mode,
            ROUTING_MODE_AUTO_HYBRID,
            ROUTING_MODE_BUILTIN_ONLY,
            ROUTING_MODE_BLUETOOTH_ONLY,
        )

        curr = get_audio_routing_mode()
        if curr == ROUTING_MODE_AUTO_HYBRID:
            nxt = ROUTING_MODE_BUILTIN_ONLY
        elif curr == ROUTING_MODE_BUILTIN_ONLY:
            nxt = ROUTING_MODE_BLUETOOTH_ONLY
        else:
            nxt = ROUTING_MODE_AUTO_HYBRID
        save_audio_routing_mode(nxt)
        if hasattr(self, "on_routing_mode_change") and self.on_routing_mode_change:
            try:
                self.on_routing_mode_change(nxt)
            except Exception:
                pass
        self._refresh_routing_btn()
        if hasattr(self, "_combo_routing") and self._combo_routing:
            self._combo_routing.blockSignals(True)
            idx = self._combo_routing.findData(nxt)
            if idx >= 0:
                self._combo_routing.setCurrentIndex(idx)
            self._combo_routing.blockSignals(False)

    def _refresh_routing_btn(self):
        if not hasattr(self, "_routing_btn"):
            return
        from memory.config_manager import (
            get_audio_routing_mode,
            ROUTING_MODE_AUTO_HYBRID,
            ROUTING_MODE_BUILTIN_ONLY,
            ROUTING_MODE_BLUETOOTH_ONLY,
        )

        curr = get_audio_routing_mode()
        _on = f"""
            QPushButton {{ background: #001a14; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ background: #00261e; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""
        if curr == ROUTING_MODE_AUTO_HYBRID:
            self._routing_btn.setText("🎧  ROUTING: HYBRID (STEREO)")
            self._routing_btn.setStyleSheet(_on)
        elif curr == ROUTING_MODE_BUILTIN_ONLY:
            self._routing_btn.setText("💻  ROUTING: LAPTOP ONLY")
            self._routing_btn.setStyleSheet(_off)
        else:
            self._routing_btn.setText("🎧  ROUTING: OPPO ONLY")
            self._routing_btn.setStyleSheet(_off)

    def _refresh_talk_btns(self):
        """Repaint the push-to-talk row from the saved setting."""
        if not hasattr(self, "_ptt_btn"):
            return
        from core.hotkey import chord_label
        from memory.config_manager import get_push_to_talk_enabled

        _on = f"""
            QPushButton {{ background: #001a08; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ background: #002010; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""

        ptt = get_push_to_talk_enabled()
        self._ptt_btn.setText(f"🎚  PUSH-TO-TALK: {chord_label()}" if ptt else "🎚  PUSH-TO-TALK: OFF")
        self._ptt_btn.setStyleSheet(_on if ptt else _off)
        self._ptt_btn.setToolTip(
            "Microphone stays closed until you hold the key — nothing is sent while you are not holding it."
            if ptt
            else "Hold a key to talk instead of streaming the mic continuously."
        )

    def _refresh_hud_btn(self):
        from memory.config_manager import get_hud_style

        face = get_hud_style() == "face"
        # Neither state is "off", so both read as active — this is a choice
        # between two things, not a switch with a disabled side.
        style = f"""
            QPushButton {{ background: {C.PANEL2}; color: {C.PRI};
                border: 1px solid {C.BORDER_A}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.WHITE}; border: 1px solid {C.BORDER_B}; }}"""
        self._hud_btn.setText("🧑  HUD: ANIMATED FACE" if face else "◉  HUD: REACTOR CORE")
        self._hud_btn.setStyleSheet(style)
        self._hud_btn.setToolTip(
            "An animated head that speaks your words and shows what JARVIS is doing. Tap to switch to the reactor core."
            if face
            else "A reactor core that turns with the state and moves with your voice. "
            "Tap to switch to the animated head."
        )

    def _toggle_hud_style(self):
        """Swap the centrepiece. Both objects stay in memory, so the change is
        instant and switching back costs nothing."""
        from memory.config_manager import get_hud_style, save_hud_style

        want = "core" if get_hud_style() == "face" else "face"
        save_hud_style(want)
        try:
            self.hud.hud_style = want
            self.hud.update()
        except Exception:
            pass
        self._refresh_hud_btn()
        self._log.append_log(
            "SYS: HUD switched to the animated face." if want == "face" else "SYS: HUD switched to the reactor core."
        )

    def _toggle_ptt(self):
        from memory.config_manager import get_push_to_talk_enabled, save_push_to_talk_enabled

        want = not get_push_to_talk_enabled()
        save_push_to_talk_enabled(want)
        scope = None
        if self.on_push_to_talk:
            try:
                scope = self.on_push_to_talk(want)
            except Exception as e:
                self._log.append_log(f"ERR: Push-to-talk failed — {e}")
                save_push_to_talk_enabled(False)
                want = False
        self._apply_ptt_shortcut(want and scope != "global")
        self._refresh_talk_btns()

    def _apply_ptt_shortcut(self, needed: bool):
        """Bind the chord inside the window when no global hook is available.

        On macOS and Linux there is no dependency-free way to read global key
        state, so the chord is at least live whenever this window has focus.
        Qt gives no key-release for a QShortcut, so a press latches the mic open
        and a short timer closes it; held down, auto-repeat keeps pushing that
        timer out, which behaves like holding a key.
        """
        from PyQt6.QtGui import QKeySequence, QShortcut
        from core.hotkey import qt_sequence

        if not needed:
            sc = getattr(self, "_ptt_sc", None)
            if sc is not None:
                sc.setEnabled(False)
                self._ptt_sc = None
            self._ptt_hold(False)
            return
        if getattr(self, "_ptt_sc", None) is not None:
            return

        self._ptt_release = QTimer(self)
        self._ptt_release.setSingleShot(True)
        self._ptt_release.setInterval(420)
        self._ptt_release.timeout.connect(lambda: self._ptt_hold(False))

        def _press():
            self._ptt_hold(True)
            self._ptt_release.start()

        self._ptt_sc = QShortcut(QKeySequence(qt_sequence()), self)
        self._ptt_sc.setAutoRepeat(True)
        self._ptt_sc.activated.connect(_press)

    def _ptt_hold(self, held: bool):
        """Report a windowed press/release to whoever owns the microphone."""
        cb = getattr(self, "ptt_hold", None)
        if cb:
            try:
                cb(bool(held))
            except Exception:
                pass

    def _toggle_wake_word(self):
        st = self._wake_state()
        if not st["ready"]:
            # First time: download openwakeword + model in a worker thread.
            self._wake_btn.setText("⬇  DOWNLOADING… (one-time)")
            self._wake_btn.setEnabled(False)

            def _work():
                try:
                    from core.wake_word import install_and_download

                    ok, msg = install_and_download(logger=lambda m: self._log_sig.emit(f"SYS: {m}"))
                except Exception as e:
                    ok, msg = False, str(e)
                if ok and self.on_wake_toggle:
                    try:
                        self.on_wake_toggle(True)  # auto-enable after a successful download
                    except Exception:
                        pass
                self._wake_dl_sig.emit(ok, msg)

            threading.Thread(target=_work, daemon=True).start()
            return
        # Already downloaded → just flip enabled/disabled through JarvisLive.
        if self.on_wake_toggle:
            try:
                self.on_wake_toggle(not st["enabled"])
            except Exception:
                pass
        self._refresh_wake_btns()

    def _on_wake_install_done(self, ok: bool, msg: str):
        self._log_sig.emit(f"SYS: {'Wake word ready.' if ok else 'Wake word setup failed: ' + msg}")
        self._refresh_wake_btns()

    def _tap_wake_manual(self):
        if self.on_wake_manual:
            try:
                self.on_wake_manual()
            except Exception:
                pass
        self._refresh_wake_btns()

    def _update_brief_btn(self, enabled: bool):
        if not hasattr(self, "_brief_btn"):
            return
        if enabled:
            self._brief_btn.setText("  MORNING BRIEF: ON")
            self._brief_btn.setIcon(get_uicon("sun", color=C.GREEN, size=14))
            self._brief_btn.setIconSize(QSize(14, 14))
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._brief_btn.setText("  MORNING BRIEF: OFF")
            self._brief_btn.setIcon(get_uicon("sun", color=C.TEXT_DIM, size=14))
            self._brief_btn.setIconSize(QSize(14, 14))
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    # ── Customization ────────────────────────────────────────────────────────────

    def _open_customize(self):
        cfg = _read_full_config()
        if self._customize_overlay:
            self._customize_overlay.hide()
        cw = self.centralWidget()
        ov = CustomizeOverlay(
            cfg.get("assistant_name", "JARVIS") or "JARVIS",
            cfg.get("user_name", ""),
            cfg.get("ui_color", "") or DEFAULT_UI_COLOR,
            cfg.get("voice_name", ""),
            parent=cw,
        )
        ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width() - ow) // 2,
            (cw.height() - oh) // 2,
            ow,
            oh,
        )
        ov.on_preview = self._preview_ui_color
        ov.saved.connect(self._apply_name_update)
        ov.show()
        self._customize_overlay = ov

    def _preview_ui_color(self, hex_color: str):
        """Live preview — paints the whole interface the new colour (does NOT write to config)."""
        old = current_palette()
        if apply_ui_accent(hex_color):
            retheme_all_widgets(old, current_palette())

    def _apply_name_update(self, name: str, user_name: str, ui_color: str = "", voice: str = ""):
        """Update all name/theme-dependent UI elements and persist to config."""
        self._assistant_name = name.strip() or "JARVIS"
        display = self._assistant_name.upper()
        self.setWindowTitle(f"{display} — {APP_VERSION}")
        self._title_lbl.setText(display)
        if display in ("JARVIS", "J.A.R.V.I.S"):
            self._sub_lbl.setText("Just A Rather Very Intelligent System")
        else:
            self._sub_lbl.setText("Personal AI Assistant")
        self._log._ai_name_lc = self._assistant_name.lower()
        self.hud._assistant_name = display

        color_changed = False
        if ui_color:
            old = current_palette()
            if apply_ui_accent(ui_color):
                # Live-paint the whole interface (panels, buttons, borders, HUD)
                retheme_all_widgets(old, current_palette())
                color_changed = old["PRI"] != C.PRI

        # Voice change → persist and, if it actually changed, rebuild the Live
        # session so the new voice takes effect (it's fixed at connect time).
        voice_changed = False
        if voice:
            from memory.config_manager import get_voice, save_voice

            if voice != get_voice():
                save_voice(voice)
                voice_changed = True

        try:
            data = _read_full_config()
            data["assistant_name"] = self._assistant_name
            data["user_name"] = user_name.strip()
            if ui_color:
                data["ui_color"] = ui_color.strip().lower()
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
            self._log.append_log(f"SYS: Identity updated — {display}")
            if color_changed:
                self._log.append_log(f"SYS: UI colour applied — {ui_color}")
            if voice_changed:
                self._log.append_log(f"SYS: Voice set — {voice}")
        except Exception as e:
            self._log.append_log(f"ERR: Config save failed — {e}")

        if voice_changed and self.on_voice_change:
            self.on_voice_change()

    def _centre_overlay(self, ov) -> None:
        """Place a floating overlay in the middle of the HUD and show it."""
        cw = self.centralWidget()
        ov.adjustSize()
        ov.setGeometry(
            max(0, (cw.width() - ov.width()) // 2),
            max(0, (cw.height() - ov.height()) // 2),
            ov.width(),
            ov.height(),
        )
        ov.show()
        ov.raise_()

    # ── Audio devices ────────────────────────────────────────────────────────

    def _open_audio_devices(self):
        ov = AudioDeviceOverlay(parent=self.centralWidget())
        ov.picked.connect(self._on_audio_devices_applied)
        self._centre_overlay(ov)
        self._audio_overlay = ov  # keep a reference so it isn't GC'd

    def _on_audio_devices_applied(self):
        self._log.append_log("SYS: Audio devices updated.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Engine & providers ───────────────────────────────────────────────────

    def _open_providers(self):
        """Display engine info: Google Gemini API is the sole reasoning engine."""
        self._log.append_log(
            "SYS: Google Gemini API is the sole AI reasoning engine across JARVIS. "
            "Local and third-party LLM pipelines are disabled by architecture."
        )

    def _on_providers_applied(self):
        """Rebuild the session so the new engine takes over.

        Reuses the audio-device callback rather than adding a second one: both
        mean 'the conversation has to be rebuilt', main.py already keeps the
        transcript across such a rebuild through session resumption, and one
        path that is known to work is better than two that look alike."""
        self._log.append_log("SYS: Engine settings saved — reconnecting.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Memory panel ─────────────────────────────────────────────────────────

    def _open_memory_panel(self):
        ov = MemoryOverlay(parent=self.centralWidget())
        self._centre_overlay(ov)
        self._memory_overlay = ov

    # ── Irreversible-action confirmation ─────────────────────────────────────

    def _show_confirm_banner(self, title: str, detail: str, tier: str = "MEDIUM"):
        self._hide_confirm_banner()

        was_min = self.isMinimized() or getattr(self, "_is_minimized_top", False)

        # 1. If HUD is minimized in Arc Reactor mini-mode (58px orb), restore to normal HUD
        if getattr(self, "_is_minimized_top", False):
            self.toggle_minimize_top()

        # 2. If window is minimized to Windows taskbar, restore back to normal (not minimized)
        if self.isMinimized():
            self.showNormal()
        elif not self.isVisible():
            self.show()

        # 3. Apply state:
        # If minimized while asking confirmation, restore back to normal (not minimized).
        # Otherwise, if confirmation is needed, show in the maximized way.
        if was_min:
            self.showNormal()
            self._was_maximized_for_confirm = False
        else:
            self._was_maximized_for_confirm = not self.isMaximized()
            self.showMaximized()

        self.raise_()
        self.activateWindow()

        ov = ConfirmBanner(title, detail, tier=tier, parent=self.centralWidget())
        ov.answered.connect(self._on_confirm_answered)
        self._centre_overlay(ov)
        self._confirm_overlay = ov

    def _hide_confirm_banner(self):
        ov = getattr(self, "_confirm_overlay", None)
        if ov is not None:
            ov.hide()
            ov.deleteLater()
            self._confirm_overlay = None
        if getattr(self, "_was_maximized_for_confirm", False):
            self._was_maximized_for_confirm = False
            self.showNormal()

    def _on_confirm_answered(self, accepted: bool):
        from core.confirm import ConfirmationManager
        try:
            if not accepted:
                self._hide_confirm_banner()
                ConfirmationManager.resolve_by_button(False)
            else:
                executed, msg = ConfirmationManager.resolve_by_button(True, player=self)
                if executed:
                    self._hide_confirm_banner()
                else:
                    # High tier waiting for voice confirmation: update banner chip and keep up
                    ov = getattr(self, "_confirm_overlay", None)
                    if ov is not None and hasattr(ov, "set_button_confirmed"):
                        ov.set_button_confirmed(True)
                    self._log.append_log("SYS: Button confirmed. Please speak confirmation to execute.")
        except Exception as e:
            self._hide_confirm_banner()
            self._log.append_log(f"ERR: Confirmation failed — {e}")


    def _open_plugin_manager(self):
        plugins = self.get_plugins() if self.get_plugins else []
        cw = self.centralWidget()
        ov = PluginManagerOverlay(plugins, parent=cw)
        ov.adjustSize()
        ov.setGeometry(
            (cw.width() - ov.width()) // 2,
            (cw.height() - ov.height()) // 2,
            ov.width(),
            ov.height(),
        )
        ov.show()
        ov.raise_()
        self._plugin_manager_overlay = ov  # keep a reference so it isn't GC'd

    def _open_plugin_settings(self):
        sections = self.get_plugin_settings() if self.get_plugin_settings else []
        cw = self.centralWidget()
        ov = PluginSettingsOverlay(sections, parent=cw)
        ow = PluginSettingsOverlay._OW
        oh = min(560, cw.height() - 16)
        ov.setGeometry(
            (cw.width() - ow) // 2,
            (cw.height() - oh) // 2,
            ow,
            oh,
        )
        ov.show()
        ov.raise_()
        self._plugin_settings_overlay = ov  # keep a reference so it isn't GC'd

    # ── Clipboard intelligence (triggers removed) ───────────────────────────────

    def _on_clipboard_changed(self):
        # Automatic clipboard triggers removed per user request
        pass

    def _show_clipboard_panel(self, text: str):
        pass

    def _position_clipboard_panel(self):
        pass

    def _on_clipboard_action(self, cmd: str):
        pass

    # ────────────────────────────────────────────────────────────────────────────

    def _do_interrupt(self):
        if self.on_interrupt:
            self.on_interrupt()

    def _toggle_mute(self):
        # A deliberate press settles the question: whatever the video did, or is
        # about to do, the user has now said what they want.
        self._video_auto_muted = False
        if self.on_mute_toggle:
            try:
                self.on_mute_toggle()
                return
            except Exception:
                pass
        self._set_muted(not self._muted)

    def _sync_mute_state(self, muted: bool, hud_state: str = ""):
        """Atomically synchronize mute flag, mute buttons, and HUD state without
        clobbering restored states such as SLEEPING."""
        muted = bool(muted)
        self._muted = muted
        self.hud.muted = muted
        self._style_mute_btn()
        target = (hud_state or ("MUTED" if muted else "LISTENING")).upper()
        self._apply_state(target)
        if hasattr(self, "_wake_btn"):
            self._refresh_wake_btns()

    def _set_muted(self, muted: bool, note: str = ""):
        muted = bool(muted)
        if muted == self._muted:
            return
        self._muted = muted
        self.hud.muted = muted
        self._style_mute_btn()
        if muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted." + (f" {note}" if note else ""))
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active." + (f" {note}" if note else ""))

    def _style_mute_btn(self):
        if hasattr(self, "_hud_mute_btn"):
            if self._muted:
                self._hud_mute_btn.setText(" MUTED")
                self._hud_mute_btn.setIcon(get_uicon("microphone-slash", color=C.MUTED_C, size=13))
                self._hud_mute_btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba(80, 10, 20, 220);
                        color: {C.MUTED_C};
                        border: 1px solid {C.MUTED_C};
                        border-radius: 4px; padding: 4px 8px;
                    }}
                """)
            else:
                self._hud_mute_btn.setText(" MIC")
                self._hud_mute_btn.setIcon(get_uicon("circle-microphone", color=C.PRI, size=13))
                self._hud_mute_btn.setStyleSheet(f"""
                    QPushButton {{
                        background: rgba(24, 4, 8, 220);
                        color: {C.TEXT};
                        border: 1px solid {C.BORDER_B};
                        border-radius: 4px; padding: 4px 8px;
                    }}
                    QPushButton:hover {{
                        background: rgba(255, 23, 68, 0.25);
                        border: 1px solid {C.PRI};
                        color: #ffffff;
                    }}
                """)
        if hasattr(self, "_mute_btn"):
            if self._muted:
                self._mute_btn.setText("MICROPHONE MUTED")
                self._mute_btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #140006; color: {C.MUTED_C};
                        border: 1px solid {C.MUTED_C}; border-radius: 3px;
                    }}
                """)
            else:
                self._mute_btn.setText("MICROPHONE ACTIVE")
                self._mute_btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {C.PANEL}; color: {C.PRI};
                        border: 1px solid {C.BORDER_B}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
                """)

    def _send(self):
        txt = self._input.text().strip()
        if not txt:
            return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        self.hud.set_user_text(txt)
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state = state
        self.hud.speaking = str(state or "").upper() == "SPEAKING"
        if hasattr(self.hud, "on_state_changed"):
            self.hud.on_state_changed(state)
        self.hud.update()

    def _check_config(self) -> bool:
        try:
            from memory.config_manager import is_configured

            if is_configured():
                return True
        except Exception:
            pass
        if not API_FILE.exists():
            return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 390
        ov.setGeometry(
            (cw.width() - ow) // 2,
            (cw.height() - oh) // 2,
            ow,
            oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        API_FILE.write_text(
            json.dumps({"gemini_api_key": key, "os_system": os_name}, indent=4),
            encoding="utf-8",
        )
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._assistant_name = _read_full_config().get("assistant_name", "JARVIS") or "JARVIS"
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. {self._assistant_name} online.")
