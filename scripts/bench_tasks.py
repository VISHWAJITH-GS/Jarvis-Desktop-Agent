"""
scripts/bench_tasks.py — Background Task Engine Performance Benchmark.

Measures:
1. Startup cost: import time and initialization latency (ms)
2. Submit latency: time to enqueue a task (ms, avg and p95)
3. Queue wait: time from enqueue until worker picks up the task (ms)
4. Per-step overhead: execution harness overhead per round (ms)
5. Idle CPU / RAM: CPU utilization (%) and resident memory (MB) when queue is empty

Usage:
    python scripts/bench_tasks.py [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

# Ensure repository root is on sys.path
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))


def measure_startup_cost() -> tuple[float, float]:
    """Measure module import time and TaskEngine initialization time in milliseconds."""
    # Measure import time in a subprocess or fresh time
    t0 = time.perf_counter()
    import core.tasks.engine as _e
    import core.tasks.store as _s
    t_import = (time.perf_counter() - t0) * 1000.0

    temp_dir = tempfile.mkdtemp()
    try:
        db_path = Path(temp_dir) / "bench_init.db"
        t1 = time.perf_counter()
        store = _s.TaskStore(db_path)
        engine = _e.TaskEngine(store=store, workspace_base=Path(temp_dir) / "ws", poll_interval=0.01)
        t_init = (time.perf_counter() - t1) * 1000.0
        return t_import, t_init
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


def measure_idle_resources(duration_sec: float = 1.5) -> tuple[float, float]:
    """Measure CPU % and RAM (MB) while TaskEngine is running with an empty queue."""
    import psutil
    from core.tasks.engine import TaskEngine
    from core.tasks.store import TaskStore
    import shutil

    temp_dir = tempfile.mkdtemp()
    try:
        db_path = Path(temp_dir) / "bench_idle.db"
        store = TaskStore(db_path)
        engine = TaskEngine(store=store, workspace_base=Path(temp_dir) / "ws", poll_interval=0.05)
        engine.start()

        process = psutil.Process(os.getpid())
        # Prime psutil cpu_percent
        process.cpu_percent(interval=None)

        time.sleep(duration_sec)
        cpu_pct = process.cpu_percent(interval=None)
        mem_info = process.memory_info()
        ram_mb = mem_info.rss / (1024 * 1024)

        engine.stop()
        return cpu_pct, ram_mb
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def measure_latency_and_overhead(num_tasks: int = 10) -> tuple[float, float, float, float]:
    """
    Measures submit latency, queue wait time, and per-step overhead.
    Returns: (avg_submit_ms, p95_submit_ms, avg_queue_wait_ms, avg_step_overhead_ms)
    """
    from core.tasks.engine import TaskEngine
    from core.tasks.store import TaskStore
    import shutil

    temp_dir = tempfile.mkdtemp()
    try:
        db_path = Path(temp_dir) / "bench_lat.db"
        store = TaskStore(db_path)
        engine = TaskEngine(store=store, workspace_base=Path(temp_dir) / "ws", poll_interval=0.01)

        # Mock gemini call to return immediate finish with no network delay
        mock_reply = {
            "thought": "bench round reasoning",
            "action": "finish",
            "findings": ["bench finding"],
            "final_answer": "done",
        }

        submit_latencies = []
        queue_waits = []
        step_overheads = []

        # Start engine
        engine.start()

        with patch("core.gemini.as_json", return_value=mock_reply):
            for i in range(num_tasks):
                task_id = f"bench_task_{i}_{int(time.time()*1000)}"
                t_enq_start = time.perf_counter()
                task = engine.queue_task(
                    task_id=task_id,
                    title=f"Bench Task {i}",
                    description="Performance evaluation task",
                    time_budget_sec=60.0,
                    round_budget=2,
                )
                t_enq_end = time.perf_counter()
                submit_latencies.append((t_enq_end - t_enq_start) * 1000.0)

                # Poll store until task starts running or finishes to capture queue wait
                wait_start = time.perf_counter()
                while time.perf_counter() - wait_start < 5.0:
                    rec = store.get_task(task_id)
                    if rec and rec.state in ("running", "done"):
                        q_wait = (time.perf_counter() - t_enq_start) * 1000.0
                        queue_waits.append(q_wait)
                        break
                    time.sleep(0.002)

                # Wait until task completes
                comp_start = time.perf_counter()
                while time.perf_counter() - comp_start < 5.0:
                    rec = store.get_task(task_id)
                    if rec and rec.state == "done":
                        # Record elapsed execution overhead
                        step_overheads.append(rec.elapsed_sec * 1000.0)
                        break
                    time.sleep(0.002)

        engine.stop()

        submit_latencies.sort()
        avg_submit = sum(submit_latencies) / len(submit_latencies) if submit_latencies else 0.0
        p95_idx = int(len(submit_latencies) * 0.95)
        p95_submit = submit_latencies[min(p95_idx, len(submit_latencies) - 1)] if submit_latencies else 0.0

        avg_queue_wait = sum(queue_waits) / len(queue_waits) if queue_waits else 0.0
        avg_step_overhead = sum(step_overheads) / len(step_overheads) if step_overheads else 0.0

        return avg_submit, p95_submit, avg_queue_wait, avg_step_overhead
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_benchmark() -> dict:
    t_import, t_init = measure_startup_cost()
    cpu_idle, ram_idle = measure_idle_resources()
    avg_submit, p95_submit, avg_queue_wait, avg_step_overhead = measure_latency_and_overhead(num_tasks=10)

    results = {
        "startup_cost_ms": round(t_import + t_init, 2),
        "import_latency_ms": round(t_import, 2),
        "init_latency_ms": round(t_init, 2),
        "submit_latency_avg_ms": round(avg_submit, 2),
        "submit_latency_p95_ms": round(p95_submit, 2),
        "queue_wait_avg_ms": round(avg_queue_wait, 2),
        "per_step_overhead_ms": round(avg_step_overhead, 2),
        "idle_cpu_percent": round(cpu_idle, 2),
        "idle_ram_mb": round(ram_idle, 2),
    }
    return results


def main():
    parser = argparse.ArgumentParser(description="Task Engine Benchmark")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    args = parser.parse_args()

    results = run_benchmark()

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print("\n" + "=" * 60)
        print("  JARVIS BACKGROUND TASK ENGINE BENCHMARK")
        print("=" * 60)
        print(f"  Startup Cost (Import + Init):  {results['startup_cost_ms']:.2f} ms")
        print(f"    - Import Latency:            {results['import_latency_ms']:.2f} ms")
        print(f"    - Engine Init Latency:       {results['init_latency_ms']:.2f} ms")
        print(f"  Submit Latency (Avg):          {results['submit_latency_avg_ms']:.2f} ms")
        print(f"  Submit Latency (p95):          {results['submit_latency_p95_ms']:.2f} ms")
        print(f"  Queue Wait Latency (Avg):      {results['queue_wait_avg_ms']:.2f} ms")
        print(f"  Per-Step Overhead (Avg):       {results['per_step_overhead_ms']:.2f} ms")
        print(f"  Idle CPU Usage:                {results['idle_cpu_percent']:.2f} %")
        print(f"  Idle RAM Usage:                {results['idle_ram_mb']:.2f} MB")
        print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
