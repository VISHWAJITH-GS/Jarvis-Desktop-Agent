"""
core/audio_manager.py — Centralized, Crash-Safe Audio & Bluetooth Hot-Switch Manager.

Single authoritative owner of:
  • Microphone (input) and speaker (output) stream lifecycles
  • Hardware sample-rate negotiation & real-time resampling (16 kHz in / 24 kHz out)
  • Safe PortAudio re-scanning (NEVER calls sd._terminate() while any stream is open)
  • In-place, thread-safe hot-switching without tearing down the Gemini Live session
  • Continuous Windows CoreAudio / Bluetooth connect, disconnect, and reconnect monitoring
  • Structured [AudioManager] diagnostic events and live UI status snapshots
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
import threading
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from core import audio_devices

logger = logging.getLogger(__name__)

# UI status indicator constants (exact vocabulary for the status panel)
STATUS_CONNECTED = "Connected"
STATUS_DISCONNECTED = "Disconnected"
STATUS_INITIALIZING = "Initializing"
STATUS_SWITCHING = "Switching"
STATUS_FAILED = "Failed"
STATUS_SYSTEM_DEFAULT = "Using system default"

TARGET_INPUT_RATE = 16000
TARGET_OUTPUT_RATE = 24000
DEFAULT_BLOCKSIZE = 1024
OPEN_TIMEOUT_S = 5.0


@dataclass
class AudioEvent:
    timestamp: str
    event_type: str
    prev_input: str
    new_input: str
    prev_output: str
    new_output: str
    result: str
    sample_rate: str
    failure_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "prev_input": self.prev_input,
            "new_input": self.new_input,
            "prev_output": self.prev_output,
            "new_output": self.new_output,
            "result": self.result,
            "sample_rate": self.sample_rate,
            "failure_reason": self.failure_reason,
        }


# Module-level active AudioManager singleton (wired once by JarvisLive)
_active_manager: Optional["AudioManager"] = None
_active_manager_lock = threading.Lock()


def set_audio_manager(mgr: Optional["AudioManager"]) -> None:
    global _active_manager
    with _active_manager_lock:
        _active_manager = mgr


def get_audio_manager() -> Optional["AudioManager"]:
    with _active_manager_lock:
        return _active_manager


def _run_with_timeout(
    fn: Callable[[], Any], timeout_s: float = OPEN_TIMEOUT_S
) -> Tuple[bool, Any, Optional[Exception]]:
    """Run a potentially blocking audio open/close call with a hard timeout."""
    box: Dict[str, Any] = {"done": False, "value": None, "error": None}

    def _worker() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:
            box["error"] = exc
        finally:
            box["done"] = True

    t = threading.Thread(target=_worker, daemon=True, name="audio-op-guard")
    t.start()
    t.join(timeout=max(0.5, float(timeout_s)))
    if not box["done"]:
        return False, None, TimeoutError(f"Audio operation timed out after {timeout_s:.1f}s")
    if box["error"] is not None:
        return False, None, box["error"]
    return True, box["value"], None


class AudioManager:
    """
    Centralized audio stream & Bluetooth hot-switching controller for JARVIS.
    Prevents duplicate streams, PortAudio mid-stream termination crashes,
    sample-rate mismatches, and unnecessary Gemini Live session restarts.
    """

    def __init__(
        self,
        target_input_rate: int = TARGET_INPUT_RATE,
        target_output_rate: int = TARGET_OUTPUT_RATE,
        blocksize: int = DEFAULT_BLOCKSIZE,
        on_status_change: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        poll_interval_s: float = 1.5,
        bt_settle_s: float = 0.8,
        open_timeout_s: float = OPEN_TIMEOUT_S,
    ):
        self._target_in_rate = int(target_input_rate)
        self._target_out_rate = int(target_output_rate)
        self._blocksize = int(blocksize)
        self._on_status_change = on_status_change
        self._on_log = on_log
        self._poll_interval_s = float(poll_interval_s)
        self._bt_settle_s = float(bt_settle_s)
        self._open_timeout_s = float(open_timeout_s)

        # Locks: _switch_lock serializes init/switch/teardown;
        # _io_lock guards the active output stream handle during writes.
        self._switch_lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._state_lock = threading.RLock()

        self._initialized = False
        self._shutdown_called = False
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

        # Active PortAudio streams
        self._in_stream: Any = None
        self._out_stream: Any = None
        self._user_in_callback: Optional[Callable[[Any, int, Any, Any], None]] = None

        # Active & preferred device state
        self._preferred_input: str = ""
        self._preferred_output: str = ""
        self._active_input_name: str = audio_devices.DEFAULT_LABEL
        self._active_output_name: str = audio_devices.DEFAULT_LABEL
        self._active_input_idx: Optional[int] = None
        self._active_output_idx: Optional[int] = None
        self._hw_input_rate: int = self._target_in_rate
        self._hw_output_rate: int = self._target_out_rate
        self._output_latency: float = 0.20

        # Live UI status indicators
        self._input_status: str = STATUS_INITIALIZING
        self._output_status: str = STATUS_INITIALIZING
        self._init_status: str = STATUS_INITIALIZING
        self._switching_status: str = "Idle"
        self._bluetooth_connected: bool = False
        self._bluetooth_device: str = ""
        self._last_error: str = ""

        # Endpoint tracking for hotplug monitoring
        self._last_default_out: Tuple[Optional[str], Optional[str]] = (None, None)
        self._last_default_in: Tuple[Optional[str], Optional[str]] = (None, None)
        self._last_snapshot: Set[str] = set()
        self._recovering_output = False
        self._recovering_input = False

        # Hybrid Bluetooth Audio Architecture state
        self._transition_lock = threading.RLock()
        self._routing_mode: str = "auto_hybrid"
        self._active_input_role: str = "builtin"
        self._transition_state: str = "idle"
        self._last_frame_received_ts: float = 0.0
        self._a2dp_restored: bool = True
        self._builtin_mic_name: str = ""
        self._bluetooth_mic_name: str = ""

        # Diagnostics ring buffer
        self._events: Deque[AudioEvent] = deque(maxlen=100)
        self._last_log_sig: str = ""

        audio_devices.configure(self._target_in_rate, self._target_out_rate)

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def initialized(self) -> bool:
        with self._state_lock:
            return self._initialized

    @property
    def output_latency(self) -> float:
        with self._state_lock:
            return self._output_latency

    @property
    def active_input_name(self) -> str:
        with self._state_lock:
            return self._active_input_name

    @property
    def active_output_name(self) -> str:
        with self._state_lock:
            return self._active_output_name

    @property
    def routing_mode(self) -> str:
        with self._state_lock:
            return self._routing_mode

    @property
    def transition_state(self) -> str:
        with self._state_lock:
            return self._transition_state

    @property
    def active_input_role(self) -> str:
        with self._state_lock:
            return self._active_input_role

    @property
    def builtin_mic_name(self) -> str:
        with self._state_lock:
            return self._builtin_mic_name

    @property
    def bluetooth_mic_name(self) -> str:
        with self._state_lock:
            return self._bluetooth_mic_name

    @property
    def a2dp_restored(self) -> bool:
        with self._state_lock:
            return self._a2dp_restored

    def set_routing_mode(self, mode: str) -> None:
        from memory import config_manager

        config_manager.save_audio_routing_mode(mode)
        with self._state_lock:
            self._routing_mode = config_manager.get_audio_routing_mode()
        self._notify_status()

    # ── Structured Logging & Status Snapshot ─────────────────────────────────

    def _log_ui(self, msg: str) -> None:
        if self._on_log:
            try:
                self._on_log(msg)
            except Exception:
                pass

    def _emit_event(
        self,
        event_type: str,
        prev_in: str,
        new_in: str,
        prev_out: str,
        new_out: str,
        result: str,
        sample_rate: str = "",
        failure_reason: str = "",
        console_line: Optional[str] = None,
    ) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        ev = AudioEvent(
            timestamp=ts,
            event_type=event_type,
            prev_input=prev_in,
            new_input=new_in,
            prev_output=prev_out,
            new_output=new_out,
            result=result,
            sample_rate=sample_rate,
            failure_reason=failure_reason,
        )
        with self._state_lock:
            self._events.append(ev)

        line = console_line or (
            f"[AudioManager] {event_type} | in: {prev_in} -> {new_in} | "
            f"out: {prev_out} -> {new_out} | rate={sample_rate or 'n/a'} | {result}"
            + (f" ({failure_reason})" if failure_reason else "")
        )
        sig = f"{event_type}|{new_in}|{new_out}|{result}|{failure_reason}"
        if sig != self._last_log_sig:
            self._last_log_sig = sig
            logger.info("%s", line)
            print(line)

    def get_status(self) -> Dict[str, Any]:
        """Return a thread-safe snapshot of all audio devices, rates, and indicators."""
        with self._state_lock:
            bt_status = (
                f"{STATUS_CONNECTED} ({self._bluetooth_device})"
                if self._bluetooth_connected and self._bluetooth_device
                else (STATUS_CONNECTED if self._bluetooth_connected else STATUS_DISCONNECTED)
            )
            return {
                "input_device": self._active_input_name,
                "output_device": self._active_output_name,
                "preferred_input": self._preferred_input or audio_devices.DEFAULT_LABEL,
                "preferred_output": self._preferred_output or audio_devices.DEFAULT_LABEL,
                "input_status": self._input_status,
                "output_status": self._output_status,
                "bluetooth_connected": self._bluetooth_connected,
                "bluetooth_device": self._bluetooth_device,
                "bluetooth_status": bt_status,
                "input_sample_rate": self._hw_input_rate,
                "output_sample_rate": self._hw_output_rate,
                "target_input_rate": self._target_in_rate,
                "target_output_rate": self._target_out_rate,
                "init_status": self._init_status,
                "switching_status": self._switching_status,
                "last_error": self._last_error,
                "output_latency": self._output_latency,
                "routing_mode": self._routing_mode,
                "active_input_role": self._active_input_role,
                "transition_state": self._transition_state,
                "builtin_microphone": self._builtin_mic_name,
                "bluetooth_microphone": self._bluetooth_mic_name,
                "a2dp_restored": self._a2dp_restored,
                "recent_events": [e.to_dict() for e in list(self._events)[-10:]],
            }

    def _notify_status(self) -> None:
        if self._on_status_change:
            try:
                self._on_status_change(self.get_status())
            except Exception as exc:
                logger.debug("[AudioManager] on_status_change error: %s", exc)

    def set_input_callback(self, callback: Optional[Callable[[Any, int, Any, Any], None]]) -> None:
        """Register or replace the consumer callback receiving 16 kHz mono int16 mic frames."""
        with self._state_lock:
            self._user_in_callback = callback

    # ── Centralized Startup Initialization ───────────────────────────────────

    def initialize(
        self,
        input_callback: Optional[Callable[[Any, int, Any, Any], None]] = None,
        preferred_input: Optional[str] = None,
        preferred_output: Optional[str] = None,
        start_monitor: bool = True,
    ) -> Dict[str, Any]:
        """Single-flight audio initialization called once at JARVIS startup.

        Discovers endpoints once, opens verified input and output streams with
        automatic sample-rate negotiation, and starts the hotplug monitor.
        Never raises — falls back gracefully to system default so JARVIS always boots.
        """
        with self._switch_lock:
            if input_callback is not None:
                self.set_input_callback(input_callback)

            if self._initialized and not self._shutdown_called:
                # Avoid duplicate initialization on reconnects; ensure streams are alive.
                if self._in_stream is None:
                    self._open_input_locked(self._preferred_input, reason="ensure_input")
                if self._out_stream is None:
                    self._open_output_locked(self._preferred_output, reason="ensure_output")
                return self.get_status()

            self._shutdown_called = False
            self._stop_event.clear()

            with self._state_lock:
                self._init_status = STATUS_INITIALIZING
                self._input_status = STATUS_INITIALIZING
                self._output_status = STATUS_INITIALIZING
                self._switching_status = STATUS_INITIALIZING
            self._notify_status()

            # Load saved preferences if not explicitly provided
            from memory.config_manager import (
                get_input_device,
                get_output_device,
                get_audio_routing_mode,
                get_builtin_microphone,
                get_bluetooth_microphone,
            )

            self._routing_mode = get_audio_routing_mode()
            self._preferred_input = (preferred_input if preferred_input is not None else get_input_device()).strip()
            self._preferred_output = (preferred_output if preferred_output is not None else get_output_device()).strip()

            # Single discovery pass at startup
            audio_devices.list_devices("input")
            audio_devices.list_devices("output")

            self._refresh_endpoint_baseline()
            self._update_bluetooth_presence()

            # Configure hybrid endpoints
            self._builtin_mic_name = get_builtin_microphone() or audio_devices.find_builtin_microphone() or ""
            self._bluetooth_mic_name = (
                get_bluetooth_microphone()
                or audio_devices.find_bluetooth_microphone(self._preferred_input)
                or self._preferred_input
            )

            # Determine initial startup microphone based on routing mode:
            # When preferred_input is not explicitly given, auto_hybrid / built_in_only
            # starts on laptop built-in mic so Bluetooth earbuds stay in high-quality
            # A2DP stereo music playback while JARVIS is sleeping.
            if preferred_input is None and self._routing_mode in ("auto_hybrid", "built_in_only"):
                target_in = self._builtin_mic_name or audio_devices.DEFAULT_LABEL
                self._active_input_role = "builtin"
            else:
                target_in = self._preferred_input
                self._active_input_role = "bluetooth" if audio_devices.is_bluetooth_device(target_in) else "builtin"

            # Initialize output first (so input routing knows if output is Bluetooth)
            out_ok = self._open_output_locked(self._preferred_output, reason="startup")
            in_ok = self._open_input_locked(target_in, reason="startup")

            with self._state_lock:
                self._initialized = True
                if out_ok and in_ok:
                    if self._input_status == STATUS_SYSTEM_DEFAULT or self._output_status == STATUS_SYSTEM_DEFAULT:
                        self._init_status = STATUS_SYSTEM_DEFAULT
                    else:
                        self._init_status = STATUS_CONNECTED
                elif out_ok or in_ok:
                    self._init_status = STATUS_SYSTEM_DEFAULT
                else:
                    self._init_status = STATUS_FAILED
                self._switching_status = "Idle"

            self._update_bluetooth_presence()
            self._notify_status()

            if start_monitor:
                self.start_monitor()

            return self.get_status()

    # ── Stream Open / Close Internals (always called under _switch_lock) ─────

    def _close_input_locked(self) -> None:
        st = self._in_stream
        self._in_stream = None
        if st is not None:
            try:
                _run_with_timeout(lambda: (st.stop(), st.close()), timeout_s=2.0)
            except Exception as exc:
                logger.debug("[AudioManager] Error closing input stream: %s", exc)
            finally:
                audio_devices.register_active_stream(-1)

    def _close_output_locked(self) -> None:
        with self._io_lock:
            st = self._out_stream
            self._out_stream = None
        if st is not None:
            try:
                _run_with_timeout(lambda: (st.stop(), st.close()), timeout_s=2.0)
            except Exception as exc:
                logger.debug("[AudioManager] Error closing output stream: %s", exc)
            finally:
                audio_devices.register_active_stream(-1)

    def _resolve_display_for_index(self, idx: Optional[int], fallback_name: str, kind: str) -> str:
        if idx is None:
            _ep_id, ep_name = audio_devices.get_current_default_endpoint(kind)
            return ep_name or fallback_name or audio_devices.DEFAULT_LABEL
        try:
            import sounddevice as sd

            info = sd.query_devices(idx)
            raw_name = (info.get("name") or "").strip()
            if fallback_name and fallback_name != audio_devices.DEFAULT_LABEL:
                return fallback_name
            return raw_name or audio_devices.DEFAULT_LABEL
        except Exception:
            return fallback_name or audio_devices.DEFAULT_LABEL

    def _open_input_locked(self, requested_name: str, reason: str = "switch") -> bool:
        import sounddevice as sd

        prev_in = self._active_input_name
        prev_out = self._active_output_name
        self._close_input_locked()

        wanted = (requested_name or "").strip()
        is_default_req = not wanted or wanted == audio_devices.DEFAULT_LABEL
        using_fallback = False

        # Determine target index & supported sample rate
        if is_default_req:
            target_idx = None
            hw_rate = audio_devices.find_supported_rate(None, "input", self._target_in_rate)
            chosen_label = self._resolve_display_for_index(None, audio_devices.DEFAULT_LABEL, "input")
        else:
            target_idx, hw_rate = audio_devices.resolve_with_rate(wanted, "input")
            if target_idx is None or hw_rate is None:
                # Requested device unavailable -> gracefully fall back to system default
                using_fallback = True
                target_idx = None
                hw_rate = audio_devices.find_supported_rate(None, "input", self._target_in_rate)
                chosen_label = self._resolve_display_for_index(None, audio_devices.DEFAULT_LABEL, "input")
            else:
                chosen_label = self._resolve_display_for_index(target_idx, wanted, "input")

        if hw_rate is None:
            err_msg = f"No usable sample rate found for input '{wanted or audio_devices.DEFAULT_LABEL}'"
            with self._state_lock:
                self._input_status = STATUS_FAILED
                self._last_error = err_msg
            self._emit_event(
                "input_init_failed",
                prev_in,
                wanted or audio_devices.DEFAULT_LABEL,
                prev_out,
                prev_out,
                "failed",
                sample_rate="none",
                failure_reason=err_msg,
            )
            return False

        # Compute hardware blocksize proportionally so callback cadence stays ~64 ms
        hw_blocksize = max(256, int(round(self._blocksize * (float(hw_rate) / float(self._target_in_rate)))))

        def _wrapped_callback(indata, frames, time_info, status) -> None:
            self._last_frame_received_ts = time.monotonic()
            cb = self._user_in_callback
            if cb is None:
                return
            try:
                if hw_rate != self._target_in_rate:
                    resampled = audio_devices.resample_pcm_int16(indata, hw_rate, self._target_in_rate)
                    cb(resampled, len(resampled), time_info, status)
                else:
                    cb(indata, frames, time_info, status)
            except Exception as cb_err:
                logger.debug("[AudioManager] Input callback error: %s", cb_err)

        def _create_and_start():
            st = sd.InputStream(
                samplerate=hw_rate,
                channels=1,
                dtype="int16",
                blocksize=hw_blocksize,
                device=target_idx,
                callback=_wrapped_callback,
            )
            st.start()
            return st

        ok, stream_or_none, err = _run_with_timeout(_create_and_start, timeout_s=self._open_timeout_s)

        # If specific device failed to start, attempt system default fallback once
        if not ok and target_idx is not None:
            using_fallback = True
            fallback_rate = audio_devices.find_supported_rate(None, "input", self._target_in_rate)
            if fallback_rate is not None:
                hw_rate = fallback_rate
                hw_blocksize = max(256, int(round(self._blocksize * (float(hw_rate) / float(self._target_in_rate)))))
                target_idx = None
                chosen_label = self._resolve_display_for_index(None, audio_devices.DEFAULT_LABEL, "input")
                ok, stream_or_none, err = _run_with_timeout(_create_and_start, timeout_s=self._open_timeout_s)

        if not ok or stream_or_none is None:
            err_msg = str(err or "Unknown input stream error")
            with self._state_lock:
                self._input_status = STATUS_FAILED
                self._last_error = err_msg
            self._emit_event(
                "input_init_failed",
                prev_in,
                chosen_label,
                prev_out,
                prev_out,
                "failed",
                sample_rate=f"{hw_rate} Hz",
                failure_reason=err_msg,
            )
            return False

        self._in_stream = stream_or_none
        audio_devices.register_active_stream(+1)

        with self._state_lock:
            self._active_input_idx = target_idx
            self._active_input_name = chosen_label
            self._hw_input_rate = hw_rate
            if using_fallback or is_default_req:
                self._input_status = STATUS_SYSTEM_DEFAULT if using_fallback else STATUS_CONNECTED
            else:
                self._input_status = STATUS_CONNECTED
            if not using_fallback:
                self._last_error = ""

        rate_desc = f"{hw_rate} Hz -> {self._target_in_rate} Hz" if hw_rate != self._target_in_rate else f"{hw_rate} Hz"
        self._emit_event(
            f"input_{reason}",
            prev_in,
            chosen_label,
            prev_out,
            prev_out,
            "fallback_default" if using_fallback else "success",
            sample_rate=rate_desc,
            failure_reason=(f"Requested '{wanted}' unavailable; fell back to system default" if using_fallback else ""),
            console_line=(
                f"[AudioManager] Input stream initialized successfully: {chosen_label} ({rate_desc})"
                if not using_fallback
                else f"[AudioManager] Requested input '{wanted}' unavailable — using system default: {chosen_label} ({rate_desc})"
            ),
        )
        return not using_fallback if (not is_default_req and reason == "switch") else True

    def _open_output_locked(self, requested_name: str, reason: str = "switch") -> bool:
        import sounddevice as sd

        prev_in = self._active_input_name
        prev_out = self._active_output_name
        self._close_output_locked()

        wanted = (requested_name or "").strip()
        is_default_req = not wanted or wanted == audio_devices.DEFAULT_LABEL
        using_fallback = False

        if is_default_req:
            target_idx = None
            hw_rate = audio_devices.find_supported_rate(None, "output", self._target_out_rate)
            chosen_label = self._resolve_display_for_index(None, audio_devices.DEFAULT_LABEL, "output")
        else:
            target_idx, hw_rate = audio_devices.resolve_with_rate(wanted, "output")
            if target_idx is None or hw_rate is None:
                using_fallback = True
                target_idx = None
                hw_rate = audio_devices.find_supported_rate(None, "output", self._target_out_rate)
                chosen_label = self._resolve_display_for_index(None, audio_devices.DEFAULT_LABEL, "output")
            else:
                chosen_label = self._resolve_display_for_index(target_idx, wanted, "output")

        if hw_rate is None:
            err_msg = f"No usable sample rate found for output '{wanted or audio_devices.DEFAULT_LABEL}'"
            with self._state_lock:
                self._output_status = STATUS_FAILED
                self._last_error = err_msg
            self._emit_event(
                "output_init_failed",
                prev_in,
                prev_in,
                prev_out,
                wanted or audio_devices.DEFAULT_LABEL,
                "failed",
                sample_rate="none",
                failure_reason=err_msg,
            )
            return False

        hw_blocksize = max(256, int(round(self._blocksize * (float(hw_rate) / float(self._target_out_rate)))))

        def _create_and_start():
            st = sd.RawOutputStream(
                samplerate=hw_rate,
                channels=1,
                dtype="int16",
                blocksize=hw_blocksize,
                device=target_idx,
            )
            st.start()
            return st

        ok, stream_or_none, err = _run_with_timeout(_create_and_start, timeout_s=self._open_timeout_s)

        if not ok and target_idx is not None:
            using_fallback = True
            fallback_rate = audio_devices.find_supported_rate(None, "output", self._target_out_rate)
            if fallback_rate is not None:
                hw_rate = fallback_rate
                hw_blocksize = max(256, int(round(self._blocksize * (float(hw_rate) / float(self._target_out_rate)))))
                target_idx = None
                chosen_label = self._resolve_display_for_index(None, audio_devices.DEFAULT_LABEL, "output")
                ok, stream_or_none, err = _run_with_timeout(_create_and_start, timeout_s=self._open_timeout_s)

        if not ok or stream_or_none is None:
            err_msg = str(err or "Unknown output stream error")
            with self._state_lock:
                self._output_status = STATUS_FAILED
                self._last_error = err_msg
            self._emit_event(
                "output_init_failed",
                prev_in,
                prev_in,
                prev_out,
                chosen_label,
                "failed",
                sample_rate=f"{hw_rate} Hz",
                failure_reason=err_msg,
            )
            return False

        with self._io_lock:
            self._out_stream = stream_or_none
        audio_devices.register_active_stream(+1)

        lat = 0.20
        try:
            raw_lat = float(getattr(stream_or_none, "latency", 0.0) or 0.0)
            if 0.0 < raw_lat < 1.0:
                lat = raw_lat
        except Exception:
            pass

        with self._state_lock:
            self._active_output_idx = target_idx
            self._active_output_name = chosen_label
            self._hw_output_rate = hw_rate
            self._output_latency = lat
            if using_fallback or is_default_req:
                self._output_status = STATUS_SYSTEM_DEFAULT if using_fallback else STATUS_CONNECTED
            else:
                self._output_status = STATUS_CONNECTED
            if not using_fallback:
                self._last_error = ""

        rate_desc = (
            f"{self._target_out_rate} Hz -> {hw_rate} Hz" if hw_rate != self._target_out_rate else f"{hw_rate} Hz"
        )
        self._emit_event(
            f"output_{reason}",
            prev_in,
            prev_in,
            prev_out,
            chosen_label,
            "fallback_default" if using_fallback else "success",
            sample_rate=rate_desc,
            failure_reason=(f"Requested '{wanted}' unavailable; fell back to system default" if using_fallback else ""),
            console_line=(
                f"[AudioManager] Output stream initialized successfully: {chosen_label} ({rate_desc})"
                if not using_fallback
                else f"[AudioManager] Requested output '{wanted}' unavailable — using system default: {chosen_label} ({rate_desc})"
            ),
        )
        return not using_fallback if (not is_default_req and reason == "switch") else True

    # ── Safe Playback Write (with automatic disconnect recovery) ─────────────

    def write_output(self, pcm_24k_bytes: bytes) -> bool:
        """Write 24 kHz mono int16 PCM bytes to the active speaker stream.

        Resamples transparently if the hardware speaker runs at another rate
        (e.g. 48 kHz Bluetooth A2DP). On device disconnect/error, catches the
        exception cleanly and triggers non-blocking fallback recovery without
        crashing the playback loop or tearing down Gemini Live.
        """
        if not pcm_24k_bytes:
            return True

        with self._io_lock:
            st = self._out_stream
            hw_rate = self._hw_output_rate

        if st is None:
            self._trigger_output_recovery("output stream missing")
            return False

        payload = pcm_24k_bytes
        if hw_rate != self._target_out_rate:
            try:
                import numpy as np

                pcm = np.frombuffer(pcm_24k_bytes, dtype=np.int16)
                resampled = audio_devices.resample_pcm_int16(pcm, self._target_out_rate, hw_rate)
                payload = resampled.tobytes()
            except Exception as exc:
                logger.warning("[AudioManager] Output resample error: %s", exc)
                return False

        try:
            with self._io_lock:
                if self._out_stream is not st or self._out_stream is None:
                    return False
                self._out_stream.write(payload)
            return True
        except Exception as exc:
            logger.warning("[AudioManager] Output write error (device may have disconnected): %s", exc)
            self._trigger_output_recovery(str(exc))
            return False

    def _trigger_output_recovery(self, reason: str) -> None:
        with self._state_lock:
            if self._recovering_output or self._shutdown_called:
                return
            self._recovering_output = True
            self._output_status = STATUS_SWITCHING
            self._switching_status = "Recovering output"
        self._notify_status()

        def _recover() -> None:
            try:
                with self._switch_lock:
                    if self._shutdown_called:
                        return
                    prev_out = self._active_output_name
                    self._emit_event(
                        "output_recovery",
                        self._active_input_name,
                        self._active_input_name,
                        prev_out,
                        audio_devices.DEFAULT_LABEL,
                        "recovering",
                        failure_reason=reason,
                    )
                    # Close both streams briefly if PortAudio needs a clean re-scan
                    had_input = self._in_stream is not None
                    cur_in = self._preferred_input
                    self._close_output_locked()
                    if had_input:
                        self._close_input_locked()
                    audio_devices.invalidate_cache(reset_portaudio=True)
                    self._open_output_locked(self._preferred_output, reason="recovery")
                    if had_input:
                        self._open_input_locked(cur_in, reason="recovery")
                    self._update_bluetooth_presence()
                    with self._state_lock:
                        self._switching_status = "Idle"
                    self._notify_status()
            finally:
                with self._state_lock:
                    self._recovering_output = False

        threading.Thread(target=_recover, daemon=True, name="audio-output-recover").start()

    # ── Safe Full PortAudio Rescan (closes streams FIRST, reopens cleanly) ───

    def _rescan_and_reopen_locked(
        self,
        target_input: Optional[str],
        target_output: Optional[str],
        reason: str = "hotplug",
    ) -> Tuple[bool, bool]:
        """Safely close active streams, reinitialize PortAudio while idle so newly
        plugged Bluetooth devices appear, and reopen verified input & output streams."""
        prev_in = self._active_input_name
        prev_out = self._active_output_name

        with self._state_lock:
            self._input_status = STATUS_SWITCHING
            self._output_status = STATUS_SWITCHING
            self._switching_status = f"Switching ({reason})"
        self._notify_status()

        print(f"[AudioManager] Input stream switching: {prev_in} -> {target_input or audio_devices.DEFAULT_LABEL}")
        print(f"[AudioManager] Output stream switching: {prev_out} -> {target_output or audio_devices.DEFAULT_LABEL}")

        # 1. Close streams FIRST so PortAudio has ZERO open streams
        self._close_input_locked()
        self._close_output_locked()

        # 2. Safe PortAudio reset while idle -> discovers hotplugged Bluetooth endpoints!
        audio_devices.invalidate_cache(reset_portaudio=True)
        audio_devices.list_devices("input")
        audio_devices.list_devices("output")

        # 3. Open output then input cleanly
        out_ok = self._open_output_locked(target_output or "", reason=reason)
        in_ok = self._open_input_locked(target_input or "", reason=reason)

        self._update_bluetooth_presence()
        with self._state_lock:
            self._switching_status = "Idle"
        self._notify_status()

        if out_ok or in_ok:
            print("[AudioManager] Audio transition completed")
            self._log_ui(
                f"SYS: Audio active — Mic: '{self._active_input_name}' ({self._hw_input_rate} Hz), "
                f"Speaker: '{self._active_output_name}' ({self._hw_output_rate} Hz)."
            )
        return in_ok, out_ok

    # ── Hybrid Bluetooth Architecture API & Stream Validation ────────────────

    def validate_input_active(self, timeout_s: float = 0.8) -> bool:
        """Validate that input PCM frames are actively being captured by the microphone stream."""
        start = time.monotonic()
        prev_ts = self._last_frame_received_ts
        while (time.monotonic() - start) < timeout_s:
            if self._in_stream is None:
                return False
            if self._last_frame_received_ts > prev_ts:
                return True
            time.sleep(0.04)
        return (self._last_frame_received_ts > prev_ts) or (
            self._in_stream is not None and getattr(self._in_stream, "active", False)
        )

    def wait_for_bluetooth_hfp_release(self, timeout_s: float = 2.0) -> bool:
        """Bounded wait after closing Bluetooth input for Windows to release HFP and re-establish A2DP."""
        start = time.monotonic()
        while (time.monotonic() - start) < timeout_s:
            stat = audio_devices.detect_a2dp_status(self._active_output_name, input_stream_open=False)
            if stat.get("a2dp_active", False):
                with self._state_lock:
                    self._a2dp_restored = True
                return True
            time.sleep(0.1)
        with self._state_lock:
            self._a2dp_restored = True
        return True

    def verify_and_recover_output(self, reason: str = "profile_switch") -> bool:
        """Check if output stream is healthy after a Bluetooth profile switch; reopens if disrupted."""
        with self._io_lock:
            st = self._out_stream
            active = bool(st is not None and getattr(st, "active", False))
        if active:
            return True
        with self._switch_lock:
            print(f"[AudioManager] Output stream disrupted ({reason}) — re-opening output...")
            ok = self._open_output_locked(self._preferred_output or self._active_output_name, reason=reason)
            return ok

    def switch_to_conversation_input(self, target_bt_mic: Optional[str] = None) -> Tuple[bool, str]:
        """
        Transition input stream to the Bluetooth microphone for conversation.
        Acquires _transition_lock, closes laptop mic, resolves BT mic, opens and validates.
        """
        with self._transition_lock:
            with self._state_lock:
                if (
                    self._active_input_role == "bluetooth"
                    and self._in_stream is not None
                    and getattr(self._in_stream, "active", False)
                ):
                    return True, self._active_input_name
                self._transition_state = "switching_to_conversation"
                self._switching_status = "Connecting Bluetooth mic..."
            self._notify_status()

            try:
                with self._switch_lock:
                    # 1. Resolve Bluetooth microphone endpoint FIRST before closing input
                    bt_dev = audio_devices.find_bluetooth_microphone(
                        target_bt_mic or self._bluetooth_mic_name or self._preferred_input
                    )
                    if not bt_dev:
                        print("[AudioManager] ⚠ Bluetooth microphone endpoint not found; keeping current input.")
                        if self._in_stream is None or not getattr(self._in_stream, "active", False):
                            self._open_input_locked(
                                self._builtin_mic_name or audio_devices.DEFAULT_LABEL, reason="hybrid_fallback"
                            )
                        with self._state_lock:
                            self._active_input_role = "builtin"
                            self._transition_state = "idle"
                            self._switching_status = "Idle"
                        self._notify_status()
                        return False, self._active_input_name

                    # 2. Close laptop built-in mic
                    self._close_input_locked()

                    # 3. Open Bluetooth microphone stream
                    ok = self._open_input_locked(bt_dev, reason="hybrid_conversation")
                    if not ok:
                        print(
                            f"[AudioManager] ⚠ Failed to open Bluetooth mic '{bt_dev}'; falling back to built-in mic."
                        )
                        self._open_input_locked(
                            self._builtin_mic_name or audio_devices.DEFAULT_LABEL, reason="hybrid_fallback"
                        )
                        with self._state_lock:
                            self._active_input_role = "builtin"
                            self._transition_state = "idle"
                            self._switching_status = "Idle"
                        self._notify_status()
                        return False, self._active_input_name

                    # 4. Validate incoming frames and verify output
                    self.validate_input_active(timeout_s=0.6)
                    self.verify_and_recover_output(reason="hybrid_conversation")

                    with self._state_lock:
                        self._active_input_role = "bluetooth"
                        self._transition_state = "idle"
                        self._switching_status = "Idle"
                    self._notify_status()

                    print(
                        f"[AudioManager] 🎧 Conversation mode active: using Bluetooth mic '{self._active_input_name}'"
                    )
                    return True, self._active_input_name
            finally:
                with self._state_lock:
                    if self._transition_state != "idle":
                        self._transition_state = "idle"
                        self._switching_status = "Idle"
                        self._notify_status()

    def switch_to_sleep_input(self, target_builtin_mic: Optional[str] = None) -> Tuple[bool, str]:
        """
        Transition input stream to the laptop built-in microphone for wake-word listening.
        Releases the Bluetooth microphone so Windows restores A2DP stereo music playback.
        """
        with self._transition_lock:
            with self._state_lock:
                if (
                    self._active_input_role == "builtin"
                    and self._in_stream is not None
                    and getattr(self._in_stream, "active", False)
                ):
                    return True, self._active_input_name
                self._transition_state = "switching_to_sleep"
                self._switching_status = "Releasing Bluetooth mic..."
            self._notify_status()

            try:
                with self._switch_lock:
                    # 1. Safely close Bluetooth microphone stream (drops HFP call mode!)
                    self._close_input_locked()

                    # 2. Bounded wait for Windows Bluetooth to tear down HFP and restore A2DP
                    self.wait_for_bluetooth_hfp_release(timeout_s=1.5)

                    # 3. Verify/recover output stream if profile switch shifted it
                    self.verify_and_recover_output(reason="hybrid_sleep")

                    # 4. Resolve laptop built-in microphone
                    builtin_dev = (
                        audio_devices.find_builtin_microphone(target_builtin_mic or self._builtin_mic_name)
                        or audio_devices.DEFAULT_LABEL
                    )

                    # 5. Open laptop built-in microphone stream
                    ok = self._open_input_locked(builtin_dev, reason="hybrid_sleep")
                    self.validate_input_active(timeout_s=0.6)

                    with self._state_lock:
                        self._active_input_role = "builtin"
                        self._transition_state = "idle"
                        self._switching_status = "Idle"
                    self._notify_status()

                    print(
                        f"[AudioManager] 😴 Sleep mode active: Bluetooth mic released (stereo music restored). "
                        f"Wake-word listening on '{self._active_input_name}'"
                    )
                    return ok, self._active_input_name
            finally:
                with self._state_lock:
                    if self._transition_state != "idle":
                        self._transition_state = "idle"
                        self._switching_status = "Idle"
                        self._notify_status()

    # ── Public Independent & Combined Device Switching API ───────────────────

    def switch_input(
        self,
        device_query: str,
        persist: bool = True,
        set_os_default: bool = False,
        rescan_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """Independently validate, switch, verify, and persist the microphone device."""
        with self._switch_lock:
            prev_in = self._active_input_name
            raw_q = (device_query or "").strip()

            with self._state_lock:
                self._input_status = STATUS_SWITCHING
                self._switching_status = "Switching microphone"
            self._notify_status()

            matched = audio_devices.match_device_name(raw_q, "input", refresh=False)
            if matched is None and rescan_if_missing and raw_q:
                # Rescan safely (closing streams briefly) in case BT just connected
                had_out = self._out_stream is not None
                cur_out = self._active_output_name if self._preferred_output else ""
                self._close_input_locked()
                if had_out:
                    self._close_output_locked()
                audio_devices.invalidate_cache(reset_portaudio=True)
                matched = audio_devices.match_device_name(raw_q, "input", refresh=False)
                if had_out:
                    self._open_output_locked(self._preferred_output or cur_out, reason="reopen")

            if matched is None:
                # Restore previous input if closed during rescan
                if self._in_stream is None:
                    self._open_input_locked(self._preferred_input, reason="restore")
                with self._state_lock:
                    self._switching_status = "Idle"
                self._notify_status()
                return {
                    "success": False,
                    "verified": False,
                    "device": prev_in,
                    "requested": raw_q,
                    "message": f"Microphone '{raw_q}' is not available or could not be opened. Still using '{self._active_input_name}'.",
                }

            if set_os_default and matched:
                audio_devices.set_default_endpoint(matched, "input")

            ok = self._open_input_locked(matched, reason="switch")
            if not ok:
                with self._state_lock:
                    self._switching_status = "Idle"
                self._notify_status()
                return {
                    "success": False,
                    "verified": False,
                    "device": self._active_input_name,
                    "requested": raw_q,
                    "message": (
                        f"Failed to open microphone '{matched or audio_devices.DEFAULT_LABEL}'. "
                        f"Active microphone is '{self._active_input_name}'."
                    ),
                }

            self._preferred_input = matched
            if persist:
                try:
                    from memory.config_manager import save_input_device

                    save_input_device(matched)
                except Exception as exc:
                    logger.warning("[AudioManager] Could not persist input device: %s", exc)

            self._update_bluetooth_presence()
            with self._state_lock:
                self._switching_status = "Idle"
            self._notify_status()

            return {
                "success": True,
                "verified": True,
                "device": self._active_input_name,
                "sample_rate": self._hw_input_rate,
                "status": self._input_status,
                "message": f"Switched microphone to '{self._active_input_name}' ({self._hw_input_rate} Hz).",
            }

    def switch_output(
        self,
        device_query: str,
        persist: bool = True,
        set_os_default: bool = False,
        rescan_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """Independently validate, switch, verify, and persist the speaker/output device."""
        with self._switch_lock:
            prev_out = self._active_output_name
            raw_q = (device_query or "").strip()

            with self._state_lock:
                self._output_status = STATUS_SWITCHING
                self._switching_status = "Switching speakers"
            self._notify_status()

            matched = audio_devices.match_device_name(raw_q, "output", refresh=False)
            if matched is None and rescan_if_missing and raw_q:
                had_in = self._in_stream is not None
                self._close_output_locked()
                if had_in:
                    self._close_input_locked()
                audio_devices.invalidate_cache(reset_portaudio=True)
                matched = audio_devices.match_device_name(raw_q, "output", refresh=False)
                if had_in:
                    self._open_input_locked(self._preferred_input, reason="reopen")

            if matched is None:
                if self._out_stream is None:
                    self._open_output_locked(self._preferred_output, reason="restore")
                with self._state_lock:
                    self._switching_status = "Idle"
                self._notify_status()
                return {
                    "success": False,
                    "verified": False,
                    "device": prev_out,
                    "requested": raw_q,
                    "message": f"Speaker/output '{raw_q}' is not available or could not be opened. Still using '{self._active_output_name}'.",
                }

            if set_os_default and matched:
                audio_devices.set_default_endpoint(matched, "output")

            ok = self._open_output_locked(matched, reason="switch")
            if not ok:
                with self._state_lock:
                    self._switching_status = "Idle"
                self._notify_status()
                return {
                    "success": False,
                    "verified": False,
                    "device": self._active_output_name,
                    "requested": raw_q,
                    "message": (
                        f"Failed to open speaker '{matched or audio_devices.DEFAULT_LABEL}'. "
                        f"Active speaker is '{self._active_output_name}'."
                    ),
                }

            self._preferred_output = matched
            if persist:
                try:
                    from memory.config_manager import save_output_device

                    save_output_device(matched)
                except Exception as exc:
                    logger.warning("[AudioManager] Could not persist output device: %s", exc)

            self._update_bluetooth_presence()
            with self._state_lock:
                self._switching_status = "Idle"
            self._notify_status()

            return {
                "success": True,
                "verified": True,
                "device": self._active_output_name,
                "sample_rate": self._hw_output_rate,
                "status": self._output_status,
                "message": f"Switched audio output to '{self._active_output_name}' ({self._hw_output_rate} Hz).",
            }

    def switch_devices(
        self,
        input_query: Optional[str] = None,
        output_query: Optional[str] = None,
        persist: bool = True,
        set_os_default: bool = False,
    ) -> Dict[str, Any]:
        """Switch input, output, or both devices cleanly in a single serialized transaction."""
        with self._switch_lock:
            out_res = (
                self.switch_output(output_query, persist=persist, set_os_default=set_os_default)
                if output_query is not None
                else None
            )
            in_res = (
                self.switch_input(input_query, persist=persist, set_os_default=set_os_default)
                if input_query is not None
                else None
            )
            ok = all(r["success"] for r in (out_res, in_res) if r is not None)
            return {
                "success": ok,
                "verified": ok,
                "input": in_res,
                "output": out_res,
                "status": self.get_status(),
            }

    def reset_to_default(self, persist: bool = True) -> Dict[str, Any]:
        """Reset both input and output devices to Windows system defaults."""
        return self.switch_devices(
            input_query=audio_devices.DEFAULT_VALUE,
            output_query=audio_devices.DEFAULT_VALUE,
            persist=persist,
        )

    def apply_saved_preferences(self) -> Dict[str, Any]:
        """Re-read config/api_keys.json preferences and hot-switch streams in-place."""
        from memory.config_manager import get_input_device, get_output_device

        return self.switch_devices(
            input_query=get_input_device(),
            output_query=get_output_device(),
            persist=False,
        )

    # ── Continuous Windows Audio & Bluetooth Monitoring ──────────────────────

    def _refresh_endpoint_baseline(self) -> None:
        self._last_default_out = audio_devices.get_current_default_endpoint("output")
        self._last_default_in = audio_devices.get_current_default_endpoint("input")
        self._last_snapshot = audio_devices.get_connected_endpoints_snapshot()

    def _update_bluetooth_presence(self) -> None:
        bt_dev = ""
        bt_active = False

        if audio_devices.is_bluetooth_device(self._active_output_name):
            bt_active = True
            bt_dev = self._active_output_name
        elif audio_devices.is_bluetooth_device(self._active_input_name):
            bt_active = True
            bt_dev = self._active_input_name
        else:
            # Check snapshot for any active Bluetooth endpoint
            for item in self._last_snapshot:
                if not item.startswith("{") and audio_devices.is_bluetooth_device(item):
                    bt_active = True
                    bt_dev = item
                    break

        with self._state_lock:
            self._bluetooth_connected = bt_active
            self._bluetooth_device = bt_dev

    def start_monitor(self) -> None:
        """Start the single background Windows audio/Bluetooth monitoring thread."""
        with self._state_lock:
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name="audio-device-monitor",
            )
            self._monitor_thread.start()

    def stop_monitor(self) -> None:
        """Stop the background audio monitor thread cleanly."""
        self._stop_event.set()
        t = self._monitor_thread
        self._monitor_thread = None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)

    def _monitor_loop(self) -> None:
        """Continuously monitor Windows default endpoint & Bluetooth connect/disconnect events."""
        while not self._stop_event.is_set():
            try:
                self._poll_device_changes_once()
            except Exception as exc:
                logger.warning("[AudioManager] Monitor tick error: %s", exc)
            self._stop_event.wait(self._poll_interval_s)

    def _poll_device_changes_once(self) -> bool:
        """Single poll cycle (also callable deterministically from unit tests).
        Returns True if an audio transition was executed."""
        cur_out = audio_devices.get_current_default_endpoint("output")
        cur_in = audio_devices.get_current_default_endpoint("input")
        cur_snap = audio_devices.get_connected_endpoints_snapshot()

        added = cur_snap - self._last_snapshot if self._last_snapshot else set()
        removed = self._last_snapshot - cur_snap if self._last_snapshot else set()

        default_out_changed = bool(cur_out[0] and self._last_default_out[0] and cur_out[0] != self._last_default_out[0])
        default_in_changed = bool(cur_in[0] and self._last_default_in[0] and cur_in[0] != self._last_default_in[0])

        # Detect Bluetooth device connection or disconnection
        bt_added = [item for item in added if not item.startswith("{") and audio_devices.is_bluetooth_device(item)]
        bt_removed = [item for item in removed if not item.startswith("{") and audio_devices.is_bluetooth_device(item)]

        # Detect user's preferred device reappearing or disappearing
        pref_out_reappeared = bool(
            self._preferred_output
            and any(
                audio_devices._fold(self._preferred_output) in audio_devices._fold(x)
                for x in added
                if not x.startswith("{")
            )
        )
        pref_in_reappeared = bool(
            self._preferred_input
            and any(
                audio_devices._fold(self._preferred_input) in audio_devices._fold(x)
                for x in added
                if not x.startswith("{")
            )
        )
        active_out_removed = bool(
            self._active_output_name
            and any(
                audio_devices._fold(self._active_output_name) in audio_devices._fold(x)
                for x in removed
                if not x.startswith("{")
            )
        )
        active_in_removed = bool(
            self._active_input_name
            and any(
                audio_devices._fold(self._active_input_name) in audio_devices._fold(x)
                for x in removed
                if not x.startswith("{")
            )
        )

        # Update baseline snapshots
        self._last_default_out = cur_out
        self._last_default_in = cur_in
        self._last_snapshot = cur_snap

        should_switch = (
            default_out_changed
            or default_in_changed
            or bool(bt_added)
            or bool(bt_removed)
            or pref_out_reappeared
            or pref_in_reappeared
            or active_out_removed
            or active_in_removed
        )
        if not should_switch:
            return False

        if bt_added:
            print(f"[AudioManager] Bluetooth device detected: {bt_added[0]}")
            self._log_ui(f"SYS: Bluetooth device detected: {bt_added[0]}")
        elif bt_removed:
            print(f"[AudioManager] Bluetooth device disconnected: {bt_removed[0]}")
            self._log_ui(f"SYS: Bluetooth device disconnected: {bt_removed[0]} — falling back to default audio.")

        # Brief settle window for Windows Bluetooth A2DP/HFP profile handoff
        if (bt_added or bt_removed) and self._bt_settle_s > 0:
            if self._stop_event.wait(self._bt_settle_s):
                return False

        with self._switch_lock:
            if self._shutdown_called:
                return False

            # Decide target devices:
            # - If user has a preferred device and it is available (or just reappeared), use it.
            # - If user is on system default (""), follow the new Windows default (including BT!).
            # - If BT was just connected and user didn't lock a specific non-BT device, prefer BT/default.
            target_out = self._preferred_output
            target_in = self._preferred_input

            if bt_added and not target_out:
                target_out = cur_out[1] or bt_added[0]
            if bt_added and not target_in:
                # If Windows switched default input to Bluetooth too, follow Windows default
                target_in = cur_in[1] or ""

            reason = (
                "bluetooth_connect"
                if bt_added
                else "bluetooth_disconnect"
                if bt_removed
                else "preferred_restored"
                if (pref_out_reappeared or pref_in_reappeared)
                else "default_changed"
            )
            self._rescan_and_reopen_locked(target_in, target_out, reason=reason)
            return True

    # ── Clean Shutdown ───────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Stop monitoring and cleanly close all active audio streams with zero leaks."""
        with self._switch_lock:
            self._shutdown_called = True
        self.stop_monitor()
        with self._switch_lock:
            self._close_input_locked()
            self._close_output_locked()
            with self._state_lock:
                self._input_status = STATUS_DISCONNECTED
                self._output_status = STATUS_DISCONNECTED
                self._init_status = STATUS_DISCONNECTED
                self._switching_status = "Idle"
                self._initialized = False
        self._notify_status()
        print("[AudioManager] Shutdown complete — all audio streams closed.")
