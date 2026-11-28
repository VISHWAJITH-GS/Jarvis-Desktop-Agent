"""
actions/_chat_extractors.py — Isolated DOM Extraction Engine for Chats and Webpages.

Extracts structured conversation messages (ChatGPT, Claude, generic web pages) via CDP:
1. Lazy scroll to stabilize full chat message history.
2. Isolates role markers: user vs assistant, excluding sidebars, headers, action buttons.
3. Preserves code blocks (<pre><code>) and paragraph structures.
4. Provides atomic file writing without overwriting and read-back verification.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import outcome

logger = logging.getLogger("chat_extractors")

# ── DOM Extraction JavaScript ────────────────────────────────────────────────

CHATGPT_EXTRACTOR_JS = """
() => {
    const messages = [];

    // 1. Find message containers
    // ChatGPT DOM structures:
    // - [data-message-author-role="user" | "assistant"]
    // - article with conversation-turn
    let nodes = document.querySelectorAll('[data-message-author-role]');
    if (!nodes || nodes.length === 0) {
        nodes = document.querySelectorAll('article');
    }

    // Helper: extract clean text preserving code blocks and excluding buttons
    const extractCleanText = (el) => {
        const clone = el.cloneNode(true);

        // Remove UI controls: buttons, copy buttons, feedback, svgs, edit buttons
        const junk = clone.querySelectorAll('button, svg, nav, [role="button"], [aria-label*="Copy"], [aria-label*="copy"], .sr-only');
        junk.forEach(j => j.remove());

        // Process code blocks: mark them cleanly with markdown fences
        const codeBlocks = clone.querySelectorAll('pre');
        codeBlocks.forEach(pre => {
            const codeEl = pre.querySelector('code');
            const codeText = codeEl ? codeEl.innerText : pre.innerText;
            const langClass = (codeEl ? codeEl.className : '') || '';
            const langMatch = langClass.match(/language-(\\w+)/);
            const lang = langMatch ? langMatch[1] : '';
            pre.innerText = '\\n```' + lang + '\\n' + codeText.trim() + '\\n```\\n';
        });

        return clone.innerText.trim();
    };

    nodes.forEach(node => {
        let role = node.getAttribute('data-message-author-role');
        if (!role) {
            // Check child elements for role marker
            const childWithRole = node.querySelector('[data-message-author-role]');
            if (childWithRole) {
                role = childWithRole.getAttribute('data-message-author-role');
            } else if (node.querySelector('[data-testid*="user"]') || node.className.includes('user')) {
                role = 'user';
            } else {
                role = 'assistant';
            }
        }

        // Locate main content container inside the turn
        let contentEl = node.querySelector('.markdown, [class*="markdown"], .whitespace-pre-wrap, .prose');
        if (!contentEl) {
            contentEl = node;
        }

        const text = extractCleanText(contentEl);
        if (text && text.length > 0) {
            messages.push({
                role: (role === 'user') ? 'user' : 'assistant',
                author: (role === 'user') ? 'You' : 'ChatGPT',
                text: text
            });
        }
    });

    return messages;
}
"""

GENERIC_PAGE_EXTRACTOR_JS = """
(selector) => {
    let root = selector ? document.querySelector(selector) : (document.querySelector('main, article, #content, [role="main"]') || document.body);
    if (!root) return [];

    const clone = root.cloneNode(true);
    // Remove scripts, styles, header, footer, nav
    const junk = clone.querySelectorAll('script, style, noscript, nav, header, footer, aside, button, svg, [role="navigation"]');
    junk.forEach(j => j.remove());

    // Format code blocks
    const codeBlocks = clone.querySelectorAll('pre');
    codeBlocks.forEach(pre => {
        const codeText = pre.innerText;
        pre.innerText = '\\n```\\n' + codeText.trim() + '\\n```\\n';
    });

    const text = clone.innerText.trim();
    if (!text) return [];

    return [{
        role: 'content',
        author: document.title || 'Page Content',
        text: text
    }];
}
"""


async def scroll_to_stabilize(page: Any, max_passes: int = 8) -> int:
    """
    Scroll to top and bottom to trigger dynamic/lazy message loading until
    the element count stabilizes (capped to max_passes).
    """
    prev_count = 0
    stable_streak = 0

    for _ in range(max_passes):
        try:
            # Scroll up slightly to load earlier messages
            await page.evaluate("""() => {
                const scroller = document.querySelector('div[class*="react-scroll-to-bottom"]') || document.scrollingElement || document.documentElement;
                if (scroller) scroller.scrollTop = 0;
            }""")
            await asyncio.sleep(0.15)

            # Scroll down to latest
            await page.evaluate("""() => {
                const scroller = document.querySelector('div[class*="react-scroll-to-bottom"]') || document.scrollingElement || document.documentElement;
                if (scroller) scroller.scrollTop = scroller.scrollHeight;
            }""")
            await asyncio.sleep(0.15)

            # Count current turns
            count = await page.evaluate("() => document.querySelectorAll('[data-message-author-role], article').length")
            if count == prev_count and count > 0:
                stable_streak += 1
                if stable_streak >= 2:
                    break
            else:
                stable_streak = 0
                prev_count = count
        except Exception:
            break

    return prev_count


async def extract_messages_from_page(page: Any, selector: Optional[str] = None) -> List[Dict[str, str]]:
    """
    Extract conversation messages from live Playwright Page or local HTML.
    """
    url = (getattr(page, "url", "") or "").lower()

    # If selector explicitly provided:
    if selector:
        raw = await page.evaluate(GENERIC_PAGE_EXTRACTOR_JS, selector)
        return raw or []

    # Check if ChatGPT or page has chat structure
    is_chatgpt = "chatgpt.com" in url or "chat.openai.com" in url
    has_chat_roles = False
    try:
        has_chat_roles = bool(await page.evaluate("() => Boolean(document.querySelector('[data-message-author-role]'))"))
    except Exception:
        pass

    if is_chatgpt or has_chat_roles:
        await scroll_to_stabilize(page, max_passes=6)
        raw = await page.evaluate(CHATGPT_EXTRACTOR_JS)
        if raw:
            return raw

    # Check if Claude
    if "claude.ai" in url:
        await scroll_to_stabilize(page, max_passes=4)
        raw = await page.evaluate(CHATGPT_EXTRACTOR_JS)
        if raw:
            for item in raw:
                if item.get("author") == "ChatGPT":
                    item["author"] = "Claude"
            return raw

    # Fallback: generic extraction
    raw = await page.evaluate(GENERIC_PAGE_EXTRACTOR_JS, None)
    return raw or []


def format_conversation_text(messages: List[Dict[str, str]], title: str = "", format_type: str = "txt") -> str:
    """
    Format extracted message dicts into a readable text or markdown document:
      You: ...
      ChatGPT: ...
    """
    lines: List[str] = []
    if title:
        if format_type == "md":
            lines.append(f"# {title}\n")
        else:
            lines.append(f"{title}\n{'=' * len(title)}\n")

    for msg in messages:
        author = msg.get("author") or ("You" if msg.get("role") == "user" else "Assistant")
        body = msg.get("text", "").strip()
        if format_type == "md":
            lines.append(f"### {author}\n\n{body}\n")
        else:
            lines.append(f"{author}:\n{body}\n")

    return "\n".join(lines).strip() + "\n"


def write_file_atomic(dest_folder: Path, base_filename: str, content: str) -> Path:
    """
    Atomically write content to file.
    NEVER overwrites an existing file: adds numeric suffix e.g. 'chat (1).txt'.
    """
    dest_folder = dest_folder.resolve()
    dest_folder.mkdir(parents=True, exist_ok=True)

    clean_name = base_filename.strip().strip("'\"")
    # Sanitize invalid Windows path characters
    clean_name = re.sub(r'[<>:"/\\|?*]', '_', clean_name)
    if not clean_name:
        clean_name = "chat_export.txt"

    stem = Path(clean_name).stem
    suffix = Path(clean_name).suffix or ".txt"

    candidate = dest_folder / f"{stem}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = dest_folder / f"{stem} ({counter}){suffix}"
        counter += 1

    temp_path = dest_folder / f".tmp_{candidate.stem}_{uuid.uuid4().hex[:6]}{suffix}"
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(candidate)
    return candidate


def verify_written_file(path: Path, expected_messages: List[Dict[str, str]], min_chars: int) -> Tuple[bool, str]:
    """
    Read back file from disk and strictly verify:
    - Non-empty (size > 0)
    - Character / message count matches
    - First and last messages appear in the file text
    """
    if not path.exists():
        return False, f"Target file '{path.name}' was not found on disk after write."

    size = path.stat().st_size
    if size == 0:
        return False, f"Target file '{path.name}' is empty (0 bytes)."

    file_text = path.read_text(encoding="utf-8")
    if len(file_text) < min_chars * 0.8:
        return (
            False,
            f"File content length ({len(file_text)} chars) is significantly shorter than extracted ({min_chars} chars).",
        )

    if expected_messages:
        first_sample = (expected_messages[0].get("text") or "")[:25].strip()
        last_sample = (expected_messages[-1].get("text") or "")[:25].strip()
        if first_sample and first_sample not in file_text:
            return False, f"Verification failed: initial message content missing from file."
        if last_sample and last_sample not in file_text:
            return False, f"Verification failed: trailing message content missing from file."

    return True, f"Verified: {len(expected_messages)} messages ({size:,} bytes) written successfully."
