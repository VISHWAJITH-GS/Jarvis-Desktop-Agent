"""
core/apps/untrusted.py — Prompt-Injection Shield & Untrusted Content Wrapping.

All text derived from external web content (emails, chats, chatgpt responses)
MUST be wrapped in <<<UNTRUSTED <source>>>> markers.
Content inside can never trigger a tool call, change a recipient, or approve a gate.
"""

from __future__ import annotations

import re


def wrap_untrusted(source: str, content: str) -> str:
    """Wrap untrusted web-derived text with unambiguous safety delimiter markers."""
    clean_src = str(source or "web_content").strip().replace("\n", " ")
    clean_content = str(content or "")
    return f"<<<UNTRUSTED {clean_src}>>>\n{clean_content}\n<<<END UNTRUSTED {clean_src}>>>"


def is_untrusted(text: str) -> bool:
    """Check if text contains untrusted content markers."""
    return "<<<UNTRUSTED" in str(text or "")


def strip_untrusted_markers(text: str) -> str:
    """Strip delimiters for display or inspection."""
    cleaned = re.sub(r"<<<UNTRUSTED [^>]+>>>", "", str(text or ""))
    cleaned = re.sub(r"<<<END UNTRUSTED [^>]+>>>", "", cleaned)
    return cleaned.strip()
