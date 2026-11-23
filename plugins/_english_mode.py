"""
plugins/_english_mode.py — English Placement Practice Mode Engine for MARK LVI / JARVIS.

Designed for college students preparing for campus recruitment, technical interviews,
and project presentations.

Features:
- Prompt overlay forcing professional English communication regardless of STT input
- Structured practice scenarios:
  1. self_intro (Campus placement self-introduction)
  2. hr_round (STAR methodology: Situation, Task, Action, Result)
  3. project_explain (Technical architecture, trade-offs, hardest challenges)
  4. code_walkthrough (Clear verbal explanation of code logic)
  5. group_discussion (Simulating peer participants and active debate)
  6. presentation (2-minute timed structured presentation)
- Feedback engine:
  - Maximum 2 actionable corrections per answer: [What was said, Polished rewrite, One-line reason]
  - Filler word tracking ('um', 'like', 'basically', 'actually', 'you know')
  - Structure and answer length analysis
  - Support for 3 feedback styles: 'after_answer', 'end_of_session', 'on_request'
  - Encouraging, confidence-building tone
- Mistake log in memory/english_mistakes.json
- Flashcard generator integrated with player.show_quiz
- Honest declared limits: Grammar/vocabulary is reliable; pronunciation over STT is not;
  does not replace human mock interviews.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core import platform_hooks
from memory import config_manager

logger = logging.getLogger(__name__)

_ROOT_DIR = Path(__file__).parent.parent
_MEMORY_DIR = _ROOT_DIR / "memory"
_MISTAKES_FILE = _MEMORY_DIR / "english_mistakes.json"

_lock = threading.RLock()

# Scenarios
SCENARIOS = {
    "self_intro": {
        "title": "Campus Placement Self-Introduction",
        "description": "Structure your elevator pitch: education, tech stack, major project, and core strength.",
        "guidelines": (
            "Guide the student through a 60-90 second self-introduction. "
            "Ensure they cover: 1) Degree and college, 2) Technical skill stack, 3) 1 standout project with impact, "
            "4) Why they are interested in engineering roles. Keep the structure crisp and confident."
        ),
    },
    "hr_round": {
        "title": "Behavioral HR Interview (STAR Method)",
        "description": "Practice answering behavioral questions using Situation, Task, Action, and Result.",
        "guidelines": (
            "Ask classic behavioral interview questions (e.g., 'Tell me about a time you had a conflict in a team', "
            "'Describe a failure and what you learned', 'Why should we hire you?'). "
            "Enforce the STAR method (Situation, Task, Action, Result). If an answer lacks an Action or Result, "
            "kindly prompt them to expand on the missing part."
        ),
    },
    "project_explain": {
        "title": "Technical Project Deep-Dive",
        "description": "Explain system architecture, technical choices, and the hardest bug you solved.",
        "guidelines": (
            "Act as a senior engineering interviewer. Ask the student to introduce their best project. "
            "Follow up with technical probes: 'Why did you choose this database over alternatives?', "
            "'What was the bottleneck or hardest bug you encountered and how did you profile it?', "
            "'How would your architecture scale if traffic grew 100x?'."
        ),
    },
    "code_walkthrough": {
        "title": "Code Walkthrough & Logic Explanation",
        "description": "Practice explaining algorithms, data structures, and code aloud clearly.",
        "guidelines": (
            "Prompt the student to explain a piece of code (they may read it aloud or copy it to the clipboard). "
            "Coach them to explain the algorithm top-down: first high-level approach and time/space complexity, "
            "then key data structures, then edge case handling. Discourage line-by-line mechanical reading."
        ),
    },
    "group_discussion": {
        "title": "Group Discussion (GD) Simulation",
        "description": "Simulate a 4-person campus GD. JARVIS plays peer candidates with different viewpoints.",
        "guidelines": (
            "Introduce a GD topic (e.g., 'AI in Software Engineering: Threat or Opportunity?', 'Work from Office vs Remote'). "
            "Simulate other candidates ('Rahul', 'Ananya') interjecting with polite points. "
            "Teach the student how to enter the discussion politely ('Building on Rahul's point...', 'I see your point, however...'), "
            "bring back derailed conversations, and summarize effectively."
        ),
    },
    "presentation": {
        "title": "2-Minute Technical Presentation",
        "description": "Deliver a concise 2-minute presentation with an opening hook, 3 key points, and conclusion.",
        "guidelines": (
            "Give the student a technical topic or let them pick one. Time their talk. "
            "Evaluate opening hook, 3 clearly structured main points, transition phrases ('Moving on to...', 'Furthermore...'), "
            "and a strong concluding takeaway. Warn if they rush or speak too slowly."
        ),
    },
}

FEEDBACK_STYLES = ["after_answer", "end_of_session", "on_request"]

COMMON_FILLER_WORDS = [
    r"\bum\b",
    r"\buh\b",
    r"\ber\b",
    r"\blike\b",
    r"\bbasically\b",
    r"\bactually\b",
    r"\byou know\b",
    r"\bkind of\b",
    r"\bsort of\b",
    r"\bliterally\b",
    r"\bhonestly\b",
    r"\bI mean\b",
]


# ─────────────────────────────────────────────────────────────────────────────
#  Mistake Log & Analytics
# ─────────────────────────────────────────────────────────────────────────────


def _load_mistakes_data() -> dict:
    with _lock:
        if not _MISTAKES_FILE.exists():
            return {
                "mistakes": [],
                "analytics": {
                    "total_answers": 0,
                    "total_words": 0,
                    "filler_counts": {},
                },
            }
        try:
            with open(_MISTAKES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[EnglishMode] Failed reading mistakes file: {e}")
            return {
                "mistakes": [],
                "analytics": {
                    "total_answers": 0,
                    "total_words": 0,
                    "filler_counts": {},
                },
            }


def _save_mistakes_data(data: dict) -> None:
    with _lock:
        try:
            _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            with open(_MISTAKES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"[EnglishMode] Failed writing mistakes file: {e}")


def log_mistake(
    wrong_phrase: str,
    corrected_phrase: str,
    reason: str,
    scenario: str = "",
) -> dict:
    """Save an identified mistake and correction to memory/english_mistakes.json."""
    entry = {
        "id": f"m_{int(time.time()*1000)}",
        "wrong_phrase": str(wrong_phrase).strip(),
        "corrected_phrase": str(corrected_phrase).strip(),
        "reason": str(reason).strip(),
        "scenario": str(scenario).strip() or "general",
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reviewed_count": 0,
    }
    with _lock:
        data = _load_mistakes_data()
        data["mistakes"].append(entry)
        _save_mistakes_data(data)
    return entry


def get_mistake_history(limit: int = 50) -> list[dict]:
    """Return recent mistake log entries."""
    data = _load_mistakes_data()
    mistakes = data.get("mistakes", [])
    return list(reversed(mistakes[-limit:]))


def count_filler_words(text: str) -> dict[str, int]:
    """Analyze text and count common English filler words."""
    counts: dict[str, int] = {}
    clean = text.lower()
    for pattern in COMMON_FILLER_WORDS:
        word = pattern.replace(r"\b", "")
        matches = re.findall(pattern, clean)
        if matches:
            counts[word] = len(matches)
    return counts


def record_answer_metrics(user_text: str) -> dict:
    """Record word count and filler words for an answer turn."""
    words = len(user_text.split())
    fillers = count_filler_words(user_text)

    with _lock:
        data = _load_mistakes_data()
        analytics = data.setdefault(
            "analytics", {"total_answers": 0, "total_words": 0, "filler_counts": {}}
        )
        analytics["total_answers"] = analytics.get("total_answers", 0) + 1
        analytics["total_words"] = analytics.get("total_words", 0) + words

        fc = analytics.setdefault("filler_counts", {})
        for word, cnt in fillers.items():
            fc[word] = fc.get(word, 0) + cnt

        _save_mistakes_data(data)

    return {"words": words, "fillers": fillers}


def get_analytics_summary() -> dict:
    """Return aggregated metrics (total answers, average length, top fillers)."""
    data = _load_mistakes_data()
    analytics = data.get("analytics", {})
    total_answers = analytics.get("total_answers", 0)
    total_words = analytics.get("total_words", 0)
    avg_len = round(total_words / total_answers, 1) if total_answers > 0 else 0
    filler_counts = analytics.get("filler_counts", {})
    sorted_fillers = sorted(filler_counts.items(), key=lambda x: x[1], reverse=True)

    return {
        "total_answers": total_answers,
        "total_words": total_words,
        "average_answer_length": avg_len,
        "total_mistakes_logged": len(data.get("mistakes", [])),
        "top_fillers": sorted_fillers[:5],
    }


def generate_flashcards(max_cards: int = 20) -> list[dict]:
    """
    Export logged mistakes into flashcards compatible with player.show_quiz.
    Format:
    [
        {
            "question": "Improve this phrase: 'I am having 3 years experience...'",
            "options": [],
            "answer": "I have 3 years of experience...",
            "note": "Use present simple with state verbs."
        }
    ]
    """
    data = _load_mistakes_data()
    mistakes = data.get("mistakes", [])
    cards = []
    for m in reversed(mistakes[-max_cards:]):
        wrong = m.get("wrong_phrase", "")
        corr = m.get("corrected_phrase", "")
        note = m.get("reason", "")
        if wrong and corr:
            cards.append(
                {
                    "question": f"Improve this phrase: \"{wrong}\"",
                    "options": [],  # Typed answer or spoken review
                    "answer": corr,
                    "note": note,
                }
            )
    return cards


def show_mistake_quiz(player=None) -> str:
    """Launch an interactive Qt review quiz of recent logged English mistakes."""
    cards = generate_flashcards(max_cards=15)
    if not cards:
        return "You have no logged English mistakes yet! Practice an interview scenario to start logging feedback."

    if player and hasattr(player, "show_quiz"):
        player.show_quiz(
            topic="English Placement Practice Review",
            questions=cards,
            grade=None,  # Assistant judges answer
        )
        return f"Loaded {len(cards)} mistake flashcards in the HUD for review."
    return f"Prepared {len(cards)} flashcards from your mistake log."


# ─────────────────────────────────────────────────────────────────────────────
#  System Prompt Overlay
# ─────────────────────────────────────────────────────────────────────────────


def get_prompt_overlay(
    scenario: Optional[str] = None,
    feedback_style: Optional[str] = None,
) -> str:
    """
    Generate the dynamic system prompt overlay applied during English Placement Practice Mode.
    Forced English, max 2 corrections, scenario guidelines, and honest capability limits.
    """
    cfg = config_manager.get_mode_settings("english_practice")
    scen_key = str(scenario or cfg.get("scenario", "self_intro")).strip().lower()
    style = str(feedback_style or cfg.get("feedback_style", "after_answer")).strip().lower()

    scen_info = SCENARIOS.get(scen_key, SCENARIOS["self_intro"])
    scen_title = scen_info["title"]
    scen_guidelines = scen_guidelines = scen_info["guidelines"]

    style_instructions = {
        "after_answer": (
            "FEEDBACK TIMING: Provide feedback IMMEDIATELY after each answer before asking the next question."
        ),
        "end_of_session": (
            "FEEDBACK TIMING: Do NOT interrupt with corrections after each turn. Conduct the mock session naturally, "
            "and give a structured review at the end when the user says 'finish' or 'how did I do?'."
        ),
        "on_request": (
            "FEEDBACK TIMING: Conduct the session naturally without unprompted corrections. Provide corrections ONLY "
            "when the user explicitly asks for feedback."
        ),
    }.get(style, "Provide feedback immediately after each answer.")

    overlay = f"""
