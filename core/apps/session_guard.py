"""
core/apps/session_guard.py — Pre-Execution Logged-In Verification Guard.

Checks that the user is logged into the target app before attempting actions.
If logged out, returns status=needs_login instead of wandering or failing silently.
"""

from __future__ import annotations

import logging
from typing import Tuple

from core.apps.app_tab import AppTab

logger = logging.getLogger("session_guard")


class SessionGuard:
    @classmethod
    def check_logged_in(cls, tab: AppTab) -> Tuple[bool, str]:
        """Verify the user's active session is authenticated."""
        app = tab.app_name.lower().strip()
        url = tab.url().lower()

        if app == "whatsapp":
            # Check for QR canvas or intro screen
            qr = tab.wait_for_selector('canvas[aria-label*="Scan"], [data-testid="qrcode"]', timeout_sec=1.0)
            if qr:
                return False, "WhatsApp Web is not logged in; please scan the QR code in your Jarvis Chrome window."
            # Check for chat list or search bar
            chat_list = tab.wait_for_selector('#side, [data-testid="chat-list"], [data-testid="chat-list-search"]', timeout_sec=2.0)
            if chat_list:
                return True, "WhatsApp Web is ready."
            return True, "WhatsApp Web state checked."

        elif app == "gmail":
            # Check for accounts.google.com or sign-in prompt
            if "accounts.google.com" in url or "signin" in url:
                return False, "Gmail is not signed in; please sign into Google in your Jarvis Chrome profile."
            login_field = tab.wait_for_selector('input[type="email"], #identifierId', timeout_sec=0.8)
            if login_field:
                return False, "Gmail login screen detected; please sign into your Google account."
            return True, "Gmail is logged in."

        elif app == "spotify":
            if "accounts.spotify.com" in url or "login" in url:
                return False, "Spotify Web is not logged in; please log into Spotify in Jarvis Chrome."
            login_btn = tab.wait_for_selector('button[data-testid="login-button"]', timeout_sec=0.8)
            if login_btn and not tab.wait_for_selector('[data-testid="user-widget-link"]', timeout_sec=0.8):
                return False, "Spotify Web is not logged in; please log into Spotify."
            return True, "Spotify Web is logged in."

        elif app == "chatgpt":
            if "login" in url or "auth" in url:
                return False, "ChatGPT is not logged in; please sign into ChatGPT in your Jarvis Chrome profile."
            login_btn = tab.wait_for_selector('button[data-testid="login-button"]', timeout_sec=0.8)
            prompt_box = tab.wait_for_selector('#prompt-textarea', timeout_sec=1.5)
            if login_btn and not prompt_box:
                return False, "ChatGPT is not logged in; please log into ChatGPT in Jarvis Chrome."
            return True, "ChatGPT is logged in."

        return True, "Session active."
