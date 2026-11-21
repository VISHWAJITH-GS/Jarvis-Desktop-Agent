"""
core/confirm.py — Centralized Confirmation Manager & Execution Gate.

THE DESIGN
    The confirmation token is issued by the interface/user, never by the model:
      1. ActionExecutionGuard parks any confirmation-required action with PendingAction.
      2. The UI receives a banner with CONFIRM / CANCEL and the assistant speaks the
         prompt directly to the user.
      3. If and only if the user says "confirm" / "yes" or presses CONFIRM, resolve(True)
         is invoked, running the stored action callable off the Qt thread.
      4. If the user says "no" / "cancel" or presses CANCEL, resolve(False) is invoked,
         cancelling and discarding the pending action.
      5. Any new action request automatically discards previous pending actions, preventing
         stale or mismatched confirmation reuse.
      6. Expired actions (>90s) are safely dropped.
      7. Duplicate execution is strictly prevented by popping pending state before execution.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

# A pending confirmation is abandoned after this long.
TIMEOUT_SECONDS = 90.0


@dataclass
class PendingAction:
    key: str
    action: str
    parameters: dict
    risk_level: str
    title: str
    detail: str
    run: Callable[[], str]
    at: float
    payment_details: Optional[dict] = None
    visual_prompt: bool = False


_VISUAL_CONFIRM_LEVELS = ("high_impact", "high", "heavy", "sensitive", "destructive")


def requires_visual_prompt(risk_level: str, payment_details: Optional[dict] = None) -> bool:
    """
    Confirmation UI policy:
      - Low / medium-level tasks ('confirmation', 'medium', 'low', 'normal'):
        Voice confirmation ONLY (no visual click pop-up).
      - High-level, heavy, or sensitive tasks ('high_impact', 'destructive',
        'sensitive', 'high', 'heavy', or any payment/purchase):
        BOTH voice confirmation AND visual click-based confirmation prompt.
    """
    if payment_details:
        return True
    lvl = str(risk_level or "").strip().lower()
    return any(tok in lvl for tok in _VISUAL_CONFIRM_LEVELS)


_pending: Optional[PendingAction] = None
_lock = threading.Lock()

# Callbacks wired to UI banner
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb: Optional[Callable[[str], None]] = None


def bind(show, hide, log=None) -> None:
    """Wire this module to the HUD banner. Called once from main.py at startup."""
    global _show_cb, _hide_cb, _log_cb
    _show_cb, _hide_cb, _log_cb = show, hide, log


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


class ConfirmationManager:
    """Centralized manager for pending confirmation requests and resolutions."""

    @classmethod
    def request_action(
        cls,
        action: str,
        parameters: dict,
        risk_level: str,
        title: str,
        detail: str,
        run: Callable[[], str],
        payment_details: Optional[dict] = None,
    ) -> str:
        """Park an action behind the confirmation gate.

        Low/medium tasks use voice confirmation only (no visual popup).
        High-level / heavy / sensitive tasks use both voice and visual click prompts.
        """
        global _pending

        show_visual = requires_visual_prompt(risk_level, payment_details=payment_details)

        with _lock:
            old = _pending
            _pending = PendingAction(
                key=action,
                action=action,
                parameters=dict(parameters or {}),
                risk_level=str(risk_level),
                title=title,
                detail=detail,
                run=run,
                at=time.monotonic(),
                payment_details=payment_details,
                visual_prompt=show_visual,
            )

        if old is not None:
            _log(f"SYS: Replaced pending action '{old.title}' with '{title}'")

        if show_visual:
            if _show_cb is not None:
                try:
                    _show_cb(title, detail)
                except Exception:
                    pass
            _log(f"SYS: Awaiting voice + visual confirmation — {title} [{risk_level}]")
        else:
            # Ensure any prior visual banner is closed for voice-only confirmations
            if old is not None and getattr(old, "visual_prompt", False) and _hide_cb is not None:
                try:
                    _hide_cb()
                except Exception:
                    pass
            _log(f"SYS: Awaiting voice-only confirmation — {title} [{risk_level}]")

        # Voice prompt phrasing
        if payment_details:
            amt = payment_details.get("amount") or "the specified amount"
            recip = payment_details.get("recipient") or payment_details.get("merchant") or "the merchant"
            return (
                f"[CONFIRMATION_PENDING] Shall I proceed with payment of {amt} to {recip}? "
                f"Say confirm to execute, or no to cancel."
            )
        elif show_visual:
            return (
                f"[CONFIRMATION_PENDING] Shall I proceed with {title}? "
                f"Note: {detail}. Say confirm or click CONFIRM to execute, or say no to cancel."
            )
        else:
            return f"[CONFIRMATION_PENDING] Shall I proceed with {title}? Say confirm to execute, or no to cancel."

    @classmethod
    def discard_pending(cls, reason: str = "") -> None:
        """Discard any currently pending action."""
        global _pending
        with _lock:
            old = _pending
            _pending = None
        if _hide_cb:
            try:
                _hide_cb()
            except Exception:
                pass
        if old is not None:
            _log(f"SYS: Discarded pending action — {old.title} ({reason or 'manual'})")

    @classmethod
    def get_pending(cls) -> Optional[PendingAction]:
        """Return the current active pending action, or None if empty or expired."""
        with _lock:
            if _pending is None:
                return None
            if time.monotonic() - _pending.at > TIMEOUT_SECONDS:
                return None
            return _pending

    @classmethod
    def resolve(cls, accepted: bool) -> bool:
        """Execute or cancel the pending action."""
        global _pending

        with _lock:
            p, _pending = _pending, None

        if _hide_cb:
            try:
                _hide_cb()
            except Exception:
                pass

        if p is None:
            return False

        if time.monotonic() - p.at > TIMEOUT_SECONDS:
            _log(f"SYS: Confirmation expired — {p.title}")
            return False

        if not accepted:
            _log(f"SYS: Cancelled — {p.title}")
            return False

        # Execute on background worker thread
        def _worker():
            try:
                result = p.run() or "Done."
                _log(f"SYS: Confirmed — {p.title}. {result}")
            except Exception as e:
                _log(f"ERR: {p.title} failed — {e}")

        threading.Thread(
            target=_worker,
            daemon=True,
            name=f"confirm-{p.key}",
        ).start()
        return True

    @classmethod
    def resolve_by_voice(cls, utterance: str) -> Optional[bool]:
        """Inspects user voice utterance when confirmation is pending."""
        if not pending_title():
            return None

        clean = str(utterance or "").lower().strip()
        yes_words = {
            "confirm",
            "cconfirm",
            "confirmed",
            "yes",
            "proceed",
            "sure",
            "do it",
            "approved",
            "go ahead",
            "okay",
            "ok",
        }
        no_words = {"no", "cancel", "stop", "abort", "don't", "dont", "never mind", "negative", "reject"}

        words = set(re.findall(r"\w+", clean))
        if "confirm" in clean or "cconfirm" in clean or (words & yes_words):
            return cls.resolve(True)
        if words & no_words:
            cls.resolve(False)
            return False
        return None

    @classmethod
    def pending_title(cls) -> str:
        """Return title of current pending action, or '' if none/expired."""
        with _lock:
            if _pending is None:
                return ""
            if time.monotonic() - _pending.at > TIMEOUT_SECONDS:
                return ""
            return _pending.title


# Backward-compatible module-level functions
def request_action(
    action: str,
    parameters: dict,
    risk_level: str,
    title: str,
    detail: str,
    run: Callable[[], str],
    payment_details: Optional[dict] = None,
) -> str:
    return ConfirmationManager.request_action(
        action=action,
        parameters=parameters,
        risk_level=risk_level,
        title=title,
        detail=detail,
        run=run,
        payment_details=payment_details,
    )


def request(key: str, title: str, detail: str, run: Callable[[], str]) -> str:
    """Backward-compatible request wrapper."""
    return ConfirmationManager.request_action(
        action=key,
        parameters={},
        risk_level="confirmation",
        title=title,
        detail=detail,
        run=run,
    )


def resolve(accepted: bool) -> bool:
    return ConfirmationManager.resolve(accepted)


def resolve_by_voice(utterance: str) -> Optional[bool]:
    return ConfirmationManager.resolve_by_voice(utterance)


def pending_title() -> str:
    return ConfirmationManager.pending_title()


def get_pending() -> Optional[PendingAction]:
    return ConfirmationManager.get_pending()


def discard_pending(reason: str = "") -> None:
    ConfirmationManager.discard_pending(reason)
