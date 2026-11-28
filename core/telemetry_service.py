"""
core/telemetry_service.py - Real-Time System & AI Telemetry Service for JARVIS

Provides:
- Non-blocking background QThread hardware/system vitals polling (CPU, RAM, GPU/VRAM via NVML, Disks, Battery).
- 60-second rolling load history deque for multi-line graphs.
- Process Radar & Top 5 consumers aggregator (2.0s interval).
- Net Link throughput monitor (1.0s interval) with download/upload deltas.
- High-resolution 100ms clock and session uptime tracker.
- Thread-safe TelemetryEventBus (QObject) with Qt signals for UI decoupling.
"""

from __future__ import annotations

import collections
import datetime
import os
import platform
import sys
import threading
import time
import warnings
from typing import Any, Dict, List, Optional, Tuple

import psutil
from PyQt6.QtCore import QObject, QThread, pyqtSignal

# Defensive import of pynvml / nvidia-ml-py
_NVML_AVAILABLE = False
_NVML_HANDLE = None
_NVML_NAME = ""

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import pynvml

        pynvml.nvmlInit()
        device_count = pynvml.nvmlDeviceGetCount()
        if device_count > 0:
            _NVML_HANDLE = pynvml.nvmlDeviceGetHandleByIndex(0)
            raw_name = pynvml.nvmlDeviceGetName(_NVML_HANDLE)
            _NVML_NAME = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)
            _NVML_AVAILABLE = True
except Exception:
    _NVML_AVAILABLE = False
    _NVML_HANDLE = None
    _NVML_NAME = ""


class TelemetryEventBus(QObject):
    """
    Thread-safe event bus emitting real-time signals from telemetry collectors,
    AI execution pipeline, and tool dispatcher to UI components.
    """

    # Hardware & System Signals
    vitals_updated = pyqtSignal(dict)
    load_history_updated = pyqtSignal(list)
    processes_updated = pyqtSignal(list)
    net_updated = pyqtSignal(dict)
    clock_updated = pyqtSignal(dict)

    # AI Pipeline & Diagnostics Signals
    status_pill_updated = pyqtSignal(dict)
    response_perf_updated = pyqtSignal(dict)
    event_logged = pyqtSignal(dict)
    model_bench_logged = pyqtSignal(dict)
    action_logged = pyqtSignal(dict)
    error_alert = pyqtSignal(str)

    _instance: Optional["TelemetryEventBus"] = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "TelemetryEventBus":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._requests_counter = 0
        self._actions_counter = 0
        self._tokens_counter = 0
        self._current_model = "gemini-2.5-flash"
        self._current_agent_state = "IDLE"
        self._current_connection = "CORE ONLINE"
        self._current_brain_mode = "HYBRID"
        self._abort_badge = "STOP CTRL+ALT+BACKSPACE"

        # Model bench history: model_name -> {"latencies": list, "pass": int, "total": int, "tier": str}
        self._model_bench: Dict[str, Dict[str, Any]] = {
            "gemini-2.5-flash": {"latencies": [0.68, 0.74, 0.82], "pass": 16, "total": 16, "tier": "FAST"},
            "gemini-3.6-flash": {"latencies": [1.1, 1.25, 1.3], "pass": 15, "total": 16, "tier": "FAST"},
            "gemini-2.5-pro": {"latencies": [3.8, 4.1, 4.0], "pass": 14, "total": 16, "tier": "SMART"},
        }

    # ── Helpers for AI Pipeline & Dispatcher ───────────────────────────────────

    def emit_status_pill(
        self,
        connection: Optional[str] = None,
        agent_state: Optional[str] = None,
        brain_mode: Optional[str] = None,
        abort_badge: Optional[str] = None,
    ):
        if connection is not None:
            self._current_connection = connection
        if agent_state is not None:
            self._current_agent_state = agent_state
        if brain_mode is not None:
            self._current_brain_mode = brain_mode
        if abort_badge is not None:
            self._abort_badge = abort_badge

        self.status_pill_updated.emit({
            "connection": self._current_connection,
            "agent_state": self._current_agent_state,
            "brain_mode": self._current_brain_mode,
            "abort_badge": self._abort_badge,
        })

    def emit_event(self, tag: str, message: str, level: str = "info"):
        """Emit structured event entry for EVENT STREAM."""
        now_ts = datetime.datetime.now().strftime("%H:%M:%S")
        entry = {
            "timestamp": now_ts,
            "tag": str(tag).upper(),
            "message": str(message),
            "level": level,  # info, success, warning, error
        }
        self.event_logged.emit(entry)

    def emit_action(self, label: str, category: str = "TO", duration_ms: float = 0.0, success: bool = True):
        """Emit executed tool / action diagnostics entry for ACTIONS history."""
        self._actions_counter += 1
        now_ts = datetime.datetime.now().strftime("%H:%M:%S")
        entry = {
            "timestamp": now_ts,
            "checkmark": "✓" if success else "✗",
            "label": str(label),
            "category": str(category).upper(),
            "duration_ms": max(1, int(duration_ms)),
            "success": success,
        }
        self.action_logged.emit(entry)

    def emit_response_perf(
        self,
        model: Optional[str] = None,
        latency_ms: float = 0.0,
        requests_inc: int = 1,
        tokens: int = 0,
    ):
        """Emit response card performance header metadata."""
        if model:
            self._current_model = model
        self._requests_counter += requests_inc
        self._tokens_counter += tokens
        self.response_perf_updated.emit({
            "model": self._current_model,
            "latency_ms": int(latency_ms),
            "requests": self._requests_counter,
            "actions": self._actions_counter,
            "tokens": self._tokens_counter,
        })

    def record_model_benchmark(self, model: str, latency_sec: float, passed: bool, tier: str = "FAST"):
        """Record model response benchmark."""
        if model not in self._model_bench:
            self._model_bench[model] = {"latencies": [], "pass": 0, "total": 0, "tier": tier}
        mb = self._model_bench[model]
        mb["latencies"].append(float(latency_sec))
        if len(mb["latencies"]) > 20:
            mb["latencies"].pop(0)
        mb["total"] += 1
        if passed:
            mb["pass"] += 1
        mb["tier"] = tier

        avg_lat = sum(mb["latencies"]) / max(1, len(mb["latencies"]))
        pass_ratio = f"{mb['pass']}/{mb['total']} pass"
        self.model_bench_logged.emit({
            "model": model,
            "avg_latency": f"{avg_lat:.1f}s",
            "pass_ratio": pass_ratio,
            "tier": tier,
        })

    def emit_error(self, error_message: str):
        """Emit error alert banner."""
        self.emit_event("FAULT", error_message, level="error")
        self.error_alert.emit(str(error_message))


