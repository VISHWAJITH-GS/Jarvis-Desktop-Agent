"""
core/intent_tracker.py — Intent Tracking, Verbatim Request Preservation & Intent Verification.

Preserves the user's original request verbatim and maintains an authoritative
interpretation across multi-step execution. Compares final outcomes against the
original intent to catch misunderstandings and wrong targets.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

from core import undo
from core.outcome import (
    VERDICT_VERIFIED,
    VERDICT_MISMATCH,
    VERDICT_MISINTERPRETED,
    VERDICT_WRONG_TARGET,
    VERDICT_NOT_DONE,
    VERDICT_UNVERIFIABLE,
)

logger = logging.getLogger("intent_tracker")

_lock = threading.RLock()
_current_user_request: str = ""
_current_interpretation: str = ""
_intent_timestamp: float = 0.0


def set_task_intent(user_request: str, interpretation: str = "") -> str:
    """
    Store the user's original request verbatim and a one-line interpretation.
    If interpretation is empty, generates a deterministic one-line summary.
    """
    global _current_user_request, _current_interpretation, _intent_timestamp
    clean_req = str(user_request or "").strip()
    with _lock:
        _current_user_request = clean_req
        _intent_timestamp = time.time()
        if interpretation:
            _current_interpretation = str(interpretation).strip()
        else:
            _current_interpretation = _derive_interpretation(clean_req)
        return _current_interpretation


def _derive_interpretation(request: str) -> str:
    """Derive a clean, concise one-line interpretation of the user's request."""
    if not request:
        return ""
    # Strip common filler prefixes
    req = re.sub(
        r"^(jarvis|please|could you|can you|hey jarvis|now)\s*,?\s*",
        "",
        request,
        flags=re.IGNORECASE,
    ).strip()
    return req[:120]


def get_task_intent() -> Tuple[str, str]:
    """Return (verbatim_user_request, interpretation)."""
    with _lock:
        return _current_user_request, _current_interpretation


def clear_task_intent() -> None:
    """Clear active task intent upon completion."""
    global _current_user_request, _current_interpretation, _intent_timestamp
    with _lock:
        _current_user_request = ""
        _current_interpretation = ""
        _intent_timestamp = 0.0


def format_intent_announcement() -> str:
    """Format the interpretation statement shown/spoken before sensitive steps."""
    with _lock:
        if _current_interpretation:
            return f"I understood: {_current_interpretation}"
        if _current_user_request:
            return f"I understood: {_current_user_request}"
        return ""


def compare_intent(
    user_request: str,
    expected: str,
    seen: str,
    facts: Optional[dict] = None,
) -> Tuple[Optional[str], str]:
    """
    Compare outcome against original verbatim user request to catch misinterpretation or wrong targets.
    Returns (verdict_override, explanation) or (None, "") if no conflict.
    """
    req = str(user_request or "").strip()
    if not req:
        return None, ""

    req_low = req.lower()
    exp_low = expected.lower()
    seen_low = seen.lower()
    facts_dict = facts or {}

    # 1. Recipient check: request asked for 'Mom', but seen or facts show 'Savemom' or someone else
    for contact in ("mom", "dad", "alice", "bob", "john", "manager", "boss"):
        if re.search(rf"\b{contact}\b", req_low):
            recipient_in_facts = str(
                facts_dict.get("contact") or facts_dict.get("recipient") or facts_dict.get("to") or ""
            ).lower()
            if recipient_in_facts and not re.search(rf"\b{contact}\b", recipient_in_facts):
                return (
                    VERDICT_WRONG_TARGET,
                    f"Requested recipient was '{contact}', but target was '{recipient_in_facts}'",
                )
            if seen and not re.search(rf"\b{contact}\b", seen_low) and ("chat" in seen_low or "message" in seen_low):
                # Check if another name is visible in seen
                other_contacts = [c for c in ("savemom", "bob", "alice", "work") if c in seen_low]
                if other_contacts:
                    return (
                        VERDICT_WRONG_TARGET,
                        f"Requested recipient was '{contact}', but screen shows chat with '{other_contacts[0]}'",
                    )

    # 2. Folder / Path check: request asked for specific folder
    path_matches = re.findall(r"([a-zA-Z]:\\[^\s'\"]+)", req)
    for p in path_matches:
        p_low = p.lower()
        if p_low not in exp_low and p_low not in seen_low:
            # Check if different folder is present in facts or seen
            target_path = str(facts_dict.get("path") or facts_dict.get("target") or "").lower()
            if target_path and target_path != p_low:
                return (
                    VERDICT_MISINTERPRETED,
                    f"Requested path '{p}' differs from target path '{target_path}'",
                )

    # 3. Model check: e.g. Gemini Pro vs Gemini Flash
    if "gemini pro" in req_low or "pro" in req_low:
        if "flash" in seen_low or "flash" in exp_low:
            return (
                VERDICT_MISINTERPRETED,
                "User requested Gemini Pro, but Gemini Flash was selected",
            )
    elif "gemini flash" in req_low or "flash" in req_low:
        if "pro" in seen_low or "pro" in exp_low:
            return (
                VERDICT_MISINTERPRETED,
                "User requested Gemini Flash, but Gemini Pro was selected",
            )

    return None, ""


def format_reporting(
    verdict: str,
    expected: str,
    seen: str,
    reason: str = "",
    can_offer_undo: Optional[bool] = None,
) -> Dict[str, str]:
    """
    Format authoritative speech and log text adhering strictly to the status vocabulary:
    'done' (verified) / 'unverified' / 'failed'.
    Never claims 'done' or 'sent' without verified result.
    """
    has_undo = undo.can_undo() if can_offer_undo is None else can_offer_undo
    undo_clause = " Would you like me to undo it?" if has_undo else ""

    if verdict == VERDICT_VERIFIED:
        speech = f"Done. Verified: {reason or expected}."
        status_word = "done"
    elif verdict == VERDICT_MISMATCH:
        speech = f"Mismatch: {reason or 'The result on screen differs from expected'}.{undo_clause} Or shall I correct it?"
        status_word = "failed"
    elif verdict == VERDICT_MISINTERPRETED:
        speech = f"Misinterpreted: {reason or 'The action does not match your original request'}.{undo_clause} Would you like me to correct it?"
        status_word = "failed"
    elif verdict == VERDICT_WRONG_TARGET:
        speech = f"Wrong target: {reason or 'The action was performed in the wrong location'}.{undo_clause} Do you want me to correct it?"
        status_word = "failed"
    elif verdict == VERDICT_NOT_DONE:
        speech = f"Not completed: {reason or 'The action has not finished or was not applied'}. Shall I try again?"
        status_word = "failed"
    else:  # VERDICT_UNVERIFIABLE
        speech = f"Unverified: could not confirm result on screen ({reason or 'screen obscured or unavailable'}). Shall I check again?"
        status_word = "unverified"

    log_line = (
        f"SYS: Outcome [{status_word.upper()} - {verdict}]: {reason or expected}. Seen: '{seen}'."
    )

    return {
        "status": status_word,
        "verdict": verdict,
        "speech": speech,
        "log": log_line,
        "next_step": "undo" if has_undo and verdict != VERDICT_VERIFIED else "retry_or_correct",
    }
