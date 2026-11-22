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

from gui.window import MainWindow


class _RootShim:
    def __init__(self, app: QApplication, ui=None):
        self._app = app
        self._ui = ui

    def mainloop(self):
        try:
            self._app.exec()
        finally:
            if self._ui is not None:
                self._ui._is_closed = True

    def protocol(self, *_):
        pass


def _auto_ui_scale() -> float:
    """A scale for the interface from the primary screen's usable height.

    Every size in this file — 7 and 8 point text, fixed button heights, panel
    widths — was laid out on a 1080-pixel-tall screen. Windows only enlarges it
    when the user has raised display scaling, so on a 1440p monitor left at 100%
    the whole HUD came out at three-quarters size and the settings text was hard
    to read. Measured against 900 logical pixels rather than 1080 because the
    designed sizes were on the small side even there — reported as "generally
    small" on a 1440p screen already at 125%.

    Windows only, through calls that are not DPI-virtualised — so nothing about
    the process's DPI awareness is touched before Qt sets it. Elsewhere the
    system's own scaling is trusted and this returns 1."""
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        hdc = user32.GetDC(0)
        try:
            physical_h = gdi32.GetDeviceCaps(hdc, 117)  # DESKTOPVERTRES
        finally:
            user32.ReleaseDC(0, hdc)
        percent = ctypes.windll.shcore.GetScaleFactorForDevice(0) or 100
        logical_h = physical_h * 100.0 / percent
    except Exception:
        return 1.0
    return max(1.0, min(2.0, round(logical_h / 900.0 * 20) / 20))


def _apply_ui_scale() -> None:
    """Set Qt's global scale factor. Must run before QApplication exists."""
    if QApplication.instance() is not None or os.environ.get("QT_SCALE_FACTOR"):
        return  # too late, or the user set it themselves
    try:
        from memory.config_manager import get_ui_scale

        scale = get_ui_scale() or _auto_ui_scale()
    except Exception:
        scale = _auto_ui_scale()
    if abs(scale - 1.0) > 0.01:
        os.environ["QT_SCALE_FACTOR"] = f"{scale:.2f}"


