"""
actions/tool_metrics.py — Performance & Latency Analytics for App Tools.
"""

from __future__ import annotations

from core.apps.metrics import AppMetricsTracker
from core.apps.tool_result import ToolResult


def tool_performance_report(parameters: dict = None, **kwargs) -> str:
    """Generate summary report of slowest and flakiest tools across app workflows."""
    report = AppMetricsTracker.get_report()
    lines = [f"=== App Tool Performance Report ({report['total_calls_tracked']} calls logged) ==="]

    lines.append("\nTop Slowest Tools:")
    for s in report.get("slowest_tools", []):
        lines.append(f"- {s['tool']}: avg {s['avg_ms']}ms | p95 {s['p95_ms']}ms ({s['calls']} calls)")
    if not report.get("slowest_tools"):
        lines.append("- (No calls logged yet)")

    lines.append("\nTop Flakiest Tools:")
    for f in report.get("flakiest_tools", []):
        lines.append(f"- {f['tool']}: {f['failure_rate_pct']}% failure rate ({f['failures']}/{f['total']})")
    if not report.get("flakiest_tools"):
        lines.append("- (No calls logged yet)")

    lines.append("\nPaths Distribution:")
    for path, cnt in report.get("paths_used", {}).items():
        lines.append(f"- {path}: {cnt}")

    return ToolResult.ok("\n".join(lines), data=report, verified=True).to_action_result("tool_performance_report")


TOOL = {
    "name": "tool_performance_report",
    "description": "View latency, duration, and reliability report of app tools (slowest and flakiest tools).",
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": tool_performance_report,
    "permission": "read_only",
}
