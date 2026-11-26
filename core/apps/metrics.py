"""
core/apps/metrics.py — High-Precision Performance & Flakiness Metrics Tracker.

Tracks:
- tool name
- path used: deep_link | selector | vision
- duration_ms
- verified: bool
- status: ok | failed | uncertain
- slowest and flakiest tool report
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("app_metrics")

_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "app_tool_metrics.jsonl"


@dataclass
class ToolExecutionMetric:
    tool: str
    path_used: str  # deep_link | selector | vision | media_key | direct
    duration_ms: float
    verified: bool
    status: str
    error: Optional[str] = None
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


class AppMetricsTracker:
    _lock = threading.Lock()
    _metrics: List[ToolExecutionMetric] = []

    @classmethod
    def record(
        cls,
        tool: str,
        path_used: str,
        duration_ms: float,
        verified: bool = True,
        status: str = "ok",
        error: Optional[str] = None,
    ) -> None:
        metric = ToolExecutionMetric(
            tool=tool,
            path_used=path_used,
            duration_ms=round(duration_ms, 2),
            verified=verified,
            status=status,
            error=error,
        )
        with cls._lock:
            cls._metrics.append(metric)
            if len(cls._metrics) > 500:
                cls._metrics = cls._metrics[-500:]

        # Append to log file
        try:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(metric)) + "\n")
        except Exception:
            pass

    @classmethod
    def get_report(cls) -> Dict[str, Any]:
        """Aggregate report showing slowest tools, flakiest tools, and path breakdown."""
        with cls._lock:
            items = list(cls._metrics)

        tool_durations: Dict[str, List[float]] = defaultdict(list)
        tool_counts: Dict[str, int] = defaultdict(int)
        tool_failures: Dict[str, int] = defaultdict(int)
        tool_unverified: Dict[str, int] = defaultdict(int)
        paths_count: Dict[str, int] = defaultdict(int)

        for m in items:
            tool_durations[m.tool].append(m.duration_ms)
            tool_counts[m.tool] += 1
            if m.status in ("failed", "error") or not m.verified:
                tool_failures[m.tool] += 1
            if not m.verified:
                tool_unverified[m.tool] += 1
            paths_count[m.path_used] += 1

        slowest = []
        for t, durs in tool_durations.items():
            avg_d = sum(durs) / len(durs)
            sorted_d = sorted(durs)
            p95 = sorted_d[int(len(sorted_d) * 0.95)] if sorted_d else avg_d
            slowest.append({"tool": t, "avg_ms": round(avg_d, 1), "p95_ms": round(p95, 1), "calls": len(durs)})
        slowest.sort(key=lambda x: x["avg_ms"], reverse=True)

        flakiest = []
        for t, total in tool_counts.items():
            fails = tool_failures.get(t, 0)
            rate = (fails / total) * 100.0 if total > 0 else 0.0
            flakiest.append({"tool": t, "failure_rate_pct": round(rate, 1), "failures": fails, "total": total})
        flakiest.sort(key=lambda x: x["failure_rate_pct"], reverse=True)

        return {
            "total_calls_tracked": len(items),
            "slowest_tools": slowest[:5],
            "flakiest_tools": flakiest[:5],
            "paths_used": dict(paths_count),
        }
