"""
core/confirm.py — Centralized Confirmation Manager, PendingAction Store & Execution Gate.

THE DESIGN
    The confirmation token is issued by the interface/user, never by the model:
      1. ActionExecutionGuard / messaging tools park any confirmation-required action with PendingAction.
      2. The UI receives a banner with CONFIRM / CANCEL and the assistant speaks the
         prompt directly to the user.
      3. User utterances are intercepted in code BEFORE the model sees them:
         - A match in the confirm set executes the saved params exactly once.
         - A match in the cancel set discards them.
         - Both sets support multiple languages (the system is language-agnostic).
      4. Execution is strictly idempotent: keyed by pending id, marked executed on
         first run, and duplicates are rejected.
      5. Expired actions (>60s) ask for confirmation again; confirm with no pending action
         asks what to confirm.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from core import outcome

# A pending confirmation expires after this long (default 60 seconds).
TIMEOUT_SECONDS = 60.0


# ── Multilingual Confirmation & Cancellation Lexicons ─────────────────────────

CONFIRM_WORDS = {
    # English
    "confirm", "confirmed", "yes", "yeah", "yep", "yup", "sure", "send", "send it",
    "go ahead", "do it", "proceed", "approved", "approve", "okay", "ok", "please do",
    "execute", "alright", "fine", "positive",
    # Spanish
    "confirmar", "confirmado", "sí", "si", "adelante", "hazlo", "proceder", "enviar",
    "claro", "dale", "de acuerdo",
    # French
    "confirmer", "confirmé", "oui", "ouais", "envoyer", "envoie", "vas-y", "allez-y",
    "d'accord", "bien sur",
    # German
    "bestätigen", "bestätigt", "ja", "senden", "schicken", "mach das", "mach es",
    "weiter", "in ordnung",
    # Italian
    "conferma", "confermato", "sì", "si", "invia", "procedi", "fallo", "va bene",
    # Portuguese
    "confirmar", "confirmado", "sim", "enviar", "mande", "pode enviar", "prossiga",
    "faz", "com certeza",
    # Russian
    "подтверждаю", "подтвердить", "да", "отправь", "отправить", "давай", "вперед",
    # Hindi / Hinglish
    "haan", "ha", "han", "bhejo", "bhej do", "sahi hai", "karo", "kar do", "theek hai",
    # Tamil
    "aam", "sari", "anupu", "anupunga", "pannu",
    # Chinese
    "确认", "是的", "发送", "好", "行", "执行",
    # Japanese
    "確認", "はい", "送信", "送って", "やって", "お願い",
}

CANCEL_WORDS = {
    # English
    "cancel", "cancelled", "no", "nope", "stop", "don't", "dont", "abort",
    "never mind", "negative", "reject", "decline", "discard", "halt",
    # Spanish
    "no", "cancelar", "cancela", "parar", "alto", "abortar",
    # French
    "non", "annuler", "annule", "stop", "arrêter",
    # German
    "nein", "abbrechen", "stopp", "halt", "nicht",
    # Italian
    "no", "annulla", "stop", "fermati",
    # Portuguese
    "não", "nao", "cancelar", "pare", "parar", "abortar",
    # Russian
    "нет", "отмена", "отменить", "стой", "стоп",
    # Hindi / Hinglish
    "nahi", "mat bhejo", "ruko", "roko", "band karo",
    # Tamil
    "vendam", "venaam", "nillu",
    # Chinese
    "取消", "不", "不要", "停止",
    # Japanese
    "キャンセル", "いいえ", "やめて", "止めて",
}


def _normalize_text(text: str) -> str:
    """Normalize utterance for language-agnostic matching."""
    clean = str(text or "").lower().strip()
    # Remove leading/trailing punctuation except non-ascii letters/characters
    return re.sub(r"[^\w\s]", " ", clean, flags=re.UNICODE).strip()


def _is_command_or_action_utterance(norm: str) -> bool:
    """True if utterance is a full action command or complex sentence, not a confirmation token."""
    # Check prefixes that initiate actions
    for p in (
        "send a ", "send an ", "send the ", "send to ", "send this ", "send that ",
        "send message", "send msg", "send text", "send email", "send mail",
        "send whatsapp", "send him", "send her", "send them", "send their",
        "can you send", "could you send", "please send a", "please send me",
        "tell ", "ask ", "open ", "call ", "search for ", "play ",
    ):
        if norm.startswith(p):
            return True
    # Check phrases indicating target/recipient or conversational structure
    for p in ("message to ", "text to ", "email to ", "mail to ", "call to ", "write to "):
        if p in norm:
            return True
    # Conversational negatives / queries that shouldn't match confirmation or cancellation
    for p in ("is it fine", "are you sure", "is not sent", "not sent", "doesnt make", "doesn't make", "did not", "didn't"):
        if p in norm:
            return True
    return False


def matches_confirm(utterance: str) -> bool:
    """True if utterance expresses confirmation in any supported language."""
    norm = _normalize_text(utterance)
    if not norm:
        return False
    if _is_command_or_action_utterance(norm):
        return False
    if norm in CONFIRM_WORDS:
        return True
    # Multi-word phrase matching
    confirm_phrases = (
        "go ahead", "do it", "send it", "please do", "please send", "yes please",
        "yes go ahead", "yeah go ahead", "ok do it", "okay do it", "confirm it",
        "confirm please", "proceed please", "de acuerdo", "in ordnung", "va bene",
        "com certeza", "bhej do", "kar do", "theek hai"
    )
    for phrase in confirm_phrases:
        if norm == phrase or norm.startswith(phrase + " ") or norm.endswith(" " + phrase):
            return True

    words = norm.split()
    # A voice confirmation token is short (at most 4 words)
    if len(words) <= 4:
        first = words[0]
        if first in ("yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "haan", "ha", "si", "sí", "oui", "ja"):
            safe_fillers = {"please", "jarvis", "that", "it", "ahead", "do", "now", "go", "proceed", "send", "confirm"}
            if all(w in CONFIRM_WORDS or w in safe_fillers for w in words):
                return True
    return False


def matches_cancel(utterance: str) -> bool:
    """True if utterance expresses cancellation in any supported language."""
    norm = _normalize_text(utterance)
    if not norm:
        return False
    if _is_command_or_action_utterance(norm):
        return False
    if norm in CANCEL_WORDS:
        return True
    cancel_phrases = (
        "never mind", "don't do it", "dont do it", "mat bhejo", "band karo",
        "no cancel", "no stop", "cancel it", "cancel that", "stop it",
        "don't send", "dont send", "no don't", "no dont", "no thanks",
        "no thank you", "please don't", "please dont"
    )
    for phrase in cancel_phrases:
        if norm == phrase or norm.startswith(phrase + " ") or norm.endswith(" " + phrase):
            return True

    words = norm.split()
    # A voice cancellation token is short (at most 4 words)
    if len(words) <= 4:
        first = words[0]
        if first in ("no", "nope", "nah", "nahi", "non", "nein"):
            safe_fillers = {"please", "jarvis", "thanks", "thank", "you", "it", "that", "dont", "don't", "stop", "cancel", "never", "mind"}
            if all(w in CANCEL_WORDS or w in safe_fillers for w in words):
                return True
    return False


# ── PendingAction Dataclass ──────────────────────────────────────────────────

class PendingAction:
    """Deterministic in-memory pending action store object."""

    def __init__(
        self,
        id: Optional[str] = None,
        tool: str = "",
        params: Optional[dict] = None,
        created_at: Optional[float] = None,
        expires_in: float = TIMEOUT_SECONDS,
        status: str = "needs_confirmation",
        executed: bool = False,
        run: Optional[Callable[[], Any]] = None,
        title: str = "",
        detail: str = "",
        risk_level: str = "confirmation",
        payment_details: Optional[dict] = None,
        visual_prompt: bool = False,
        result: Optional[Any] = None,
        # Legacy backward-compatibility parameters
        key: Optional[str] = None,
        action: Optional[str] = None,
        parameters: Optional[dict] = None,
        at: Optional[float] = None,
    ):
        self.id = id or f"pending_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        self.tool = tool or action or key or ""
        self.params = dict(params if params is not None else (parameters if parameters is not None else {}))
        self.created_at = created_at if created_at is not None else (at if at is not None else time.monotonic())
        self.expires_in = float(expires_in)
        self.status = status
        self.executed = executed
        self.executed_at: Optional[float] = None
        self.run = run
        self.title = title or f"Execute {self.tool}"
        self.detail = detail or ""
        self.risk_level = risk_level
        self.payment_details = payment_details
        self.visual_prompt = visual_prompt
        self.result = result

        # Ensure contact and text params are populated if receiver/message were used
        if "contact" not in self.params and "receiver" in self.params:
            self.params["contact"] = self.params["receiver"]
        if "text" not in self.params:
            for k in ("message_text", "message"):
                if k in self.params:
                    self.params["text"] = self.params[k]
                    break

    # Backward-compatible properties
    @property
    def key(self) -> str:
        return self.tool

    @key.setter
    def key(self, val: str) -> None:
        self.tool = val

    @property
    def action(self) -> str:
        return self.tool

    @action.setter
    def action(self, val: str) -> None:
        self.tool = val

    @property
    def parameters(self) -> dict:
        return self.params

    @parameters.setter
    def parameters(self, val: dict) -> None:
        self.params = val

    @property
    def at(self) -> float:
        return self.created_at

    @at.setter
    def at(self, val: float) -> None:
        self.created_at = val

    def is_expired(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.monotonic()
        return (t - self.created_at) > self.expires_in

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tool": self.tool,
            "params": self.params,
            "created_at": self.created_at,
            "expires_in": self.expires_in,
            "status": self.status,
            "executed": self.executed,
            "title": self.title,
            "detail": self.detail,
        }


_VISUAL_CONFIRM_LEVELS = ("high_impact", "high", "heavy", "sensitive", "destructive")


def requires_visual_prompt(risk_level: str, payment_details: Optional[dict] = None) -> bool:
    """Confirmation UI policy."""
    if payment_details:
        return True
    lvl = str(risk_level or "").strip().lower()
    return any(tok in lvl for tok in _VISUAL_CONFIRM_LEVELS)


# Callbacks wired to UI banner
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb: Optional[Callable[[str], None]] = None


def bind(show, hide, log=None) -> None:
    """Wire this module to the HUD banner. Called once from main.py / live_session at startup."""
    global _show_cb, _hide_cb, _log_cb
    _show_cb, _hide_cb, _log_cb = show, hide, log


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


class ConfirmationManager:
    """Centralized manager and store for pending confirmation requests and resolutions."""

    _lock = threading.RLock()
    _store: Dict[str, PendingAction] = {}
    _active_id: Optional[str] = None
    _last_expired: Optional[PendingAction] = None
    _last_executed: Optional[PendingAction] = None
    _executed_ids: set[str] = set()

    @classmethod
    def reset_state(cls) -> None:
        """Reset internal store and state (primarily for tests)."""
        with cls._lock:
            cls._store.clear()
            cls._active_id = None
            cls._last_expired = None
            cls._last_executed = None
            cls._executed_ids.clear()
        if _hide_cb:
            try:
                _hide_cb()
            except Exception:
                pass

    @classmethod
    def save_pending(
        cls,
        tool: str,
        params: dict,
        run: Optional[Callable[[], Any]] = None,
        expires_in: float = TIMEOUT_SECONDS,
        title: str = "",
        detail: str = "",
        risk_level: str = "confirmation",
        payment_details: Optional[dict] = None,
        visual_prompt: bool = False,
        action_id: Optional[str] = None,
    ) -> PendingAction:
        """Save a pending action into the store deterministically without launching any app."""
        with cls._lock:
            old_p = cls.get_pending_internal()
            p = PendingAction(
                id=action_id,
                tool=tool,
                params=params,
                created_at=time.monotonic(),
                expires_in=expires_in,
                run=run,
                title=title or f"Execute {tool}",
                detail=detail or "",
                risk_level=risk_level,
                payment_details=payment_details,
                visual_prompt=visual_prompt,
            )
            cls._store[p.id] = p
            cls._active_id = p.id

        if old_p is not None and old_p.id != p.id:
            _log(f"SYS: Replaced pending action '{old_p.title}' with '{p.title}'")

        return p

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
        action_id: Optional[str] = None,
    ) -> str:
        """Park an action behind the confirmation gate."""
        show_visual = requires_visual_prompt(risk_level, payment_details=payment_details)

        p = cls.save_pending(
            tool=action,
            params=parameters,
            run=run,
            expires_in=TIMEOUT_SECONDS,
            title=title,
            detail=detail,
            risk_level=risk_level,
            payment_details=payment_details,
            visual_prompt=show_visual,
            action_id=action_id,
        )

        if show_visual:
            if _show_cb is not None:
                try:
                    _show_cb(title, detail)
                except Exception:
                    pass
            _log(f"SYS: Awaiting voice + visual confirmation — {title} [{risk_level}] (id={p.id})")
        else:
            if _hide_cb is not None:
                try:
                    _hide_cb()
                except Exception:
                    pass
            _log(f"SYS: Awaiting voice-only confirmation — {title} [{risk_level}] (id={p.id})")

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
        """Discard any currently active pending action."""
        with cls._lock:
            p = cls.get_pending_internal()
            if p is not None:
                p.status = "cancelled"
            cls._active_id = None
        if _hide_cb:
            try:
                _hide_cb()
            except Exception:
                pass
        if p is not None:
            _log(f"SYS: Discarded pending action — {p.title} ({reason or 'manual'})")

    @classmethod
    def get_pending_internal(cls) -> Optional[PendingAction]:
        """Internal lock-free check for active pending action."""
        if not cls._active_id:
            return None
        p = cls._store.get(cls._active_id)
        if p is None:
            return None
        if p.is_expired():
            p.status = "expired"
            cls._last_expired = p
            cls._active_id = None
            return None
        if p.executed or p.status in ("executed", "cancelled"):
            return None
        return p

    @classmethod
    def get_pending(cls, action_id: Optional[str] = None) -> Optional[PendingAction]:
        """Return the current active pending action, or None if empty or expired."""
        with cls._lock:
            if action_id:
                p = cls._store.get(action_id)
                if p is not None and p.is_expired():
                    p.status = "expired"
                    cls._last_expired = p
                    return None
                return p
            return cls.get_pending_internal()

    @classmethod
    def pending_title(cls) -> str:
        """Return title of current pending action, or '' if none/expired."""
        with cls._lock:
            p = cls.get_pending_internal()
            return p.title if p else ""

    @classmethod
    def has_recent_execution(cls, tool_name: str, seconds: float = 5.0) -> bool:
        """Check if a tool was executed via confirmation within the last N seconds."""
        now = time.monotonic()
        with cls._lock:
            for p in cls._store.values():
                if p.tool == tool_name and p.executed and p.executed_at is not None:
                    if (now - p.executed_at) <= seconds:
                        return True
        return False

    @classmethod
    def _execute_saved_action(cls, p: PendingAction, player=None) -> Any:
        """Execute saved params exactly once."""
        if p.run is not None:
            return p.run()

        # Direct execution fallback for standard tools if run callable was not bound
        if p.tool in ("whatsapp_send", "whatsapp"):
            from plugins import whatsapp_automation
            auth_params = {**p.params, "_execution_authorized": True, "confirmed": True}
            return whatsapp_automation.run(auth_params, player=player)
        elif p.tool == "send_message":
            from actions import send_message
            auth_params = {**p.params, "_execution_authorized": True, "confirmed": True}
            return send_message.send_message(auth_params, player=player)
        else:
            from core.action_loader import get_registry
            reg = get_registry()
            if reg and reg.has(p.tool):
                auth_params = {**p.params, "_execution_authorized": True, "confirmed": True}
                return reg.run(p.tool, auth_params, ctx={"player": player})
            return "Done."

    @classmethod
    def resolve(cls, accepted: bool, action_id: Optional[str] = None, sync: bool = False) -> bool:
        """Execute or cancel the pending action with strict idempotency."""
        with cls._lock:
            if action_id:
                p = cls._store.get(action_id)
            else:
                p = cls.get_pending_internal()

            if p is None:
                return False

            # Idempotency check: Reject duplicate runs
            if p.executed or p.id in cls._executed_ids:
                _log(f"SYS: Reject duplicate execution attempt for '{p.title}' (id={p.id})")
                return False

            if p.is_expired():
                p.status = "expired"
                cls._last_expired = p
                cls._active_id = None
                _log(f"SYS: Confirmation expired — {p.title}")
                return False

            if not accepted:
                p.status = "cancelled"
                cls._active_id = None
                _log(f"SYS: Cancelled — {p.title}")
                return False

            # Mark executed immediately before invoking run to reject concurrent duplicates
            p.status = "executed"
            p.executed = True
            p.executed_at = time.monotonic()
            cls._executed_ids.add(p.id)
            cls._last_executed = p
            cls._active_id = None

        if _hide_cb:
            try:
                _hide_cb()
            except Exception:
                pass

        if sync:
            try:
                res = cls._execute_saved_action(p)
                p.result = res
                _log(f"SYS: Confirmed — {p.title}. {res}")
                return True
            except Exception as e:
                _log(f"ERR: {p.title} failed — {e}")
                return False

        # Execute on background worker thread
        def _worker():
            try:
                res = cls._execute_saved_action(p)
                p.result = res
                _log(f"SYS: Confirmed — {p.title}. {res}")
            except Exception as e:
                _log(f"ERR: {p.title} failed — {e}")

        threading.Thread(
            target=_worker,
            daemon=True,
            name=f"confirm-{p.id}",
        ).start()
        return True

    @classmethod
    def intercept_confirmation(
        cls,
        utterance: str,
        player=None,
        speak=None,
    ) -> Optional[outcome.ActionResult]:
        """
        Intercept confirmation or cancellation utterances BEFORE the model sees them.
        Deterministic and language-agnostic.

        Returns:
            - outcome.ActionResult when resolved or handled.
            - None if utterance does not match confirmation or cancellation vocabulary.
        """
        is_conf = matches_confirm(utterance)
        is_canc = matches_cancel(utterance)

        if not (is_conf or is_canc):
            return None

        # Handle cancellation
        if is_canc:
            with cls._lock:
                p = cls.get_pending_internal()
                if p is not None:
                    p.status = "cancelled"
                    cls._active_id = None
                    msg = f"Cancelled {p.title}."
                else:
                    msg = "No pending action to cancel."

            if _hide_cb:
                try:
                    _hide_cb()
                except Exception:
                    pass

            if player and hasattr(player, "write_log"):
                try:
                    player.write_log(f"SYS: {msg}")
                except Exception:
                    pass
            if speak:
                try:
                    speak(msg)
                except Exception:
                    pass

            return outcome.ActionResult(
                status="failed" if "No pending" in msg else "cancelled",
                action=p.tool if p else "confirmation",
                success=False,
                verified=False,
                message=msg,
                data={"id": p.id if p else None, "cancelled": True},
            )

        # Handle confirmation
        with cls._lock:
            p = cls.get_pending_internal()
            last_exp = cls._last_expired

            # 1. No active pending action
            if p is None:
                if last_exp is None:
                    for item in reversed(list(cls._store.values())):
                        if item.is_expired():
                            last_exp = item
                            break

                if last_exp is not None:
                    # Expired pending -> asks again
                    ask_msg = (
                        f"The confirmation for {last_exp.title} has expired. "
                        f"Shall I proceed with {last_exp.title}? Say confirm to execute, or no to cancel."
                    )
                    cls._last_expired = None
                    # Renew pending action with fresh timeout
                    fresh = cls.save_pending(
                        tool=last_exp.tool,
                        params=last_exp.params,
                        run=last_exp.run,
                        expires_in=TIMEOUT_SECONDS,
                        title=last_exp.title,
                        detail=last_exp.detail,
                        risk_level=last_exp.risk_level,
                    )
                    if player and hasattr(player, "write_log"):
                        try:
                            player.write_log(f"SYS: {ask_msg}")
                        except Exception:
                            pass
                    if speak:
                        try:
                            speak(ask_msg)
                        except Exception:
                            pass
                    return outcome.result_needs_confirmation(
                        fresh.tool,
                        message=ask_msg,
                        data={"id": fresh.id, "expired": True, "renewed": True},
                    )

                # Check if an action was recently executed (reject duplicates)
                last_exec = cls._last_executed
                if last_exec is None:
                    for item in reversed(list(cls._store.values())):
                        if item.executed or item.id in cls._executed_ids:
                            last_exec = item
                            break

                if last_exec is not None and (last_exec.executed or last_exec.id in cls._executed_ids):
                    dup_msg = f"Action '{last_exec.title}' has already been executed."
                    if player and hasattr(player, "write_log"):
                        try:
                            player.write_log(f"SYS: {dup_msg}")
                        except Exception:
                            pass
                    return outcome.result_done(
                        last_exec.tool,
                        message=dup_msg,
                        verified=True,
                        data={"id": last_exec.id, "duplicate": True},
                    )

                # Confirm with no pending action -> asks what to confirm
                ask_msg = "There is no pending action to confirm. What would you like me to confirm?"
                if player and hasattr(player, "write_log"):
                    try:
                        player.write_log(f"SYS: {ask_msg}")
                    except Exception:
                        pass
                if speak:
                    try:
                        speak(ask_msg)
                    except Exception:
                        pass
                return outcome.result_failed("confirmation", ask_msg, data={"no_pending": True})

            # 2. Check duplicate execution: key by pending id, mark executed, reject duplicates
            if p.executed or p.id in cls._executed_ids:
                dup_msg = f"Action '{p.title}' has already been executed."
                if player and hasattr(player, "write_log"):
                    try:
                        player.write_log(f"SYS: {dup_msg}")
                    except Exception:
                        pass
                return outcome.result_done(
                    p.tool,
                    message=dup_msg,
                    verified=True,
                    data={"id": p.id, "duplicate": True},
                )

            # Mark executed immediately under lock
            p.status = "executed"
            p.executed = True
            p.executed_at = time.monotonic()
            cls._executed_ids.add(p.id)
            cls._last_executed = p
            cls._active_id = None

        if _hide_cb:
            try:
                _hide_cb()
            except Exception:
                pass

        # 3. Execute saved params exactly once
        try:
            raw_res = cls._execute_saved_action(p, player=player)
            p.result = raw_res
            res_obj = outcome.normalize_result(raw_res, action_name=p.tool)
            if not res_obj.action:
                res_obj.action = p.tool

            speech_text = res_obj.message or res_obj.to_speech_text()
            if player and hasattr(player, "write_log"):
                try:
                    player.write_log(f"SYS: Confirmed — {p.title}. {speech_text}")
                except Exception:
                    pass
            if speak:
                try:
                    speak(speech_text)
                except Exception:
                    pass

            return res_obj
        except Exception as e:
            err_res = outcome.result_failed(p.tool, f"Execution failed: {e}", error=str(e))
            p.result = err_res
            return err_res

    @classmethod
    def resolve_by_voice(cls, utterance: str) -> Optional[bool]:
        """Backward-compatible voice resolution check returning bool or None."""
        if not cls.pending_title():
            # If nothing pending, check if cancel was said
            if matches_cancel(utterance):
                return False
            return None

        if matches_confirm(utterance):
            return cls.resolve(True)
        if matches_cancel(utterance):
            cls.resolve(False)
            return False
        return None


# ── Backward-compatible module-level functions ───────────────────────────────

def save_pending(
    tool: str,
    params: dict,
    run: Optional[Callable[[], Any]] = None,
    expires_in: float = TIMEOUT_SECONDS,
    title: str = "",
    detail: str = "",
    risk_level: str = "confirmation",
    payment_details: Optional[dict] = None,
    visual_prompt: bool = False,
    action_id: Optional[str] = None,
) -> PendingAction:
    return ConfirmationManager.save_pending(
        tool=tool,
        params=params,
        run=run,
        expires_in=expires_in,
        title=title,
        detail=detail,
        risk_level=risk_level,
        payment_details=payment_details,
        visual_prompt=visual_prompt,
        action_id=action_id,
    )


def request_action(
    action: str,
    parameters: dict,
    risk_level: str,
    title: str,
    detail: str,
    run: Callable[[], str],
    payment_details: Optional[dict] = None,
    action_id: Optional[str] = None,
) -> str:
    return ConfirmationManager.request_action(
        action=action,
        parameters=parameters,
        risk_level=risk_level,
        title=title,
        detail=detail,
        run=run,
        payment_details=payment_details,
        action_id=action_id,
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


def resolve(accepted: bool, action_id: Optional[str] = None, sync: bool = False) -> bool:
    return ConfirmationManager.resolve(accepted, action_id=action_id, sync=sync)


def resolve_by_voice(utterance: str) -> Optional[bool]:
    return ConfirmationManager.resolve_by_voice(utterance)


def intercept_confirmation(utterance: str, player=None, speak=None) -> Optional[outcome.ActionResult]:
    return ConfirmationManager.intercept_confirmation(utterance, player=player, speak=speak)


def pending_title() -> str:
    return ConfirmationManager.pending_title()


def get_pending(action_id: Optional[str] = None) -> Optional[PendingAction]:
    return ConfirmationManager.get_pending(action_id=action_id)


def discard_pending(reason: str = "") -> None:
    ConfirmationManager.discard_pending(reason)


def reset_state() -> None:
    ConfirmationManager.reset_state()


def has_recent_execution(tool_name: str, seconds: float = 5.0) -> bool:
    return ConfirmationManager.has_recent_execution(tool_name, seconds=seconds)
