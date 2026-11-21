"""
core/tasks/budgets.py — Time, round, and early-finish budget tracking.

Design reference: Mark LVII (CC BY-NC 4.0).
Enforces hard bounds on background task duration and reasoning iterations,
triggering final report synthesis when budgets expire.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TaskBudget:
    time_budget_sec: float = 300.0
    round_budget: int = 10
    finish_early_requested: bool = False

    def is_exhausted(self, elapsed_sec: float, round_count: int) -> tuple[bool, str]:
        """Check if time or round limits have been reached, or finish was requested."""
        if self.finish_early_requested:
            return True, "Early finish requested by user"
        if round_count >= self.round_budget:
            return True, f"Round budget exhausted ({round_count}/{self.round_budget} rounds)"
        if elapsed_sec >= self.time_budget_sec:
            return True, f"Time budget exhausted ({elapsed_sec:.1f}s/{self.time_budget_sec:.1f}s)"
        return False, ""
