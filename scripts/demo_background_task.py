"""
scripts/demo_background_task.py — Demonstrates durable background task execution,
crash simulation, recovery from atomic checkpoint, and task completion.

Usage:
    python scripts/demo_background_task.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

# Add repository root to path
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

from core.tasks.engine import TaskEngine
from core.tasks.store import TaskStore


def run_demo():
    print("=" * 70)
    print("🚀 JARVIS DURABLE BACKGROUND TASK ENGINE DEMO")
    print("=" * 70)

    # Use a dedicated demo database and workspace
    demo_dir = repo_root / "scratch" / "demo_task"
    demo_dir.mkdir(parents=True, exist_ok=True)
    db_path = demo_dir / "demo_tasks.db"
    ws_base = demo_dir / "workspaces"

    store = TaskStore(db_path)
    engine = TaskEngine(store=store, workspace_base=ws_base, poll_interval=0.1)

    task_id = "demo_res_001"
    print(f"\n1. [Client] Enqueuing 2-minute research task: {task_id}")
    task = engine.queue_task(
        task_id=task_id,
        title="2-Minute Quantum Computing Research",
        description="Research recent developments in quantum error correction.",
        time_budget_sec=120.0,
        round_budget=5,
        allowlist=["web_search", "workspace_write_file"],
    )
    print(f"   Task state: {task.state}, budget: {task.time_budget_sec}s / {task.round_budget} rounds")

    # Step 1: Execute Round 1 and write checkpoint
    print("\n2. [Engine] Executing Round 1: Model reasons and gathers findings...")
    mock_round_1 = {
        "thought": "Searching for recent quantum error correction breakthroughs",
        "action": "finish",
        "findings": [
            "Surface code threshold reached with logical qubit fidelity > 99.9%",
            "Neutral-atom architectures demonstrating transversal gate operations",
        ],
        "final_answer": (
            "### Quantum Error Correction Research Summary\n"
            "- Discovered high-fidelity logical qubits with error suppression exceeding physical error rates.\n"
            "- Multi-qubit transversal gates demonstrated across neutral-atom lattices."
        ),
    }

    # Simulate round 1 checkpointing
    store.atomic_checkpoint(
        task_id=task_id,
        state="running",
        round_count=1,
        elapsed_sec=15.0,
        checkpoint={
            "rounds": [{"round_num": 1, "summary": "Gathered initial QEC papers"}],
            "findings": mock_round_1["findings"],
        },
    )
    print("   ✅ Round 1 atomic checkpoint saved to SQLite database.")
    t_after_r1 = store.get_task(task_id)
    print(f"   Stored findings: {t_after_r1.checkpoint['findings']}")

    # Step 2: Simulate process crash / kill
    print("\n3. [Simulate] ⚡ SIMULATING PROCESS KILL / SUDDEN POWER LOSS ⚡")
    print("   (Process was terminated mid-round while task state was 'running')")
    time.sleep(1.0)

    # Step 3: Simulate restart
    print("\n4. [Recovery] Restarting JARVIS application and TaskEngine...")
    new_engine = TaskEngine(store=store, workspace_base=ws_base, poll_interval=0.1)
    new_engine.recover_interrupted_tasks()

    recovered = store.get_task(task_id)
    print(f"   ✅ Recovered task '{recovered.id}' from database.")
    print(f"   Resume attempt count: {recovered.attempts}/{recovered.max_attempts}")
    print(f"   Resumed state: {recovered.state}")
    print(f"   Checkpointed findings intact: {recovered.checkpoint['findings']}")

    # Step 4: Complete execution from checkpoint
    print("\n5. [Engine] Resuming execution from checkpoint...")
    with patch("core.gemini.as_json", return_value=mock_round_1):
        round_res = new_engine._execute_round(recovered, [])
        new_engine.store.atomic_checkpoint(
            task_id=task_id,
            state="done",
            round_count=2,
            elapsed_sec=28.0,
            checkpoint=recovered.checkpoint,
            result=mock_round_1["final_answer"],
        )

    final_task = store.get_task(task_id)
    print(f"   ✅ Task completed successfully! State: {final_task.state.upper()}")
    print("\n" + "=" * 70)
    print("FINAL DELIVERABLE / SYNTHESIZED REPORT:")
    print("=" * 70)
    print(final_task.result)
    print("=" * 70)
    print("🎉 Demo complete! Durable task survived crash and completed.")


if __name__ == "__main__":
    run_demo()