class JarvisUI:
    def __init__(self, face_path: str, size=None):
        _apply_ui_scale()
        self._is_closed = False
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._app.setStyleSheet((self._app.styleSheet() or "") + "\n" + _NO_SCROLLBAR_STYLE)
        _load_app_fonts()
        self._win = MainWindow(face_path)
        self.root = _RootShim(self._app, self)
        self._win.show()

    def _is_alive(self) -> bool:
        if getattr(self, "_is_closed", False):
            return False
        try:
            return self._win is not None and not getattr(self._win, "_is_closed", False)
        except (RuntimeError, AttributeError):
            return False

    @property
    def muted(self) -> bool:
        try:
            if not self._is_alive():
                return True
            return self._win._muted
        except (RuntimeError, AttributeError):
            return True

    @muted.setter
    def muted(self, v: bool):
        try:
            if not self._is_alive():
                return
            self._win._mute_sig.emit(bool(v))
        except (RuntimeError, AttributeError):
            pass

    @property
    def speaker_muted(self) -> bool:
        return self.muted

    @speaker_muted.setter
    def speaker_muted(self, v: bool):
        self.muted = bool(v)

    @property
    def mic_muted(self) -> bool:
        return getattr(self, "_mic_muted", False)

    @mic_muted.setter
    def mic_muted(self, v: bool):
        self._mic_muted = bool(v)

    def mute_speaker(self) -> None:
        self.speaker_muted = True

    def unmute_speaker(self) -> None:
        self.speaker_muted = False

    def mute_mic(self) -> None:
        self.mic_muted = True

    def unmute_mic(self) -> None:
        self.mic_muted = False

    def minimize_jarvis(self) -> None:
        """Thread-safe: minimize Jarvis HUD."""
        try:
            if self._is_alive():
                self._win._jarvis_window_sig.emit("minimize")
        except (RuntimeError, AttributeError):
            pass

    def restore_jarvis(self) -> None:
        """Thread-safe: restore Jarvis HUD back to normal."""
        try:
            if self._is_alive():
                self._win._jarvis_window_sig.emit("restore")
        except (RuntimeError, AttributeError):
            pass

    def sync_mute_state(self, muted: bool, hud_state: str = "") -> None:
        """Thread-safe: atomically update mute indicator and exact HUD state
        (e.g. MUTED, SLEEPING, LISTENING)."""
        try:
            if not self._is_alive():
                return
            self._win._mute_state_sig.emit(bool(muted), str(hud_state or ""))
        except (RuntimeError, AttributeError):
            pass

    @property
    def on_mute_toggle(self):
        return self._win.on_mute_toggle

    @on_mute_toggle.setter
    def on_mute_toggle(self, cb):
        self._win.on_mute_toggle = cb

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    @property
    def on_voice_change(self):
        return self._win.on_voice_change

    @on_voice_change.setter
    def on_voice_change(self, cb):
        self._win.on_voice_change = cb

    @property
    def on_audio_device_change(self):
        return self._win.on_audio_device_change

    @on_audio_device_change.setter
    def on_audio_device_change(self, cb):
        self._win.on_audio_device_change = cb

    def show_confirm(self, title: str, detail: str, tier: str = "MEDIUM") -> None:
        """Thread-safe: raise the irreversible-action gate. Called from action
        handlers running in executor threads, so it goes through a signal."""
        self._win._confirm_sig.emit(str(title)[:120], str(detail)[:300], str(tier))

    def hide_confirm(self) -> None:
        """Thread-safe: take the gate down."""
        self._win._confirm_hide_sig.emit()

    @property
    def get_plugins(self):
        return self._win.get_plugins

    @get_plugins.setter
    def get_plugins(self, cb):
        self._win.get_plugins = cb

    @property
    def get_plugin_settings(self):
        return self._win.get_plugin_settings

    @get_plugin_settings.setter
    def get_plugin_settings(self, cb):
        self._win.get_plugin_settings = cb

    @property
    def on_wake_toggle(self):
        return self._win.on_wake_toggle

    @on_wake_toggle.setter
    def on_wake_toggle(self, cb):
        self._win.on_wake_toggle = cb

    @property
    def on_wake_manual(self):
        return self._win.on_wake_manual

    @on_wake_manual.setter
    def on_wake_manual(self, cb):
        self._win.on_wake_manual = cb

    @property
    def wake_get_state(self):
        return self._win.wake_get_state

    @wake_get_state.setter
    def wake_get_state(self, cb):
        self._win.wake_get_state = cb

    @property
    def on_auto_sleep_toggle(self):
        return self._win.on_auto_sleep_toggle

    @on_auto_sleep_toggle.setter
    def on_auto_sleep_toggle(self, cb):
        self._win.on_auto_sleep_toggle = cb

    @property
    def auto_sleep_is_enabled(self):
        return self._win.auto_sleep_is_enabled

    @auto_sleep_is_enabled.setter
    def auto_sleep_is_enabled(self, cb):
        self._win.auto_sleep_is_enabled = cb

    @property
    def on_routing_mode_change(self):
        return self._win.on_routing_mode_change

    @on_routing_mode_change.setter
    def on_routing_mode_change(self, cb):
        self._win.on_routing_mode_change = cb

    @property
    def get_routing_mode(self):
        return self._win.get_routing_mode

    @get_routing_mode.setter
    def get_routing_mode(self, cb):
        self._win.get_routing_mode = cb

    def set_audio_level(self, level: float) -> None:
        """Thread-safe: feed a 0.0–1.0 live audio level to the HUD waveform.
        Called from the audio threads; a plain float store is atomic under the
        GIL, so no signal/lock is needed for this cosmetic value."""
        try:
            self._win.hud.set_audio_level(level)
        except Exception:
            pass

    def glance(self, dx: float, dy: float, hold: float = 1.1) -> None:
        """Ask the avatar to look somewhere for a moment (see HoloAvatar.glance)."""
        try:
            if self._avatar is not None:
                self._avatar.glance(dx, dy, hold)
        except Exception:
            pass

    @property
    def ptt_hold(self):
        return self._win.ptt_hold

    @ptt_hold.setter
    def ptt_hold(self, cb):
        self._win.ptt_hold = cb

    @property
    def on_push_to_talk(self):
        return self._win.on_push_to_talk

    @on_push_to_talk.setter
    def on_push_to_talk(self, cb):
        self._win.on_push_to_talk = cb

    def push_visemes(self, frames, hop: float, at: float) -> None:
        """Thread-safe: post a schedule of (level, openness, width) mouth frames
        for JARVIS's own speech. `at` is the wall-clock time the batch begins to
        sound, not the time of the call. See HudCanvas.push_visemes()."""
        try:
            self._win.hud.push_visemes(frames, hop, at)
        except Exception:
            pass

    def notify_phone_connected(self) -> None:
        try:
            if not self._is_alive():
                return
            self._win.notify_phone_connected()
        except (RuntimeError, AttributeError):
            pass

    def set_state(self, state: str):
        try:
            if not self._is_alive():
                return
            self._win._state_sig.emit(state)
        except (RuntimeError, AttributeError):
            pass

    def write_log(self, text: str):
        try:
            if not self._is_alive():
                return
            self._win._log_sig.emit(text)
        except (RuntimeError, AttributeError):
            pass

    def stream_user_text(self, text: str):
        try:
            if not self._is_alive():
                return
            self._win._stream_user_sig.emit(str(text or ""))
        except (RuntimeError, AttributeError):
            pass

    def stream_reply_text(self, text: str):
        try:
            if not self._is_alive():
                return
            self._win._stream_reply_sig.emit(str(text or ""))
        except (RuntimeError, AttributeError):
            pass

    def wait_for_api_key(self):
        while not self._is_alive() or not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the panel below the HUD."""
        try:
            if not self._is_alive():
                return
            self._win._content_sig.emit(title[:48], text[:4000])
        except (RuntimeError, AttributeError):
            pass

    def show_code_job(self, job: dict) -> None:
        """Thread-safe: draw the coding agent's live progress in the panel."""
        try:
            if not self._is_alive():
                return
            self._win._codejob_sig.emit(dict(job))
        except (RuntimeError, AttributeError):
            pass

    def show_quiz(self, topic: str, questions, grade=None) -> None:
        """Thread-safe: put an interactive quiz on the board."""
        try:
            if not self._is_alive():
                return
            self._win._quiz_sig.emit(str(topic or ""), list(questions or []), grade)
        except (RuntimeError, AttributeError):
            pass

    def hide_quiz(self) -> None:
        """Thread-safe: clear any quiz currently on the board."""
        try:
            if not self._is_alive():
                return
            self._win._quiz_hide_sig.emit()
        except (RuntimeError, AttributeError):
            pass

    def show_review(self, title: str, summary: str, findings, unclear=None) -> None:
        """Thread-safe: lay a document review into the panel below the HUD."""
        try:
            if not self._is_alive():
                return
            self._win._review_sig.emit(str(title or ""), str(summary or ""), list(findings or []), list(unclear or []))
        except (RuntimeError, AttributeError):
            pass

    def prompt_reconfig(self):
        """Thread-safe: show the API key setup overlay (e.g. after an auth error)."""
        try:
            if not self._is_alive():
                return
            self._win._ready = False
            self._win._reconfig_sig.emit()
        except (RuntimeError, AttributeError):
            pass

    def show_video(self, source: str, title: str = "", muted: bool = True, audio_source: str = "") -> None:
        """Video display on HUD is removed per user request. Opens in external player."""
        try:
            import os, webbrowser

            s = str(source or "")
            if os.path.exists(s):
                os.startfile(s)
            elif s.startswith(("http://", "https://")):
                webbrowser.open(s)
            self.write_log(f"▶ Opened video '{title or s}' in external player.")
        except Exception:
            pass

    def stop_video(self) -> None:
        """Thread-safe: close the video and give the HUD back to the avatar."""
        try:
            if not self._is_alive():
                return
            self._win._video_close_sig.emit()
        except (RuntimeError, AttributeError):
            pass

    def set_video_muted(self, muted: bool) -> None:
        """Thread-safe: silence or unsilence whatever is playing."""
        try:
            if not self._is_alive():
                return
            self._win._video_mute_sig.emit(bool(muted))
        except (RuntimeError, AttributeError):
            pass

    def video_is_playing(self) -> bool:
        try:
            if not self._is_alive():
                return False
            return bool(self._win.video_is_playing())
        except (RuntimeError, AttributeError):
            return False

    @property
    def assistant_name(self) -> str:
        try:
            if not self._is_alive():
                return "JARVIS"
            return self._win._assistant_name
        except (RuntimeError, AttributeError):
            return "JARVIS"

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")

    def show_task_progress(self, task_data: dict) -> int:
        """Thread-safe: show or update plan & task progress in dedicated display window with actions."""
        try:
            if not self._is_alive():
                return 0
            self._win._codejob_sig.emit(task_data)
            return 1
        except (RuntimeError, AttributeError):
            return 0

    def show_plan(self, title: str, plan_data: dict) -> int:
        """Thread-safe: show plan in dedicated display window with actions."""
        data = dict(plan_data or {})
        data.setdefault("folder", title)
        data.setdefault("request", title)
        return self.show_task_progress(data)

    def open_display(self, content_type: str, source: str, title: str = "") -> int:
        """Thread-safe: Open an image or video in a new named floating display window."""
        try:
            if not self._is_alive():
                return 0
            disp_id = self._win._get_next_display_id(reserve=True)
            self._win._open_display_sig.emit(str(content_type), str(source), f"{disp_id}:{title}")
            return disp_id
        except (RuntimeError, AttributeError):
            return 0

    def focus_display(self, display_id: int = 0) -> bool:
        """Thread-safe: Focus display N (or latest if 0) and bring to front in center of screen."""
        try:
            if not self._is_alive():
                return False
            self._win._focus_display_sig.emit(int(display_id))
            return True
        except (RuntimeError, AttributeError):
            return False

    def maximize_display_window(self, display_id: int = 0) -> bool:
        """Thread-safe: Maximize display N (or 0 for all)."""
        try:
            if not self._is_alive():
                return False
            self._win._maximize_display_sig.emit(int(display_id))
            return True
        except (RuntimeError, AttributeError):
            return False

    def minimize_display_window(self, display_id: int = 0) -> bool:
        """Thread-safe: Minimize display N to the left edge dock (or 0 for all)."""
        try:
            if not self._is_alive():
                return False
            self._win._minimize_display_sig.emit(int(display_id))
            return True
        except (RuntimeError, AttributeError):
            return False

    def close_display_window(self, display_id: int = 0) -> bool:
        """Thread-safe: Close display N (or 0 for all)."""
        try:
            if not self._is_alive():
                return False
            self._win._close_display_sig.emit(int(display_id))
            return True
        except (RuntimeError, AttributeError):
            return False

    def get_open_displays(self) -> list[dict]:
        """Thread-safe query for currently open displays."""
        try:
            if not self._is_alive():
                return []
            res = []
            for d_id, w in list(self._win._displays.items()):
                res.append(
                    {
                        "display_id": d_id,
                        "name": getattr(w, "display_name", f"DISPLAY {d_id}"),
                        "type": getattr(w, "content_type", "image"),
                        "title": getattr(w, "title_text", ""),
                        "minimized": d_id in getattr(self._win, "_minimized_display_ids", set()),
                        "maximized": getattr(w, "_is_maximized_disp", False),
                    }
                )
            return res
        except (RuntimeError, AttributeError):
            return []

    def show_screen_glow(self, duration_ms: int = 2500):
        """Thread-safe: Trigger red glowing screen edge overlay indicating screen observation."""
        try:
            if self._is_alive():
                self._win._screen_glow_sig.emit(int(duration_ms))
        except (RuntimeError, AttributeError):
            pass

    def show_memory_actions_display(self, tab: str = "memory"):
        """Thread-safe: Open the dedicated Memory and Actions management platform."""
        try:
            if self._is_alive():
                self._win._memory_actions_sig.emit(str(tab))
        except (RuntimeError, AttributeError):
            pass

    def show_slide_alert(self, message: str, title: str = "SYSTEM ALERT"):
        """Thread-safe: Trigger sliding down HUD system alert."""
        try:
            if self._is_alive():
                self._win._slide_alert_sig.emit(str(title), str(message))
        except (RuntimeError, AttributeError):
            pass

    def show_reminders(self, scope: str = "upcoming"):
        """Thread-safe: Show or update reminders dock."""
        try:
            if self._is_alive():
                self._win._reminders_sig.emit(str(scope))
        except (RuntimeError, AttributeError):
            pass

    def show_tasks(self, scope: str = "all"):
        """Thread-safe: Display the active to-do list and task management window."""
        try:
            if self._is_alive():
                self._win._tasks_sig.emit(str(scope or "all"))
        except (RuntimeError, AttributeError):
            pass

    def show_todo_list(self, scope: str = "all"):
        """Thread-safe alias for show_tasks."""
        self.show_tasks(scope=scope)

    def toggle_search_dock_minimize(self):
        """Thread-safe: Toggle minimizing / restoring the search dock."""
        try:
            if self._is_alive():
                self._win._search_dock_min_sig.emit()
        except (RuntimeError, AttributeError):
            pass

    def jarvis_window_control(self, action: str):
        """Thread-safe: Control JARVIS main window (minimize/maximize/restore)."""
        try:
            if self._is_alive():
                self._win._jarvis_window_ctrl_sig.emit(str(action))
        except (RuntimeError, AttributeError):
            pass

    def minimize_jarvis(self):
        """Thread-safe: Minimize JARVIS."""
        self.jarvis_window_control("minimize")

    def refresh_audio_devices(self):
        """Thread-safe: Trigger audio device dropdown refresh."""
        try:
            if self._is_alive():
                self._win._refresh_audio_sig.emit()
        except (RuntimeError, AttributeError):
            pass

    def update_audio_status(self, status: dict) -> None:
        """Thread-safe: Push verified AudioManager status snapshot to the UI."""
        try:
            if self._is_alive():
                self._win._audio_status_sig.emit(dict(status or {}))
        except (RuntimeError, AttributeError):
            pass

    def maximize_jarvis(self):
        """Thread-safe: Maximize / restore JARVIS."""
        self.jarvis_window_control("maximize")

    def is_jarvis_minimized(self) -> bool:
        """Query if JARVIS is currently minimized."""
        try:
            if not self._is_alive():
                return False
            return bool(getattr(self._win, "_is_minimized_top", False) or self._win.isMinimized())
        except (RuntimeError, AttributeError):
            return False
