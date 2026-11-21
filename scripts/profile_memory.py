"""
scripts/profile_memory.py — Memory profiler and diagnostic report for JARVIS.

Measures:
1. Full process tree (main process + all children) with Private Bytes and Working Set.
2. Checkpoint memory progression through JARVIS startup sequence:
   - interpreter start
   - core imports
   - action & plugin discovery
   - UI initialization
   - wake word setup
   - session ready
3. Top 25 module imports ranked by memory cost (Native RSS delta and Python tracemalloc).
4. Emits a clean ranked table.
"""

from __future__ import annotations

import gc
import os
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psutil


def get_process_memory(proc: psutil.Process | None = None) -> Dict[str, float]:
    """Return working set (RSS) and private bytes in Megabytes."""
    if proc is None:
        proc = psutil.Process(os.getpid())
    try:
        mem_info = proc.memory_info()
        rss_mb = mem_info.rss / (1024 * 1024)
        # On Windows, try private bytes from full memory info, fall back to vms
        private_mb = rss_mb
        try:
            full_info = proc.memory_full_info()
            if hasattr(full_info, "private"):
                private_mb = full_info.private / (1024 * 1024)
            elif hasattr(full_info, "pagefile"):
                private_mb = full_info.pagefile / (1024 * 1024)
        except (psutil.AccessDenied, AttributeError):
            private_mb = getattr(mem_info, "vms", 0.0) / (1024 * 1024)
        return {"rss_mb": round(rss_mb, 2), "private_mb": round(private_mb, 2)}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {"rss_mb": 0.0, "private_mb": 0.0}


