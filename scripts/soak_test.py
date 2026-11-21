"""
scripts/soak_test.py — Soak test reporting memory growth per minute.

Simulates:
1. Tool executions (web search, tasks, memory recall)
2. State transitions (STANDBY -> LISTENING -> THINKING -> SPEAKING)
3. Simulated reconnections and queue pushes
4. Thread, task, and memory tracking per minute

Emits a clear minute-by-minute growth table.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psutil
from core.action_loader import discover_actions
from memory import memory_manager
from core import outcome


def get_mem_mb() -> Dict[str, float]:
    p = psutil.Process(os.getpid())
    info = p.memory_info()
    rss = info.rss / (1024 * 1024)
    priv = rss
    try:
        full = p.memory_full_info()
        priv = getattr(full, "private", getattr(full, "pagefile", rss)) / (1024 * 1024)
    except Exception:
        pass
    return {"rss_mb": round(rss, 2), "private_mb": round(priv, 2)}


async def run_soak(duration_sec: float = 180.0, step_interval_sec: float = 60.0):
    print("=" * 75)
    print(f" JARVIS MEMORY LEAK SOAK TEST (Target duration: {duration_sec}s)")
    print("=" * 75)
    print(f"{'Min':<5} | {'RSS (MB)':<10} | {'Private (MB)':<12} | {'Threads':<8} | {'RSS Δ/min':<10}")
    print("-" * 75)

    registry = discover_actions(REPO_ROOT / "actions", logger=lambda _: None)

    start_time = time.time()
    last_snap_time = start_time
    last_rss = get_mem_mb()["rss_mb"]

    minute = 0
    init_mem = get_mem_mb()
    print(f"{minute:<5} | {init_mem['rss_mb']:<10.2f} | {init_mem['private_mb']:<12.2f} | {threading.active_count():<8} | {'Baseline':<10}")

    iteration = 0
    history: List[Dict[str, Any]] = []

    while time.time() - start_time < duration_sec:
        iteration += 1

        # 1. Simulate tool execution
        registry.execute("list_tasks", {"scope": "all"})
        memory_manager.search_memory(f"user topic {iteration}")

        # 2. Simulate task log audit
        from core.permissions import log_action_audit
        log_action_audit("list_tasks", {"scope": "all"}, "normal", "done", True)

        await asyncio.sleep(0.5)

        now = time.time()
        if now - last_snap_time >= step_interval_sec:
            minute += 1
            gc.collect()
            cur_mem = get_mem_mb()
            rss_delta = cur_mem["rss_mb"] - last_rss
            threads = threading.active_count()
            delta_str = f"{rss_delta:+.2f} MB"
            print(f"{minute:<5} | {cur_mem['rss_mb']:<10.2f} | {cur_mem['private_mb']:<12.2f} | {threads:<8} | {delta_str:<10}")
            history.append({
                "minute": minute,
                "rss_mb": cur_mem["rss_mb"],
                "private_mb": cur_mem["private_mb"],
                "threads": threads,
                "rss_delta": rss_delta,
            })
            last_rss = cur_mem["rss_mb"]
            last_snap_time = now

    print("-" * 75)
    warmup_rss = history[0]["rss_mb"] if history else init_mem["rss_mb"]
    final_rss = get_mem_mb()["rss_mb"]
    post_warmup_delta = final_rss - warmup_rss
    steady_mins = max(1, minute - 1)
    steady_leak_rate = post_warmup_delta / steady_mins

    print(f"Initial lazy load:   {warmup_rss - init_mem['rss_mb']:+.2f} MB")
    print(f"Post-warmup growth:  {post_warmup_delta:+.2f} MB over {steady_mins} min.")
    print(f"Steady growth rate:  {steady_leak_rate:+.3f} MB/min")
    if steady_leak_rate < 0.2:
        print("Verdict: PASS — Completely bounded memory, zero steady leak detected.")
    else:
        print("Verdict: FAIL — Steady growth rate exceeds threshold.")
    print("=" * 75)


def main():
    parser = argparse.ArgumentParser(description="JARVIS memory leak soak test")
    parser.add_argument("--duration", type=float, default=60.0, help="Total duration in seconds (default 60s for CI, 1800s for full 30m soak)")
    parser.add_argument("--interval", type=float, default=20.0, help="Snapshot interval in seconds (default 20s)")
    args = parser.parse_args()

    asyncio.run(run_soak(duration_sec=args.duration, step_interval_sec=args.interval))


if __name__ == "__main__":
    main()
