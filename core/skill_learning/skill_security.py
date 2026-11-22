"""
core/skill_learning/skill_security.py — Security, Sensitive Field Protection, and Permissions.

Strictly protects user privacy during demonstration:
- Detects password, token, PIN, and sensitive credential fields.
- Masks sensitive inputs so passwords are NEVER recorded or sent to Gemini.
- Classifies declarative skills by RiskLevel (READ_ONLY, NORMAL, SENSITIVE, DESTRUCTIVE).
- Enforces mandatory user confirmation for sensitive or destructive actions (emailing, purchasing, deleting).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from core.skill_learning.models import RiskLevel, SkillDefinition, StepDefinition

_SENSITIVE_KEYWORD_RE = re.compile(
    r"(pass(word)?|pin|secret|token|api_?key|auth|cvv|cvc|credit_?card|card_?number|ssn)",
    re.IGNORECASE,
)

_DESTRUCTIVE_ACTIONS = {
    "delete",
    "remove",
    "drop",
    "destroy",
    "format",
    "shutdown",
    "restart",
    "send_email",
    "publish",
    "purchase",
    "pay",
    "order",
    "transfer",
    "kill",
}

_SENSITIVE_ACTIONS = {
    "upload",
    "download",
    "login",
    "authenticate",
    "connect",
    "install",
    "modify_settings",
    "write_file",
    "dev_agent",
}

SENSITIVE_INPUT_PLACEHOLDER = "[REDACTED_SENSITIVE_CREDENTIAL]"

_CREDENTIAL_PATTERNS = [
    re.compile(r"(password\s*(?:is|for|:|are|=)?\s*)([^\s,.]+)", re.IGNORECASE),
    re.compile(r"(pass\s*(?:is|for|:|are|=)?\s*)([^\s,.]+)", re.IGNORECASE),
    re.compile(r"(credentials?\s*(?:are|is|:|for)?\s*)([^\s,.]+)", re.IGNORECASE),
    re.compile(r"(login\s*id\s*(?:is|:|are)?\s*)([^\s,.]+)", re.IGNORECASE),
    re.compile(r"(\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+\b)"),
    re.compile(r"(\b\d{10,}@\w+\b)"),  # Student register number / university handle
]


def sanitize_text_and_credentials(text: str) -> str:
    """Scrub passwords, tokens, and private credentials from text before persistence or model analysis."""
    if not text:
        return ""
    result = str(text)

    # 1. Scrub student register numbers / institutional handles first
    result = re.sub(r"(\b\d{10,}@\w+\b)", r"[REDACTED_USER]", result)

    # 2. Scrub passwords, secrets, credentials in a single pass with word boundaries
    result = re.sub(
        r"\b(password|pass|pwd|credentials?|login\s*id|secret|token)\s*(?:is|for|:|are|=)?\s*([^\s,.]+)",
        r"\1 [REDACTED_CREDENTIAL]",
        result,
        flags=re.IGNORECASE,
    )

    try:
        from memory.config_manager import redact_secret

        result = redact_secret(result)
    except Exception:
        pass
    return result


def is_sensitive_control(control_info: dict) -> bool:
    """Check if a UI control is a password, secret, or credential field."""
    if not isinstance(control_info, dict):
        return False

    # Check is_password attribute from accessibility tree
    if control_info.get("is_password") is True:
        return True

    # Check names, automation_ids, and roles
    name = str(control_info.get("name") or "")
    aid = str(control_info.get("automation_id") or "")
    ctype = str(control_info.get("control_type") or "")
    class_name = str(control_info.get("class_name") or "")

    for s in (name, aid, class_name):
        if _SENSITIVE_KEYWORD_RE.search(s):
            return True

    # Web input type password
    if control_info.get("type", "").lower() == "password":
        return True

    return False


_API_KEY_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_-]{20,}", re.IGNORECASE),
    re.compile(r"ghp_[a-zA-Z0-9]{20,}", re.IGNORECASE),
    re.compile(r"xoxb-[a-zA-Z0-9_-]{20,}", re.IGNORECASE),
    re.compile(r"eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"),  # JWT
    re.compile(r"bearer\s+[a-zA-Z0-9_.-]{20,}", re.IGNORECASE),
]

_PRIVACY_RISK_KEYWORDS = {
    "1password", "bitwarden", "keepass", "keepassxc", "lastpass", "dashlane",
    "enpass", "roboform", "credential manager", "password manager",
    "chase", "wells fargo", "bank of america", "citi", "paypal", "capital one",
    "barclays", "hsbc", "stripe checkout", "netbanking", "payment gateway",
    "windows security", "lockapp", "logonui", "user account control",
}


def redact_secret_with_length(control_info: Optional[dict], text_value: str) -> tuple[str, bool, int]:
    """
    Sanitize text input and redact secrets with placeholder including length.
    Returns:
        (sanitized_text, is_secret, secret_length)
    """
    if not text_value:
        return "", False, 0

    # 1. Check if control is flagged sensitive
    if control_info and is_sensitive_control(control_info):
        return f"[SECRET:length={len(text_value)}]", True, len(text_value)

    # 2. Check for token/key regex patterns
    for pat in _API_KEY_PATTERNS:
        if pat.search(text_value):
            return f"[SECRET:length={len(text_value)}]", True, len(text_value)

    # 3. Check for configured secrets
    try:
        from memory.config_manager import redact_secret
        clean = redact_secret(text_value)
        if clean != text_value:
            return f"[SECRET:length={len(text_value)}]", True, len(text_value)
    except Exception:
        pass

    return text_value, False, 0


def is_privacy_risk_window(
    title: str = "",
    app_name: str = "",
    process_name: str = "",
    url: Optional[str] = None,
) -> tuple[bool, str]:
    """Check if the active window/app/url belongs to a sensitive domain (password manager, banking, lock screen)."""
    combined = f"{title} {app_name} {process_name} {url or ''}".lower()

    # 1. Lock screen / OS Security
    for kw in ("windows security", "lockapp", "logonui", "user account control", "credential prompt"):
        if kw in combined:
            return True, "lock_screen"

    # 2. Password managers
    for kw in ("1password", "bitwarden", "keepass", "lastpass", "dashlane", "enpass", "roboform", "credential manager", "password manager"):
        if kw in combined:
            return True, "password_manager"

    # 3. Banking & financial
    for kw in ("chase", "wells fargo", "bank of america", "paypal", "capital one", "barclays", "hsbc", "netbanking", "checkout/pay"):
        if re.search(rf"\b{re.escape(kw)}\b", combined):
            return True, "banking"

    return False, ""


def sanitize_input_value(control_info: dict, text_value: str) -> str:
    """Mask or redact input value if control is sensitive."""
    if is_sensitive_control(control_info):
        return SENSITIVE_INPUT_PLACEHOLDER
    from memory.config_manager import redact_secret

    return redact_secret(text_value)


def classify_skill_risk(steps: list[StepDefinition], description: str = "") -> RiskLevel:
    """Determine the effective security RiskLevel for a skill using word-boundary matching."""
    full_text = (description or "").lower()
    for s in steps:
        thought_str = getattr(s, "thought", "") or ""
        tool_str = getattr(s, "tool", "") or ""
        params_str = str(getattr(s, "parameters", ""))
        full_text += f" {thought_str} {tool_str} {params_str}".lower()

    words = set(re.findall(r"\b\w+\b", full_text))

    for act in _DESTRUCTIVE_ACTIONS:
        if act in words:
            return RiskLevel.DESTRUCTIVE

    for act in _SENSITIVE_ACTIONS:
        if act in words:
            return RiskLevel.SENSITIVE

    # Check tools
    for s in steps:
        tool_name = getattr(s, "tool", "")
        if tool_name in ("computer_control", "windows_control", "dev_agent"):
            return RiskLevel.SENSITIVE

    return RiskLevel.NORMAL


def requires_explicit_confirmation(steps: list[Any], permissions: str = "") -> bool:
    """Check whether a list of steps or risk tier requires explicit confirmation."""
    if permissions in (RiskLevel.DESTRUCTIVE.value, "destructive"):
        return True
    for s in steps:
        if getattr(s, "is_sensitive", False) or (isinstance(s, dict) and s.get("is_sensitive")):
            return True
    return False


def requires_skill_confirmation(skill: SkillDefinition, parameters: dict | None = None) -> bool:
    """Check whether executing this skill requires explicit user confirmation."""
    if skill.permissions == RiskLevel.DESTRUCTIVE.value:
        return True
    if any(s.is_sensitive for s in skill.steps):
        return True
    return False