def get_tree_memory() -> Dict[str, Any]:
    """Measure main process and all child processes recursively."""
    main_proc = psutil.Process(os.getpid())
    main_mem = get_process_memory(main_proc)

    children_info = []
    total_rss = main_mem["rss_mb"]
    total_private = main_mem["private_mb"]

    try:
        children = main_proc.children(recursive=True)
        for child in children:
            try:
                cmem = get_process_memory(child)
                total_rss += cmem["rss_mb"]
                total_private += cmem["private_mb"]
                children_info.append({
                    "pid": child.pid,
                    "name": child.name(),
                    "rss_mb": cmem["rss_mb"],
                    "private_mb": cmem["private_mb"],
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass

    return {
        "main": {"pid": main_proc.pid, "name": main_proc.name(), **main_mem},
        "children": children_info,
        "total_rss_mb": round(total_rss, 2),
        "total_private_mb": round(total_private, 2),
    }


def measure_import(module_name: str) -> Dict[str, Any]:
    """Import a module while recording RSS delta and tracemalloc delta."""
    gc.collect()
    proc = psutil.Process(os.getpid())
    rss_before = proc.memory_info().rss / (1024 * 1024)
    tm_before, _ = tracemalloc.get_traced_memory()
    t0 = time.perf_counter()

    err = None
    try:
        __import__(module_name)
    except Exception as e:
        err = str(e)

    dt_ms = (time.perf_counter() - t0) * 1000
    gc.collect()
    rss_after = proc.memory_info().rss / (1024 * 1024)
    tm_after, tm_peak = tracemalloc.get_traced_memory()

    return {
        "module": module_name,
        "rss_delta_mb": round(max(0.0, rss_after - rss_before), 2),
        "tracemalloc_delta_mb": round(max(0.0, (tm_after - tm_before) / (1024 * 1024)), 2),
        "import_time_ms": round(dt_ms, 1),
        "error": err,
    }


def profile_top_imports() -> List[Dict[str, Any]]:
    """Measure heavy candidate imports in an isolated subprocess to prevent pollution."""
    candidates = [
        "PyQt6.QtWidgets",
        "PyQt6.QtGui",
        "PyQt6.QtCore",
        "PyQt6.QtMultimedia",
        "faster_whisper",
        "openwakeword",
        "fastapi",
        "uvicorn",
        "playwright",
        "pywinauto",
        "pyautogui",
        "PIL.Image",
        "cv2",
        "numpy",
        "torch",
        "google.genai",
        "pyaudio",
        "websockets",
        "cryptography",
        "httpx",
        "pydantic",
        "soundfile",
        "scipy",
        "rich",
        "core.permissions",
        "core.action_loader",
        "core.plugin_loader",
        "gui.display_windows",
        "gui.hud",
        "core.avatar",
        "dashboard.server",
    ]

    results = []
    import subprocess
    import json

    script = """
import sys, os, gc, time, tracemalloc, psutil, json
module_name = sys.argv[1]
gc.collect()
proc = psutil.Process(os.getpid())
rss_before = proc.memory_info().rss / (1024 * 1024)
tracemalloc.start()
t0 = time.perf_counter()
err = None
try:
    __import__(module_name)
except Exception as e:
    err = str(e)
dt_ms = (time.perf_counter() - t0) * 1000
gc.collect()
rss_after = proc.memory_info().rss / (1024 * 1024)
cur, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
print(json.dumps({
    "module": module_name,
    "rss_delta_mb": round(max(0.0, rss_after - rss_before), 2),
    "tracemalloc_delta_mb": round(cur / (1024 * 1024), 2),
    "peak_mb": round(peak / (1024 * 1024), 2),
    "import_time_ms": round(dt_ms, 1),
    "error": err
}))
"""

    for mod in candidates:
        try:
            res = subprocess.run(
                [sys.executable, "-c", script, mod],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                timeout=20,
            )
            for line in res.stdout.strip().splitlines():
                if line.startswith("{"):
                    results.append(json.loads(line))
                    break
        except Exception as e:
            results.append({"module": mod, "rss_delta_mb": 0.0, "tracemalloc_delta_mb": 0.0, "import_time_ms": 0.0, "error": str(e)})

    # Rank by RSS delta descending, then import time
    results.sort(key=lambda x: (x.get("rss_delta_mb", 0.0), x.get("tracemalloc_delta_mb", 0.0)), reverse=True)
    return results[:25]


def run_checkpoint_profiling() -> List[Dict[str, Any]]:
    """Profile sequential checkpoints in the live startup flow."""
    checkpoints = []

    def snap(name: str):
        gc.collect()
        tree = get_tree_memory()
        tm_cur, tm_peak = tracemalloc.get_traced_memory() if tracemalloc.is_tracing() else (0, 0)
        checkpoints.append({
            "checkpoint": name,
            "main_rss_mb": tree["main"]["rss_mb"],
            "main_private_mb": tree["main"]["private_mb"],
            "total_rss_mb": tree["total_rss_mb"],
            "total_private_mb": tree["total_private_mb"],
            "heap_mb": round(tm_cur / (1024 * 1024), 2),
            "peak_heap_mb": round(tm_peak / (1024 * 1024), 2),
        })

    tracemalloc.start()
    snap("1. Interpreter startup")

    # Checkpoint 2: Base Core Imports
    from memory import config_manager
    from core import outcome, permissions
    snap("2. Core foundation imports")

    # Checkpoint 3: Action & Plugin Discovery
    from core.action_loader import discover_actions
    from core.plugin_loader import discover_plugins
    act_reg = discover_actions(REPO_ROOT / "actions", logger=lambda _: None)
    plug_reg = discover_plugins(REPO_ROOT / "plugins", core_tool_names=act_reg.names(), logger=lambda _: None)
    snap("3. Action & plugin discovery")

    # Checkpoint 4: Qt & UI Module Imports
    os.environ["QT_QPA_PLATFORM"] = "offscreen"  # Headless-safe for profiling
    from PyQt6.QtWidgets import QApplication
    from gui.window import MainWindow
    from gui.hud import HudCanvas
    snap("4. UI & HUD classes loaded")

    # Checkpoint 5: QApplication & UI instantiated
    app = QApplication.instance() or QApplication(["jarvis_profile", "-platform", "offscreen"])
    win = MainWindow("")
    snap("5. UI Window initialized (offscreen)")

    # Checkpoint 6: Wake Word Detector initialized
    from core.wake_word import WakeWordDetector
    detector = WakeWordDetector(on_detect=lambda *_: None)
    snap("6. Wake word detector created")

    # Clean up UI
    try:
        win.close()
    except Exception:
        pass

    tracemalloc.stop()
    return checkpoints


def print_report():
    print("=" * 80)
    print(" JARVIS MEMORY PROFILING REPORT")
    print("=" * 80)

    print("\n[1/3] CHECKPOINT MEMORY PROGRESSION:")
    print("-" * 80)
    print(f"{'Checkpoint':<35} | {'RSS (MB)':<10} | {'Private (MB)':<12} | {'Heap (MB)':<10}")
    print("-" * 80)
    checkpoints = run_checkpoint_profiling()
    for cp in checkpoints:
        print(f"{cp['checkpoint']:<35} | {cp['main_rss_mb']:<10.2f} | {cp['main_private_mb']:<12.2f} | {cp['heap_mb']:<10.2f}")
    print("-" * 80)

    print("\n[2/3] TOP 25 MODULE IMPORTS RANKED BY MEMORY COST:")
    print("-" * 80)
    print(f"{'Rank':<5} | {'Module':<28} | {'RSS Δ (MB)':<11} | {'Heap Δ (MB)':<11} | {'Load Time':<10}")
    print("-" * 80)
    top_imports = profile_top_imports()
    for idx, row in enumerate(top_imports, 1):
        mod = row["module"]
        rss = f"{row.get('rss_delta_mb', 0.0):.2f} MB"
        heap = f"{row.get('tracemalloc_delta_mb', 0.0):.2f} MB"
        t_ms = f"{row.get('import_time_ms', 0.0):.0f} ms"
        err = f" (error: {row['error'][:20]})" if row.get("error") else ""
        print(f"{idx:<5} | {mod:<28} | {rss:<11} | {heap:<11} | {t_ms:<10}{err}")
    print("-" * 80)

    print("\n[3/3] PROCESS TREE SNAPSHOT:")
    print("-" * 80)
    tree = get_tree_memory()
    print(f"Main Process: PID {tree['main']['pid']} ({tree['main']['name']})")
    print(f"  Working Set (RSS): {tree['main']['rss_mb']:.2f} MB")
    print(f"  Private Bytes:     {tree['main']['private_mb']:.2f} MB")
    if tree["children"]:
        print(f"Child Processes ({len(tree['children'])}):")
        for ch in tree["children"]:
            print(f"  - PID {ch['pid']} ({ch['name']}): RSS {ch['rss_mb']:.2f} MB, Private {ch['private_mb']:.2f} MB")
    else:
        print("Child Processes: None")
    print(f"TOTAL WORKING SET:   {tree['total_rss_mb']:.2f} MB")
    print(f"TOTAL PRIVATE BYTES: {tree['total_private_mb']:.2f} MB")
    print("=" * 80)


if __name__ == "__main__":
    print_report()