class TelemetryWorker(QThread):
    """
    Dedicated background worker QThread.
    Runs asynchronous timers and sensor collectors to poll system vitals,
    process radar, and net link throughput without stalling the Qt main thread.
    """

    def __init__(self, bus: TelemetryEventBus, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.bus = bus
        self._running = True
        self._session_start_time = time.time()

        # Rolling buffers
        self._load_buffer = collections.deque(maxlen=60)
        self._net_buffer = collections.deque(maxlen=60)

        # Network rate tracking
        self._last_net_bytes = None
        self._last_net_time = time.time()

        # Hardware probing state
        self._nvml_ok = _NVML_AVAILABLE
        self._nvml_handle = _NVML_HANDLE

    def stop(self):
        self._running = False
        self.wait(1500)

    def run(self):
        # Warmup psutil cpu measurement
        psutil.cpu_percent(interval=None)

        last_1s_tick = 0.0
        last_2s_tick = 0.0

        while self._running:
            now = time.time()

            # ── 100ms Clock & Session Uptime Tracker ──
            now_dt = datetime.datetime.now()
            tenths = int(now_dt.microsecond / 100000)
            time_str = now_dt.strftime("%H:%M:%S") + f".{tenths}"
            date_str = now_dt.strftime("%a, %d %b, %Y").upper()

            elapsed_sec = int(now - self._session_start_time)
            hh = elapsed_sec // 3600
            mm = (elapsed_sec % 3600) // 60
            ss = elapsed_sec % 60
            uptime_str = f"{hh:02d}:{mm:02d}:{ss:02d}"

            self.bus.clock_updated.emit({
                "time_str": time_str,
                "date_str": date_str,
                "uptime_str": uptime_str,
                "elapsed_sec": elapsed_sec,
            })

            # ── 1.0s System Vitals & Net Link Collector ──
            if now - last_1s_tick >= 1.0:
                last_1s_tick = now
                vitals = self._collect_vitals()
                net_info = self._collect_net_throughput()

                # Record 60-second rolling load buffer: (timestamp, cpu_pct, mem_pct, gpu_pct)
                gpu_p = vitals.get("gpu_pct", 0.0)
                if gpu_p is None or gpu_p < 0:
                    gpu_p = 0.0
                self._load_buffer.append((
                    now,
                    vitals.get("cpu_pct", 0.0),
                    vitals.get("mem_pct", 0.0),
                    gpu_p,
                ))

                self.bus.vitals_updated.emit(vitals)
                self.bus.net_updated.emit(net_info)
                self.bus.load_history_updated.emit(list(self._load_buffer))

            # ── 2.0s Process Radar & Top Consumers Collector ──
            if now - last_2s_tick >= 2.0:
                last_2s_tick = now
                top_procs = self._collect_top_processes()
                self.bus.processes_updated.emit(top_procs)

            # Sleep 100ms for next tick
            time.sleep(0.10)

    # ── Collectors ────────────────────────────────────────────────────────────

    def _collect_vitals(self) -> Dict[str, Any]:
        """Collect CPU, RAM, GPU/VRAM, Storage drives, and Battery vitals."""
        # 1. CPU
        try:
            cpu_pct = float(psutil.cpu_percent(interval=None))
            cpu_percore = [float(x) for x in psutil.cpu_percent(percpu=True)]
            cf = psutil.cpu_freq()
            cpu_freq_mhz = float(cf.current) if cf and cf.current else 0.0
        except Exception:
            cpu_pct = 0.0
            cpu_percore = []
            cpu_freq_mhz = 0.0

        # 2. RAM
        try:
            vm = psutil.virtual_memory()
            mem_used_gb = vm.used / (1024**3)
            mem_total_gb = vm.total / (1024**3)
            mem_pct = float(vm.percent)
            mem_formatted = f"{mem_used_gb:.1f} / {mem_total_gb:.1f} GB"
        except Exception:
            mem_used_gb = 0.0
            mem_total_gb = 0.0
            mem_pct = 0.0
            mem_formatted = "0.0 / 0.0 GB"

        # 3. GPU & VRAM via NVML
        gpu_pct = -1.0
        gpu_temp_c = -1.0
        gpu_vram_used_gb = -1.0
        gpu_vram_total_gb = -1.0
        gpu_vram_pct = -1.0
        gpu_name = _NVML_NAME

        if self._nvml_ok and self._nvml_handle:
            try:
                import pynvml
                util = pynvml.nvmlDeviceGetUtilizationRates(self._nvml_handle)
                gpu_pct = float(util.gpu)
                gpu_temp_c = float(pynvml.nvmlDeviceGetTemperature(self._nvml_handle, pynvml.NVML_TEMPERATURE_GPU))
                mem = pynvml.nvmlDeviceGetMemoryInfo(self._nvml_handle)
                gpu_vram_used_gb = mem.used / (1024**3)
                gpu_vram_total_gb = mem.total / (1024**3)
                gpu_vram_pct = (mem.used / mem.total * 100.0) if mem.total > 0 else 0.0
            except Exception:
                # NVML may fail if GPU is asleep in D3Cold
                pass

        # 4. Storage (All mounted fixed drives)
        disks = []
        try:
            partitions = psutil.disk_partitions(all=False)
            seen_mounts = set()
            for part in partitions:
                mount = part.mountpoint
                if mount in seen_mounts:
                    continue
                # On Windows, keep fixed drives (C:, D:, etc.)
                if os.name == "nt" and ("cdrom" in part.opts.lower() or not part.fstype):
                    continue
                try:
                    usage = psutil.disk_usage(mount)
                    seen_mounts.add(mount)
                    free_gb = usage.free / (1024**3)
                    total_gb = usage.total / (1024**3)
                    used_gb = usage.used / (1024**3)
                    pct = float(usage.percent)
                    # Clean label, e.g. "C:" or "/dev/sda1"
                    label = mount.rstrip("\\").rstrip("/") or mount
                    disks.append({
                        "mount": mount,
                        "label": label,
                        "free_gb": free_gb,
                        "used_gb": used_gb,
                        "total_gb": total_gb,
                        "pct": pct,
                        "formatted": f"{int(free_gb)} GB free",
                    })
                except (PermissionError, OSError):
                    pass
        except Exception:
            pass

        # 5. Battery & Power State
        battery_pct = None
        battery_plugged = None
        battery_formatted = "N/A"
        try:
            sb = psutil.sensors_battery()
            if sb is not None:
                battery_pct = float(sb.percent)
                battery_plugged = bool(sb.power_plugged)
                plug_label = "AC" if battery_plugged else "BAT"
                battery_formatted = f"{int(battery_pct)}% {plug_label}"
        except Exception:
            pass

        return {
            "cpu_pct": cpu_pct,
            "cpu_percore": cpu_percore,
            "cpu_freq_mhz": cpu_freq_mhz,
            "mem_used_gb": mem_used_gb,
            "mem_total_gb": mem_total_gb,
            "mem_pct": mem_pct,
            "mem_formatted": mem_formatted,
            "gpu_pct": gpu_pct,
            "gpu_temp_c": gpu_temp_c,
            "gpu_vram_used_gb": gpu_vram_used_gb,
            "gpu_vram_total_gb": gpu_vram_total_gb,
            "gpu_vram_pct": gpu_vram_pct,
            "gpu_name": gpu_name,
            "disks": disks,
            "battery_pct": battery_pct,
            "battery_plugged": battery_plugged,
            "battery_formatted": battery_formatted,
        }

    def _collect_net_throughput(self) -> Dict[str, Any]:
        """Calculate network throughput delta in bytes/sec and push to history."""
        now = time.time()
        dl_rate = 0.0
        ul_rate = 0.0

        try:
            counters = psutil.net_io_counters()
            if self._last_net_bytes is not None:
                dt = max(0.1, now - self._last_net_time)
                dl_delta = max(0, counters.bytes_recv - self._last_net_bytes[0])
                ul_delta = max(0, counters.bytes_sent - self._last_net_bytes[1])
                dl_rate = dl_delta / dt
                ul_rate = ul_delta / dt

            self._last_net_bytes = (counters.bytes_recv, counters.bytes_sent)
            self._last_net_time = now
        except Exception:
            pass

        def _fmt_rate(rate_bytes: float, arrow: str) -> str:
            kb = rate_bytes / 1024.0
            if kb < 1024.0:
                return f"{arrow} {int(kb)} kb/s"
            mb = kb / 1024.0
            return f"{arrow} {mb:.1f} MB/s"

        dl_formatted = _fmt_rate(dl_rate, "↓")
        ul_formatted = _fmt_rate(ul_rate, "↑")

        self._net_buffer.append((now, dl_rate, ul_rate))

        return {
            "dl_rate_bps": dl_rate,
            "ul_rate_bps": ul_rate,
            "dl_formatted": dl_formatted,
            "ul_formatted": ul_formatted,
            "net_history": list(self._net_buffer),
        }

    def _collect_top_processes(self) -> List[Dict[str, Any]]:
        """Query processes, aggregate multi-process instances, and sort top 5 by RSS."""
        from collections import defaultdict
        proc_map = defaultdict(lambda: {"rss": 0, "cpu": 0.0, "count": 0, "pids": []})

        for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
            try:
                info = p.info
                name = info.get("name") or "unknown"
                mem = info.get("memory_info")
                rss = mem.rss if mem else 0
                cpu = info.get("cpu_percent") or 0.0
                pid = info.get("pid")

                proc_map[name]["rss"] += rss
                proc_map[name]["cpu"] += cpu
                proc_map[name]["count"] += 1
                if pid:
                    proc_map[name]["pids"].append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        top5 = sorted(proc_map.items(), key=lambda x: x[1]["rss"], reverse=True)[:5]
        result = []
        import math

        for idx, (name, data) in enumerate(top5):
            rss_bytes = data["rss"]
            rss_mb = rss_bytes / (1024**2)
            rss_gb = rss_bytes / (1024**3)
            ram_str = f"{rss_gb:.1f} GB" if rss_gb >= 1.0 else f"{int(rss_mb)} MB"
            cpu_val = data["cpu"]

            # Map radar coordinates (angle based on index, radius based on resource load)
            angle_rad = (idx / 5.0) * 2.0 * math.pi
            # Normalized radius 0.2 to 0.85
            norm_r = min(0.85, max(0.25, (rss_mb / 2048.0) * 0.6 + (cpu_val / 100.0) * 0.25))

            result.append({
                "name": name,
                "ram_bytes": rss_bytes,
                "ram_formatted": ram_str,
                "cpu_pct": cpu_val,
                "count": data["count"],
                "angle_rad": angle_rad,
                "radius_norm": norm_r,
            })

        return result


class TelemetryService:
    """
    Central singleton service controlling the TelemetryWorker thread
    and exposing the TelemetryEventBus.
    """

    _instance: Optional["TelemetryService"] = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "TelemetryService":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self.bus = TelemetryEventBus.get_instance()
        self.worker: Optional[TelemetryWorker] = None
        self._started = False

    def start(self):
        """Start the background collector thread."""
        with self._lock:
            if self._started and self.worker and self.worker.isRunning():
                return
            self.worker = TelemetryWorker(self.bus)
            self.worker.start()
            self._started = True

    def stop(self):
        """Stop background worker."""
        with self._lock:
            if self.worker:
                self.worker.stop()
                self.worker = None
            self._started = False

    def is_running(self) -> bool:
        return self._started and self.worker is not None and self.worker.isRunning()


def get_telemetry_service() -> TelemetryService:
    return TelemetryService.get_instance()


def get_event_bus() -> TelemetryEventBus:
    return TelemetryEventBus.get_instance()
