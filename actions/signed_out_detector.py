"""
actions/signed_out_detector.py — Detection of signed-out or expired sessions across web services.

Detects when user sessions in the dedicated Jarvis browser profile have expired or are logged out:
- Gmail / Google Workspace
- WhatsApp Web
- ChatGPT / OpenAI
- Claude / Anthropic
- Spotify Web Player

Reports honest, actionable messages instructing the user to sign in manually.
Jarvis NEVER asks for, types, or stores user passwords.
"""

from __future__ import annotations

import re
from typing import Optional


def check_signed_out_status(
    url: str,
    title: str = "",
    html_text: str = "",
) -> Optional[str]:
    """
    Check if the current page URL, title, or content indicates a signed-out or expired session.
    Returns a plain-language notification string if signed out, else None.
    """
    clean_url = (url or "").lower().strip()
    clean_title = (title or "").lower().strip()
    clean_text = (html_text or "").lower()

    # 1. Gmail / Google Accounts
    if "mail.google.com" in clean_url or "accounts.google.com" in clean_url:
        if (
            "accounts.google.com/signin" in clean_url
            or "accounts.google.com/servicelogin" in clean_url
            or "accounts.google.com/v3/signin" in clean_url
            or "sign in - google accounts" in clean_title
            or "choose an account" in clean_text
            or "sign in to continue to gmail" in clean_text
            or "use your google account" in clean_text
        ):
            return "Gmail login expired, please sign in again in the Jarvis browser."

    # 2. WhatsApp Web
    if "web.whatsapp.com" in clean_url:
        if (
            "use whatsapp on your computer" in clean_text
            or "to use whatsapp on your computer" in clean_text
            or "link with phone number" in clean_text
            or "scan this code" in clean_text
            or "qr code" in clean_title
        ):
            return "WhatsApp Web session expired or unlinked. Please scan the QR code in the Jarvis browser."

    # 3. Spotify Web Player
    if "open.spotify.com" in clean_url:
        if (
            "/login" in clean_url
            or "log in to spotify" in clean_title
            or "sign up for spotify" in clean_text
            or ("log in" in clean_text and "sign up" in clean_text and "web player: music for everyone" in clean_title)
        ):
            return "Spotify Web login expired, please sign in again in the Jarvis browser."

    # 4. ChatGPT / OpenAI
    if "chatgpt.com" in clean_url or "chat.openai.com" in clean_url:
        if (
            "/auth/login" in clean_url
            or "log in" in clean_title
            or "welcome back" in clean_text and "log in" in clean_text
            or "get started" in clean_text and "log in" in clean_text and "sign up" in clean_text
        ):
            return "ChatGPT session expired, please sign in again in the Jarvis browser."

    # 5. Claude / Anthropic
    if "claude.ai" in clean_url:
        if (
            "/login" in clean_url
            or "sign in" in clean_title
            or "talk with claude" in clean_text and "sign in" in clean_text
        ):
            return "Claude session expired, please sign in again in the Jarvis browser."

    return None
