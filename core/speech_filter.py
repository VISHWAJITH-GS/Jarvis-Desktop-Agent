"""
core/speech_filter.py — Audio artifact rejection and language-based transcript filtering.

Filters mis-transcribed audio artifacts, foreign language hallucinations,
and short echo/filler noise arriving while Jarvis is speaking or immediately after.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Set

logger = logging.getLogger("speech_filter")

# Script detection regexes for major languages
SCRIPT_PATTERNS = {
    "ko": re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]"),  # Korean Hangul
    "zh": re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]"),              # Chinese Hanzi
    "ja": re.compile(r"[\u3040-\u309f\u30a0-\u30ff]"),              # Japanese Hiragana/Katakana
    "ru": re.compile(r"[\u0400-\u04ff]"),                          # Cyrillic (Russian)
    "ar": re.compile(r"[\u0600-\u06ff]"),                          # Arabic
    "hi": re.compile(r"[\u0900-\u097f]"),                          # Devanagari (Hindi)
    "ta": re.compile(r"[\u0b80-\u0bff]"),                          # Tamil
}

# Known foreign hallucination words in Latin script when the language is not configured
FOREIGN_LATIN_TOKENS = {
    "es": {"gracias", "de nada", "hola", "confirmo", "por favor"},
    "it": {"grazie", "prego", "ciao", "confirmo"},
    "fr": {"merci", "bonjour", "s'il vous plaît", "sil vous plait"},
    "de": {"danke", "bitte", "hallo"},
    "pt": {"obrigado", "obrigada", "olá", "ola", "confirmo"},
}

FILLER_NOISE_RE = re.compile(
    r"^(?:[a-z]{1,2}|um|uh|ah|er|hmm?|mm|oh|eh|huh|mhm|uh-huh|ha|shh|huh\?)$",
    re.IGNORECASE,
)


def get_effective_stt_languages(allowed_langs: Optional[List[str]] = None) -> Set[str]:
    """Return normalized language codes (e.g. {'en', 'es'})."""
    if allowed_langs is None:
        try:
            from memory import config_manager
            allowed_langs = config_manager.get_stt_languages()
        except Exception:
            allowed_langs = ["en"]

    norm = set()
    for l in (allowed_langs or ["en"]):
        code = str(l).strip().lower().split("-")[0].split("_")[0]
        if code:
            norm.add(code)
    # English is always safe baseline
    norm.add("en")
    return norm


def is_allowed_language(text: str, allowed_langs: Optional[List[str]] = None) -> bool:
    """
    Returns False if text contains characters or tokens from an unconfigured language.
    """
    clean = text.strip()
    if not clean:
        return False

    norm_langs = get_effective_stt_languages(allowed_langs)

    # 1. Non-Latin script checks
    for script_code, pattern in SCRIPT_PATTERNS.items():
        if script_code not in norm_langs and pattern.search(clean):
            return False

    # 2. Known Latin foreign tokens check (e.g. 'confirmo', 'gracias')
    lower_words = set(re.findall(r"\b\w+\b", clean.lower()))
    for lang_code, tokens in FOREIGN_LATIN_TOKENS.items():
        if lang_code not in norm_langs:
            if lower_words.intersection(tokens):
                return False

    return True


def is_speech_noise(text: str, is_speaking_or_tail: bool = False) -> bool:
    """
    Returns True if turn consists solely of short noise or filler words,
    especially when arriving while Jarvis is speaking or immediately after.
    """
    clean = text.strip()
    if not clean:
        return True

    # Drop single character non-alphanumeric noise unconditionally
    if len(clean) == 1 and not clean.isalnum():
        return True

    # When speaking or in playback tail: drop very short noise and filler words
    if is_speaking_or_tail:
        if len(clean) <= 2:
            return True
        if FILLER_NOISE_RE.match(clean):
            return True

    return False


def should_drop_transcript(
    text: str,
    is_speaking_or_tail: bool = False,
    allowed_langs: Optional[List[str]] = None,
) -> tuple[bool, str]:
    """
    Authoritative check on whether an incoming transcript turn should be dropped.
    Returns (should_drop, reason).
    """
    clean = text.strip()
    if not clean:
        return True, "empty_transcript"

    if not is_allowed_language(clean, allowed_langs):
        return True, "disallowed_language"

    if is_speech_noise(clean, is_speaking_or_tail):
        return True, "speech_noise"

    return False, ""
