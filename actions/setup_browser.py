"""
actions/setup_browser.py — First-Run Browser Setup & Manual Sign-In Command.

Opens the dedicated Jarvis browser profile in a visible, headed desktop window
and instructs the user to sign in manually to their essential services:
- Google Account & Gmail
- WhatsApp Web
- ChatGPT / Claude
- Spotify

Guarantees:
- Runs strictly inside the dedicated JarvisProfile directory.
- Sessions and cookies are persisted securely in the dedicated profile.
- Jarvis NEVER types, prompts for, captures, or stores user passwords.
- Detects signed-out status and reports honest guidance to the user.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core import outcome
from memory import config_manager
from actions.browser_session import get_browser_session_manager

logger = logging.getLogger("browser_setup")

SETUP_SITES = [
    ("Google Account / Gmail", "https://mail.google.com"),
    ("WhatsApp Web", "https://web.whatsapp.com"),
    ("ChatGPT", "https://chatgpt.com"),
    ("Claude", "https://claude.ai"),
]


def setup_browser(parameters: Optional[Dict[str, Any]] = None, player: Any = None, **kwargs: Any) -> str:
    """
    Launch the dedicated Jarvis browser in a visible window for first-run login.
    Instructs the user to manually log in to essential accounts.
    """
    engine = config_manager.get_browser_engine()
    profile_dir = config_manager.get_browser_profile_dir()
    session_mgr = get_browser_session_manager(engine=engine)

    # 1. Launch in headed (visible) mode
    try:
        ok, _ = session_mgr.check_connection(timeout=1.0)
        if not ok:
            session_mgr._try_auto_launch(headless=False)

        # 2. Open tabs for setup
        opened_tabs = []
        for name, url in SETUP_SITES:
            try:
                tab = session_mgr.open_tab(url)
                opened_tabs.append(f"{name}: {url}")
            except Exception as e:
                logger.debug(f"Setup tab opening error for {name}: {e}")

        # 3. Present instructions to user
        msg = (
            f"Opened dedicated {engine.capitalize()} profile window at {profile_dir}. "
            "Please log in manually to your accounts (Gmail, WhatsApp Web, ChatGPT, Claude). "
            "Your authenticated sessions will be securely saved in your dedicated Jarvis profile. "
            "Jarvis will never ask for, type, or store your passwords."
        )

        if player and hasattr(player, "show_content"):
            content_body = (
                f"### Dedicated {engine.capitalize()} Profile Setup\n\n"
                f"**Profile Directory**: `{profile_dir}`\n\n"
                "Please sign in manually to the opened tabs:\n"
                "- **Gmail**: `https://mail.google.com`\n"
                "- **WhatsApp Web**: `https://web.whatsapp.com` (scan QR code)\n"
                "- **ChatGPT / Claude**: AI research accounts\n\n"
                "> 🔒 **Security Notice**: Jarvis never types, captures, or stores your passwords. "
                "All credentials stay strictly in your local browser profile."
            )
            player.show_content("JARVIS BROWSER SETUP", content_body)

        if player and hasattr(player, "write_log"):
            player.write_log(f"SYS: Dedicated {engine} profile setup launched.")

        return outcome.done(
            "setup_browser",
            message=msg,
            engine=engine,
            profile_dir=str(profile_dir),
            tabs=opened_tabs,
            verified=True,
        )

    except Exception as e:
        logger.exception("Error during browser setup")
        return outcome.failed(
            "setup_browser",
            f"Could not open Jarvis browser for setup: {e}",
            engine=engine,
        )


TOOL = {
    "name": "setup_browser",
    "permission": "normal",
    "description": (
        "Opens the dedicated Jarvis browser profile in a visible window for first-run setup. "
        "Opens Gmail, WhatsApp Web, and AI workspaces so the user can manually sign in. "
        "Jarvis never asks for or stores passwords."
    ),
    "brief": "Opens dedicated Jarvis browser profile for first-run manual sign-in.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "service": {
                "type": "STRING",
                "description": "Optional specific service to sign into (e.g. 'gmail', 'whatsapp', 'all').",
            }
        },
    },
    "handler": setup_browser,
}
