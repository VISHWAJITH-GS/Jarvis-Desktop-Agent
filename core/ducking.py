"""
core/ducking.py — Per-Application Audio Ducking for MARK LVI / JARVIS

Ducks background media applications (Spotify, YouTube, browser audio, etc.)
while JARVIS is speaking by adjusting individual application audio sessions
via Windows pycaw (ISimpleAudioVolume). Never changes the master volume.
Runs on worker threads and never touches Qt widgets.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import platform
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Constants
DEFAULT_DUCK_RATIO = 0.25      # 25% of current application volume
FADE_DOWN_SECONDS = 0.15       # 150 ms
FADE_UP_SECONDS = 0.40         # 400 ms
DEBOUNCE_PAUSE_SECONDS = 0.60  # 600 ms hold across inter-sentence pauses
SPEECH_MIN_DURATION = 0.10     # 100 ms minimum speech before engaging duck
MAX_DUCK_DURATION = 60.0       # 60 second watchdog timeout

_STATE_DIR = Path.home() / ".jarvis"
_STATE_FILE = _STATE_DIR / "ducking_state.json"


class AudioDucker:
    """
    Manages per-application audio session attenuation during assistant speech.
    Safe against crashes, session terminations, user overrides, and COM failures.
    """

    def __init__(self, state_file: Optional[Path] = None):
        self._state_file = state_file or _STATE_FILE
        self._lock = threading.RLock()
        self._is_ducked = False
        self._duck_start_time: float = 0.0
        self._last_speech_time: float = 0.0
        self._speech_active = False
        self._speech_start_time: float = 0.0

        # Snapshot of ducked sessions: session_key -> {orig_vol, ducked_vol, orig_mute, pid, name}
        self._snapshot: Dict[str, dict] = {}

        self._disabled = False
        self._consecutive_failures = 0
        self._logged_error = False

        self._restore_timer: Optional[threading.Timer] = None
        self._engage_timer: Optional[threading.Timer] = None
        self._watchdog_thread: Optional[threading.Thread] = None

        # Clean up on process exit
        atexit.register(self._emergency_restore_atexit)

        # Recover from previous crash if state file exists
        self.recover_stale_state()

    # ── Public API ───────────────────────────────────────────────────────────

    def notify_speech_activity(self, is_speaking: bool) -> None:
        """
        Called when speech playback starts or stops.
        Applies debouncing: delays engagement for short noises and holds ducking
        across pauses under 600 ms to prevent audio pumping.
        """
        if self._disabled or platform.system() != "Windows":
            return

        with self._lock:
            now = time.monotonic()
            if is_speaking:
                self._speech_active = True
                self._last_speech_time = now

                # Cancel any pending restore
                if self._restore_timer is not None:
                    try:
                        self._restore_timer.cancel()
                    except Exception:
                        pass
                    self._restore_timer = None

                if not self._is_ducked and self._engage_timer is None:
                    self._speech_start_time = now
                    # Debounce short clicks/chimes: engage after minimum speech duration
                    self._engage_timer = threading.Timer(
                        SPEECH_MIN_DURATION,
                        self._debounced_engage,
                    )
                    self._engage_timer.daemon = True
                    self._engage_timer.start()
            else:
                self._speech_active = False
                self._last_speech_time = now

                # Cancel engage timer if speech ended before minimum threshold
                if self._engage_timer is not None:
                    try:
                        self._engage_timer.cancel()
                    except Exception:
                        pass
                    self._engage_timer = None

                # If ducked, schedule restore after DEBOUNCE_PAUSE_SECONDS
                if self._is_ducked and self._restore_timer is None:
                    self._restore_timer = threading.Timer(
                        DEBOUNCE_PAUSE_SECONDS,
                        self._debounced_restore,
                    )
                    self._restore_timer.daemon = True
                    self._restore_timer.start()

    def force_restore(self) -> None:
        """Immediately restore all attenuated applications."""
        with self._lock:
            if self._restore_timer is not None:
                try:
                    self._restore_timer.cancel()
                except Exception:
                    pass
                self._restore_timer = None
            if self._engage_timer is not None:
                try:
                    self._engage_timer.cancel()
                except Exception:
                    pass
                self._engage_timer = None

            if self._is_ducked:
                self._perform_restore()

    def check_watchdog(self) -> None:
        """Enforces a 60-second maximum duck duration safety limit."""
        with self._lock:
            if self._is_ducked:
                if (time.monotonic() - self._duck_start_time) > MAX_DUCK_DURATION:
                    logger.warning("[AudioDucker] Watchdog expired (>60s). Restoring background audio.")
                    self._perform_restore()

    def recover_stale_state(self) -> None:
        """Check for and restore orphaned snapshot from a prior crash on startup."""
        if platform.system() != "Windows":
            return
        try:
            if self._state_file.exists():
                logger.info("[AudioDucker] Found stale ducking state file. Restoring volumes...")
                content = self._state_file.read_text(encoding="utf-8")
                snapshot = json.loads(content)
                if isinstance(snapshot, dict):
                    self._restore_from_snapshot_data(snapshot)
                self._delete_state_file()
        except Exception as e:
            logger.debug("[AudioDucker] Stale state recovery error: %s", e)
            self._delete_state_file()

    # ── Internal Worker Routines ─────────────────────────────────────────────

    def _debounced_engage(self) -> None:
        with self._lock:
            self._engage_timer = None
            if self._speech_active and not self._is_ducked:
                self._perform_duck()

    def _debounced_restore(self) -> None:
        with self._lock:
            self._restore_timer = None
            if not self._speech_active and self._is_ducked:
                self._perform_restore()

    def _perform_duck(self) -> None:
        """Duck external audio sessions to target percentage."""
        try:
            from memory.config_manager import (
                get_ducking_enabled,
                get_ducking_level,
                get_ducking_exclude_apps,
            )

            if not get_ducking_enabled():
                return

            duck_percent = get_ducking_level()
            duck_ratio = max(0.0, min(1.0, duck_percent / 100.0))
            excludes = {x.lower() for x in get_ducking_exclude_apps()}
            own_pids = self._get_jarvis_process_family()

            self._init_com()
            from pycaw.pycaw import AudioUtilities

            sessions = AudioUtilities.GetAllSessions()
            new_snapshot: Dict[str, dict] = {}
            fade_targets: List[tuple] = []  # (session, start_vol, target_vol)

            for s in sessions:
                try:
                    proc = s.Process
                    pid = proc.pid if proc else None
                    pname = (proc.name() if proc else "").lower()

                    # Exclude JARVIS and its child processes
                    if pid in own_pids or pname in ("python.exe", "pythonw.exe") and pid == os.getpid():
                        continue

                    # Exclude configured calling applications (Teams, Discord, Zoom, etc.)
                    if pname in excludes:
                        continue

                    sav = s.SimpleAudioVolume
                    if sav is None:
                        continue

                    cur_vol = float(sav.GetMasterVolume())
                    mute = int(sav.GetMute())
                    if cur_vol <= 0.001 or mute:
                        continue

                    target_vol = max(0.0, cur_vol * duck_ratio)
                    key = f"{pid}_{pname}"

                    new_snapshot[key] = {
                        "pid": pid,
                        "name": pname,
                        "orig_vol": cur_vol,
                        "ducked_vol": target_vol,
                        "orig_mute": mute,
                    }
                    fade_targets.append((sav, cur_vol, target_vol))
                except Exception:
                    continue

            if new_snapshot:
                self._snapshot = new_snapshot
                self._save_state_file(new_snapshot)
                self._is_ducked = True
                self._duck_start_time = time.monotonic()
                self._start_watchdog_thread()

                # Smoothly fade down (~150 ms)
                self._apply_fade(fade_targets, FADE_DOWN_SECONDS)
                self._consecutive_failures = 0
                logger.debug("[AudioDucker] Attenuated %d background audio sessions", len(fade_targets))
        except Exception as e:
            self._handle_failure(e, "ducking")
        finally:
            self._uninit_com()

    def _start_watchdog_thread(self) -> None:
        if self._watchdog_thread is None or not self._watchdog_thread.is_alive():
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                daemon=True,
                name="ducking-watchdog-rescan",
            )
            self._watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        """Periodic background monitor running every ~500 ms while ducked."""
        while True:
            with self._lock:
                if not self._is_ducked:
                    break
            time.sleep(0.5)
            with self._lock:
                if not self._is_ducked:
                    break
            self.check_watchdog()
            self._rescan_sessions_while_ducked()

    def _rescan_sessions_while_ducked(self) -> None:
        """Scan for any sessions that started mid-speech and duck them."""
        with self._lock:
            if not self._is_ducked:
                return

        try:
            from memory.config_manager import (
                get_ducking_level,
                get_ducking_exclude_apps,
            )

            duck_percent = get_ducking_level()
            duck_ratio = max(0.0, min(1.0, duck_percent / 100.0))
            excludes = {x.lower() for x in get_ducking_exclude_apps()}
            own_pids = self._get_jarvis_process_family()

            self._init_com()
            from pycaw.pycaw import AudioUtilities

            sessions = AudioUtilities.GetAllSessions()
            new_fade_targets: List[tuple] = []

            with self._lock:
                if not self._is_ducked:
                    return

                for s in sessions:
                    try:
                        proc = s.Process
                        pid = proc.pid if proc else None
                        pname = (proc.name() if proc else "").lower()
                        if pid in own_pids or (pname in ("python.exe", "pythonw.exe") and pid == os.getpid()):
                            continue
                        if pname in excludes:
                            continue

                        key = f"{pid}_{pname}"
                        if key in self._snapshot:
                            continue  # already ducked

                        sav = s.SimpleAudioVolume
                        if sav is None:
                            continue

                        cur_vol = float(sav.GetMasterVolume())
                        mute = int(sav.GetMute())
                        if cur_vol <= 0.001 or mute:
                            continue

                        target_vol = max(0.0, cur_vol * duck_ratio)
                        self._snapshot[key] = {
                            "pid": pid,
                            "name": pname,
                            "orig_vol": cur_vol,
                            "ducked_vol": target_vol,
                            "orig_mute": mute,
                        }
                        new_fade_targets.append((sav, cur_vol, target_vol))
                    except Exception:
                        continue

                if new_fade_targets:
                    self._save_state_file(self._snapshot)
                    self._apply_fade(new_fade_targets, FADE_DOWN_SECONDS)
                    logger.debug("[AudioDucker] Ducked %d new mid-speech sessions", len(new_fade_targets))
        except Exception as e:
            logger.debug("[AudioDucker] Rescan error: %s", e)
        finally:
            self._uninit_com()

    def _perform_restore(self) -> None:
        """Restore attenuated applications to their original volume."""
        if not self._snapshot:
            self._is_ducked = False
            self._delete_state_file()
            return

        try:
            self._init_com()
            from pycaw.pycaw import AudioUtilities

            sessions = AudioUtilities.GetAllSessions()
            session_map = {}
            for s in sessions:
                try:
                    proc = s.Process
                    pid = proc.pid if proc else None
                    pname = (proc.name() if proc else "").lower()
                    key = f"{pid}_{pname}"
                    session_map[key] = s
                except Exception:
                    continue

            fade_targets: List[tuple] = []  # (session, start_vol, target_vol)

            for key, info in list(self._snapshot.items()):
                try:
                    s = session_map.get(key)
                    if not s or not s.SimpleAudioVolume:
                        continue

                    sav = s.SimpleAudioVolume
                    cur_vol = float(sav.GetMasterVolume())
                    ducked_vol = float(info.get("ducked_vol", 0.0))
                    orig_vol = float(info.get("orig_vol", 1.0))
                    orig_mute = int(info.get("orig_mute", 0))

                    # Safety check: if user adjusted volume during duck, do not overwrite!
                    if abs(cur_vol - ducked_vol) > 0.06:
                        logger.debug("[AudioDucker] User manually adjusted volume for %s; skipping restore.", info.get("name"))
                        continue

                    fade_targets.append((sav, cur_vol, orig_vol))
                    sav.SetMute(orig_mute, None)
                except Exception:
                    continue

            if fade_targets:
                # Smoothly fade up (~400 ms)
                self._apply_fade(fade_targets, FADE_UP_SECONDS)
                logger.debug("[AudioDucker] Restored %d audio sessions", len(fade_targets))

            self._snapshot.clear()
            self._is_ducked = False
            self._delete_state_file()
            self._consecutive_failures = 0
        except Exception as e:
            self._handle_failure(e, "restoring")
        finally:
            self._uninit_com()

    def _restore_from_snapshot_data(self, snapshot: Dict[str, dict]) -> None:
        """Restores audio sessions from an arbitrary snapshot dictionary."""
        self._init_com()
        try:
            from pycaw.pycaw import AudioUtilities
            sessions = AudioUtilities.GetAllSessions()
            for s in sessions:
                try:
                    proc = s.Process
                    pid = proc.pid if proc else None
                    pname = (proc.name() if proc else "").lower()
                    key = f"{pid}_{pname}"
                    if key in snapshot:
                        info = snapshot[key]
                        sav = s.SimpleAudioVolume
                        if sav:
                            cur_vol = float(sav.GetMasterVolume())
                            ducked_vol = float(info.get("ducked_vol", 0.0))
                            orig_vol = float(info.get("orig_vol", 1.0))
                            # Only restore if it appears still ducked (near ducked value)
                            if abs(cur_vol - ducked_vol) <= 0.06:
                                sav.SetMasterVolume(orig_vol, None)
                                sav.SetMute(int(info.get("orig_mute", 0)), None)
                except Exception:
                    continue
        finally:
            self._uninit_com()

    def _apply_fade(self, targets: List[tuple], duration: float) -> None:
        """Smooth linear volume interpolation across targets."""
        if not targets:
            return
        steps = max(3, int(duration / 0.04))
        dt = duration / steps
        for step in range(1, steps + 1):
            alpha = step / steps
            for sav, start_vol, end_vol in targets:
                try:
                    v = start_vol + alpha * (end_vol - start_vol)
                    sav.SetMasterVolume(max(0.0, min(1.0, v)), None)
                except Exception:
                    pass
            time.sleep(dt)

    # ── Safety & Process Utilities ───────────────────────────────────────────

    def _get_jarvis_process_family(self) -> Set[int]:
        """Collect current process PID and all child PIDs."""
        pids = {os.getpid()}
        try:
            import psutil
            parent = psutil.Process(os.getpid())
            for child in parent.children(recursive=True):
                pids.add(child.pid)
        except Exception:
            pass
        return pids

    def _save_state_file(self, snapshot: Dict[str, dict]) -> None:
        try:
            self._state_dir_ensure()
            self._state_file.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug("[AudioDucker] Failed to write state file: %s", e)

    def _delete_state_file(self) -> None:
        try:
            if self._state_file.exists():
                self._state_file.unlink()
        except Exception as e:
            logger.debug("[AudioDucker] Failed to delete state file: %s", e)

    def _state_dir_ensure(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _init_com(self) -> None:
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass

    def _uninit_com(self) -> None:
        try:
            import comtypes
            comtypes.CoUninitialize()
        except Exception:
            pass

    def _handle_failure(self, err: Exception, context: str) -> None:
        self._consecutive_failures += 1
        if not self._logged_error:
            self._logged_error = True
            logger.warning("[AudioDucker] Error during %s: %s", context, err)

        if self._consecutive_failures >= 5:
            self._disabled = True
            logger.warning("[AudioDucker] Repeated errors encountered. Ducking disabled for this session.")

    def _emergency_restore_atexit(self) -> None:
        """atexit handler ensuring original volumes are restored on clean shutdown."""
        if self._is_ducked:
            try:
                self._perform_restore()
            except Exception:
                pass


# ── Global Singleton & Convenience Helpers ───────────────────────────────────

_global_ducker: Optional[AudioDucker] = None
_global_ducker_lock = threading.Lock()


def get_ducker() -> AudioDucker:
    global _global_ducker
    with _global_ducker_lock:
        if _global_ducker is None:
            _global_ducker = AudioDucker()
        return _global_ducker


def notify_speech_activity(is_speaking: bool) -> None:
    """Thread-safe notification hook when JARVIS starts or finishes speaking."""
    try:
        get_ducker().notify_speech_activity(is_speaking)
    except Exception:
        pass


def duck_background_audio() -> None:
    try:
        get_ducker().notify_speech_activity(True)
    except Exception:
        pass


def restore_background_audio() -> None:
    try:
        get_ducker().force_restore()
    except Exception:
        pass
