"""
actions/verify_outcome.py — Visual and Structured Outcome Verification Action for JARVIS.

Inspects the real screen, window, or browser tab after an important action
and cross-references against structured evidence and the user's original request.

Authoritative Verdicts:
- verified: The screen clearly shows the expected result.
- mismatch: Something is visible but differs (wrong text, model, file, tab, etc.).
- misinterpreted: Action matched what Jarvis did, but not what the user originally requested.
- wrong_target: Right action performed in the wrong target (wrong window, chat, project, profile).
- not_done: Nothing happened, or the operation is still in progress.
- unverifiable: Screen obscured, blocked, sensitive, or insufficient visibility to judge.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core import outcome

logger = logging.getLogger("verify_outcome")

VERDICT_VERIFIED = "verified"
VERDICT_MISMATCH = "mismatch"
VERDICT_MISINTERPRETED = "misinterpreted"
VERDICT_WRONG_TARGET = "wrong_target"
VERDICT_NOT_DONE = "not_done"
VERDICT_UNVERIFIABLE = "unverifiable"

ALLOWED_VERDICTS = {
    VERDICT_VERIFIED,
    VERDICT_MISMATCH,
    VERDICT_MISINTERPRETED,
    VERDICT_WRONG_TARGET,
    VERDICT_NOT_DONE,
    VERDICT_UNVERIFIABLE,
}

SCOPE_SCREEN = "screen"
SCOPE_WINDOW = "window"
SCOPE_BROWSER_TAB = "browser_tab"

ALLOWED_SCOPES = {SCOPE_SCREEN, SCOPE_WINDOW, SCOPE_BROWSER_TAB}


def verify_outcome(parameters: dict, player=None) -> outcome.ActionResult:
    """Action entry point for verify_outcome."""
    from actions import visual_verifier

    return visual_verifier.verify_outcome_action(parameters, player=player)


TOOL = {
    "name": "verify_outcome",
    "description": (
        "Visually verify that an action completed as the user intended. "
        "Inspects the target screen, active window, or browser tab with vision model, "
        "cross-checks against structured tool evidence and the user's original intent, "
        "and returns an authoritative verdict: 'verified', 'mismatch', 'misinterpreted', "
        "'wrong_target', 'not_done', or 'unverifiable'."
    ),
    "brief": "Visual verification: check real screen against expected outcome and user intent.",
    "tier": "core",
    "permission": "read_only",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "expected": {
                "type": "STRING",
                "description": "Concrete expected visual or state outcome (e.g. 'WhatsApp chat Mom shows outgoing message with tick').",
            },
            "scope": {
                "type": "STRING",
                "description": "Verification target scope: 'screen', 'window', or 'browser_tab'. Default is 'window'.",
                "enum": ["screen", "window", "browser_tab"],
            },
            "evidence_from_tool": {
                "type": "OBJECT",
                "description": "Optional structured evidence dictionary returned by previous tool calls.",
            },
            "user_request": {
                "type": "STRING",
                "description": "Optional original verbatim user request to verify against misinterpretation.",
            },
        },
        "required": ["expected"],
    },
    "handler": verify_outcome,
}