================================================================================
[MODE ACTIVE: ENGLISH PLACEMENT PRACTICE MODE — {scen_title.upper()}]
================================================================================

YOU ARE NOW AN EXPERT CAMPUS RECRUITMENT INTERVIEWER AND ENGLISH COMMUNICATION COACH.
The user is a college student preparing for competitive campus placements, technical interviews,
and corporate communications.

1. LANGUAGE POLICY (STRICT):
   - You MUST respond in clear, professional, fluent ENGLISH ONLY at all times.
   - Ignore any general directive to reply in the user's native language.
   - Exception: If the user explicitly asks in their native language for a translation or explanation,
     you may provide a brief one-sentence explanation in their language, then immediately switch back to English.

2. CURRENT SCENARIO: {scen_title}
   {scen_guidelines}

3. FEEDBACK RULES (STRICT QUALITY BAR):
   {style_instructions}
   - QUANTITY LIMIT: Give AT MOST TWO (<= 2) actionable corrections per answer. Never overwhelm the user.
   - FEEDBACK FORMAT:
     * Original: [Quote the exact phrase the user said]
     * Polished Rewrite: [Provide a concise, professional corporate-English alternative]
     * Why: [One-line clear reason regarding grammar, technical vocabulary, conciseness, or filler words]
   - FOCUS CRITERIA:
     * Check for unnecessary filler words ('um', 'like', 'basically', 'actually', 'you know').
     * Check answer structure (e.g. STAR method for HR; problem-solution-impact for projects).
     * Check technical vocabulary accuracy (e.g. avoid vague terms like 'I did the database work' -> 'I designed the relational schema').
   - ENCOURAGEMENT: Always maintain a warm, highly encouraging, and constructive tone. Acknowledge what was good before correcting.
   - RETRY PROMPT: After offering a rewrite, briefly invite the user to retry the sentence aloud.

