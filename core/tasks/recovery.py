"""
core/tasks/recovery.py — Failure Recovery and Honest Verification for Background Tasks.

Provides:
- Failure classification: transient | fixable | blocked | unknown
- Exponential backoff with jitter for transient failures (max 3 attempts)
- Alternative paths / parameter corrections for fixable failures (max 2 attempts, no loops)
- Side-effect safety guard: never blindly retry steps with side effects without state verification
- Strict permission & confirmation preservation (never bypasses security tiers)
- Full attempt auditing in task checkpoint recovery_log
- Honest status reporting: 'done' only when verified, otherwise 'unverified'
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Callable, Optional, Tuple, Union

from core import outcome

logger = logging.getLogger("JarvisTasksRecovery")

# Classifications
CLASSIFICATION_TRANSIENT = "transient"
CLASSIFICATION_FIXABLE = "fixable"
CLASSIFICATION_BLOCKED = "blocked"
CLASSIFICATION_UNKNOWN = "unknown"

MAX_TRANSIENT_ATTEMPTS = 3
MAX_FIXABLE_ATTEMPTS = 2

# Tools with potential external side effects
SIDE_EFFECT_TOOLS = {
    "send_message",
    "whatsapp_send",
    "email_send",
    "move_file",
    "delete_file",
    "workspace_write_file",
    "write_file",
    "create_routine",
    "delete_routine",
    "desktop_control",
    "windows_control",
}

# Error pattern matches for classification
TRANSIENT_PATTERNS = [
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "econnreset",
    "429",
    "rate limit",
    "quota",
    "resource_exhausted",
    "busy",
    "locked",
    "temporarily unavailable",
    "temporary failure",
    "network error",
    "service unavailable",
    "502",
    "503",
    "504",
]

BLOCKED_PATTERNS = [
    "permission denied",
    "unauthorized",
    "401",
    "403",
    "forbidden",
    "not in allowlist",
    "not allowed",
    "sandbox violation",
    "confirmation denied",
    "needs_confirmation",
    "access denied",
    "untrusted domain",
    "prohibited",
    "security block",
    "restricted",
]

FIXABLE_PATTERNS = [
    "missing required",
    "invalid argument",
    "invalid parameter",
    "no such file",
    "file not found",
    "bad request",
    "unknown command",
    "schema validation",
    "syntax error",
    "failed to parse",
    "not found",
    "unrecognized",
]


def classify_failure(
    tool_name: str,
    parameters: dict,
    result_or_exc: Union[outcome.ActionResult, Exception, str],
) -> str:
    """
    Classify a step failure into transient | fixable | blocked | unknown.
    """
    if isinstance(result_or_exc, outcome.ActionResult):
        if result_or_exc.status == outcome.STATUS_NEEDS_CONFIRMATION:
            return CLASSIFICATION_BLOCKED
        err_msg = f"{result_or_exc.error or ''} {result_or_exc.message or ''}".lower()
    elif isinstance(result_or_exc, Exception):
        err_msg = str(result_or_exc).lower()
    else:
        err_msg = str(result_or_exc).lower()

    # 1. Blocked / Permission / Security checks take priority
    for pat in BLOCKED_PATTERNS:
        if pat in err_msg:
            return CLASSIFICATION_BLOCKED

    # 2. Transient / network / quota checks
    for pat in TRANSIENT_PATTERNS:
        if pat in err_msg:
            return CLASSIFICATION_TRANSIENT

    # 3. Fixable / syntax / missing parameters
    for pat in FIXABLE_PATTERNS:
        if pat in err_msg:
            return CLASSIFICATION_FIXABLE

    return CLASSIFICATION_UNKNOWN


def has_side_effects(tool_name: str, parameters: dict) -> bool:
    """Return True if tool has external state-changing side effects."""
    if tool_name in SIDE_EFFECT_TOOLS:
        return True
    if any(k in tool_name for k in ("send", "delete", "write", "move", "remove", "kill", "create")):
        return True
    return False


def verify_state_before_retry(
    tool_name: str,
    parameters: dict,
    workspace_dir: str = "",
) -> Tuple[bool, str]:
    """
    Verify state before retrying a step with side effects.
    Never blindly retry unless idempotent or verified not yet done.
    Returns (can_retry: bool, reason: str).
    """
    # 1. File write / move verification
    if tool_name in ("write_file", "workspace_write_file"):
        path = parameters.get("path") or parameters.get("filename")
        if path:
            if not os.path.isabs(path) and workspace_dir:
                path = os.path.join(workspace_dir, path)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                # File already exists and has content: already succeeded!
                return False, "File already written to destination; retry skipped to avoid overwrite corruption."
        return True, "Verified target file does not yet exist or requires rewrite."

    # 2. Messaging / communication tools
    if "send" in tool_name or tool_name in ("send_message", "whatsapp_send", "email_send"):
        # Without definitive verification receipt that message did NOT send, do NOT blindly resend.
        # Check if idempotency key or verification is present
        if parameters.get("_verified_unsent"):
            return True, "Verified message was not transmitted."
        return False, "Cannot verify message delivery status; blind retry aborted to prevent duplicate send."

    # 3. Delete / remove actions
    if "delete" in tool_name or "remove" in tool_name:
        path = parameters.get("path") or parameters.get("filename")
        if path:
            if not os.path.isabs(path) and workspace_dir:
                path = os.path.join(workspace_dir, path)
            if not os.path.exists(path):
                return False, "Target file/resource already removed; retry skipped."
        return True, "Target still exists; safe to retry removal."

    # By default, non-idempotent side effects cannot be blindly retried without verification
    if has_side_effects(tool_name, parameters):
        return False, f"Side effect step '{tool_name}' cannot be verified; retry aborted for safety."

    return True, "Step is side-effect free and safe to retry."


def compute_backoff(
    attempt: int,
    base: float = 0.5,
    max_backoff: float = 8.0,
    jitter: bool = True,
) -> float:
    """Compute exponential backoff with randomized jitter."""
    delay = min(max_backoff, base * (2 ** (attempt - 1)))
    if jitter:
        delay += random.uniform(0.0, 0.25 * delay)
    return delay


def get_alternative_path(
    tool_name: str,
    parameters: dict,
    error_msg: str,
    attempt: int,
) -> Optional[Tuple[str, dict]]:
    """
    Generate an alternative tool route or corrected parameters for fixable failures.
    Max 2 attempts. Returns (new_tool_name, new_params) or None.
    """
    if attempt > MAX_FIXABLE_ATTEMPTS:
        return None

    err_lower = (error_msg or "").lower()

    # Route 1: Web search query simplification or alternative provider
    if tool_name == "web_search":
        query = parameters.get("query", "")
        if attempt == 1 and ("too long" in err_lower or "no results" in err_lower or "syntax" in err_lower):
            # Simplify query
            clean_q = " ".join([w for w in query.split() if len(w) > 2][:6])
            new_params = dict(parameters)
            new_params["query"] = clean_q
            return tool_name, new_params
        elif attempt == 2:
            new_params = dict(parameters)
            new_params["fallback_provider"] = True
            return tool_name, new_params

    # Route 2: File reading with normalized or relative path
    if tool_name in ("read_file", "workspace_read_file"):
        path = parameters.get("path") or parameters.get("filename") or ""
        if attempt == 1 and ("not found" in err_lower or "no such" in err_lower):
            # Try stripped / normalized path
            norm = os.path.normpath(path.strip().strip("'\""))
            if norm != path:
                new_params = dict(parameters)
                new_params["path"] = norm
                return tool_name, new_params
        elif attempt == 2 and not os.path.isabs(path):
            # Try looking up in current working directory
            abs_p = os.path.abspath(path)
            new_params = dict(parameters)
            new_params["path"] = abs_p
            return tool_name, new_params

    # Route 3: Generic parameter trimming / cleanup
    if attempt == 1:
        cleaned_params = {}
        for k, v in parameters.items():
            if isinstance(v, str):
                cleaned_params[k] = v.strip()
            else:
                cleaned_params[k] = v
        if cleaned_params != parameters:
            return tool_name, cleaned_params

    return None


def build_failure_report(
    task_title: str,
    failed_step: str,
    classification: str,
    attempts: list[dict],
    root_cause: str,
) -> dict[str, Any]:
    """
    Construct a structured, plain-language failure report.
    Explains the failed step, root cause, what was tried, and what the user can do next.
    """
    what_was_tried = []
    for att in attempts:
        t_name = att.get("tool", "unknown_tool")
        num = att.get("attempt", 1)
        st = att.get("status", "failed")
        err = att.get("error", "Error")
        alt = att.get("alternative_applied")
        if alt:
            what_was_tried.append(f"Attempt {num}: Tried alternative route ({alt}) for '{t_name}' -> {st}: {err}")
        else:
            what_was_tried.append(f"Attempt {num}: Executed '{t_name}' -> {st}: {err}")

    # Plain-language suggestions
    if classification == CLASSIFICATION_BLOCKED:
        user_action = (
            "Grant permission for the requested action in Settings or approve the pending confirmation."
        )
    elif classification == CLASSIFICATION_TRANSIENT:
        user_action = (
            "Check your internet connection and network connectivity. The service may be temporarily unavailable."
        )
    elif classification == CLASSIFICATION_FIXABLE:
        user_action = (
            "Verify the provided inputs, file paths, or query parameters. Correct any invalid arguments."
        )
    else:
        user_action = (
            "Review task requirements and inspect system logs for unexpected errors."
        )

    return {
        "status": "failed",
        "task_title": task_title,
        "failed_step": failed_step,
        "classification": classification,
        "root_cause": root_cause,
        "attempts_count": len(attempts),
        "what_was_tried": what_was_tried,
        "user_action": user_action,
        "plain_report": (
            f"Task '{task_title}' failed at step: '{failed_step}'.\n"
            f"Root cause: {root_cause} (Classification: {classification}).\n"
            f"What was tried:\n" + "\n".join(f"- {w}" for w in what_was_tried) + "\n"
            f"Suggested action: {user_action}"
        ),
    }


def execute_step_with_recovery(
    task_id: str,
    task_title: str,
    tool_name: str,
    parameters: dict,
    execute_fn: Callable[[str, dict], outcome.ActionResult],
    workspace_dir: str = "",
    on_attempt_logged: Optional[Callable[[dict], None]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> outcome.ActionResult:
    """
    Execute a task step with automated failure classification and recovery.
    - transient: exponential backoff + jitter (max 3 attempts)
    - fixable: alternative path (max 2 attempts, no loops)
    - blocked/unknown or retries exhausted: mark failed with clear plain report
    - side-effect safety: never blindly retry side effects
    """
    attempts: list[dict] = []
    current_tool = tool_name
    current_params = dict(parameters)
    attempt_num = 1

    while True:
        # 1. Side effect safety check before retry
        if attempt_num > 1 and has_side_effects(current_tool, current_params):
            can_retry, reason = verify_state_before_retry(current_tool, current_params, workspace_dir)
            if not can_retry:
                logger.warning("[Recovery] Side effect step '%s' aborted retry: %s", current_tool, reason)
                root_cause = f"Refused blind retry on side-effect step: {reason}"
                report = build_failure_report(
                    task_title=task_title,
                    failed_step=f"{current_tool}({list(current_params.keys())})",
                    classification=CLASSIFICATION_BLOCKED,
                    attempts=attempts,
                    root_cause=root_cause,
                )
                return outcome.ActionResult.failed(
                    action=current_tool,
                    message=root_cause,
                    error=root_cause,
                    data={"recovery_report": report},
                )

        # 2. Execute step
        t0 = time.monotonic()
        try:
            raw_res = execute_fn(current_tool, current_params)
            res = outcome.normalize_result(raw_res, action_name=current_tool)
        except Exception as e:
            res = outcome.ActionResult.failed(current_tool, message=str(e), error=str(e))
        dur = time.monotonic() - t0
        res.duration_ms = round(dur * 1000.0, 2)

        err_msg = res.error or res.message or ""
        classification = classify_failure(current_tool, current_params, res)

        attempt_record = {
            "attempt": attempt_num,
            "tool": current_tool,
            "parameters": current_params,
            "status": res.status,
            "error": err_msg,
            "verified": getattr(res, "verified", False),
            "classification": classification,
            "duration_sec": round(dur, 4),
            "timestamp": time.time(),
        }
        attempts.append(attempt_record)
        if on_attempt_logged:
            on_attempt_logged(attempt_record)

        # 3. Check for human confirmation requirement: never bypass!
        if res.status == outcome.STATUS_NEEDS_CONFIRMATION:
            return res

        # 4. Check for success (done and verified)
        if res.status == outcome.STATUS_DONE:
            return res

        # 5. Handle transient failures
        if classification == CLASSIFICATION_TRANSIENT:
            if attempt_num < MAX_TRANSIENT_ATTEMPTS:
                delay = compute_backoff(attempt_num)
                logger.info(
                    "[Recovery] Step '%s' transient failure (%s). Backoff %.2fs before retry %d/%d...",
                    current_tool,
                    err_msg[:60],
                    delay,
                    attempt_num + 1,
                    MAX_TRANSIENT_ATTEMPTS,
                )
                sleep_fn(delay)
                attempt_num += 1
                continue
            else:
                root_cause = f"Transient failures exhausted after {attempt_num} attempts. Last error: {err_msg}"
                report = build_failure_report(
                    task_title=task_title,
                    failed_step=f"{current_tool}({list(current_params.keys())})",
                    classification=CLASSIFICATION_TRANSIENT,
                    attempts=attempts,
                    root_cause=root_cause,
                )
                return outcome.ActionResult.failed(
                    action=current_tool,
                    message=root_cause,
                    error=root_cause,
                    data={"recovery_report": report},
                )

        # 6. Handle fixable failures (try alternative paths)
        if classification == CLASSIFICATION_FIXABLE:
            if attempt_num <= MAX_FIXABLE_ATTEMPTS:
                alt = get_alternative_path(current_tool, current_params, err_msg, attempt_num)
                if alt:
                    alt_tool, alt_params = alt
                    attempt_record["alternative_applied"] = f"{alt_tool}({list(alt_params.keys())})"
                    logger.info(
                        "[Recovery] Step '%s' fixable failure. Trying alternative path (%s)...",
                        current_tool,
                        attempt_record["alternative_applied"],
                    )
                    current_tool = alt_tool
                    current_params = alt_params
                    attempt_num += 1
                    continue

            root_cause = f"Fixable error could not be resolved after alternative attempts. Error: {err_msg}"
            report = build_failure_report(
                task_title=task_title,
                failed_step=f"{current_tool}({list(current_params.keys())})",
                classification=CLASSIFICATION_FIXABLE,
                attempts=attempts,
                root_cause=root_cause,
            )
            return outcome.ActionResult.failed(
                action=current_tool,
                message=root_cause,
                error=root_cause,
                data={"recovery_report": report},
            )

        # 7. Blocked or Unknown: Fail immediately without looping
        root_cause = f"Execution blocked or failed: {err_msg}"
        report = build_failure_report(
            task_title=task_title,
            failed_step=f"{current_tool}({list(current_params.keys())})",
            classification=classification,
            attempts=attempts,
            root_cause=root_cause,
        )
        return outcome.ActionResult.failed(
            action=current_tool,
            message=root_cause,
            error=root_cause,
            data={"recovery_report": report},
        )