4. THINKING PAUSES & PACING:
   - Allow thinking pauses. If the user pauses briefly to formulate thoughts, do NOT interrupt prematurely.
   - Encourage using push-to-talk if they need extra time to organize their thoughts.

5. HONEST CAPABILITY LIMITS (DO NOT DECEIVE):
   - Grammar, phrasing, vocabulary, and structural coaching are reliable.
   - Real-time phonetic pronunciation and acoustic accent coaching over speech-to-text are NOT reliable.
   - Remind the student that this practice complements, but does not replace, live mock interviews with human mentors.
================================================================================
"""
    return overlay.strip()


# ─────────────────────────────────────────────────────────────────────────────
#  Activation / Deactivation API
# ─────────────────────────────────────────────────────────────────────────────


def activate_english_mode(
    scenario: str = "self_intro",
    feedback_style: str = "after_answer",
    player=None,
) -> tuple[bool, str]:
    """Turn on English Placement Practice Mode and trigger mid-session prompt reload."""
    if scenario not in SCENARIOS:
        scenario = "self_intro"
    if feedback_style not in FEEDBACK_STYLES:
        feedback_style = "after_answer"

    # Persist in config
    config_manager.update_mode_settings(
        "english_practice",
        {
            "scenario": scenario,
            "feedback_style": feedback_style,
            "silence_threshold_ms": 3500,
        },
    )
    config_manager.set_active_mode("english_practice")

    scen_title = SCENARIOS[scenario]["title"]
    msg = (
        f"English Placement Practice Mode is now ACTIVE ({scen_title}). "
        f"Feedback style: {feedback_style.replace('_', ' ')}. "
        "I am ready for your interview practice. Please speak in English!"
    )

    if player and hasattr(player, "write_log"):
        player.write_log(f"JARVIS: {msg}")

    # Request live session reconnection to apply prompt overlay while keeping context
    try:
        platform_hooks.call_hook(
            "request_session_reconnect",
            keep_context=True,
            reason=f"English Practice Mode ({scen_title})",
        )
    except Exception as e:
        logger.debug(f"[EnglishMode] Reconnect hook call: {e}")

    return True, msg


def deactivate_english_mode(player=None) -> tuple[bool, str]:
    """Turn off English Placement Practice Mode and restore standard assistant prompt."""
    if config_manager.get_active_mode() == "english_practice":
        config_manager.set_active_mode(None)

    msg = "English Placement Practice Mode has been DEACTIVATED. Standard assistant mode restored."

    if player and hasattr(player, "write_log"):
        player.write_log(f"JARVIS: {msg}")

    # Reconnect to drop English overlay
    try:
        platform_hooks.call_hook(
            "request_session_reconnect",
            keep_context=True,
            reason="Exiting English Practice Mode",
        )
    except Exception as e:
        logger.debug(f"[EnglishMode] Reconnect hook call: {e}")

    return True, msg


def get_english_mode_status() -> dict:
    """Return status and configuration of English Practice Mode."""
    active = config_manager.get_active_mode() == "english_practice"
    cfg = config_manager.get_mode_settings("english_practice")
    analytics = get_analytics_summary()
    return {
        "active": active,
        "scenario": cfg.get("scenario", "self_intro"),
        "scenario_title": SCENARIOS.get(cfg.get("scenario", "self_intro"), {}).get("title", ""),
        "feedback_style": cfg.get("feedback_style", "after_answer"),
        "silence_threshold_ms": cfg.get("silence_threshold_ms", 3500),
        "analytics": analytics,
    }
