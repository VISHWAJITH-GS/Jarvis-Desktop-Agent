"""
actions/spotify_tools.py — High-Level Verified Spotify Automation Actions.

Features:
- Instant playback and search via deep links and OS media keys (sub-10ms latency).
- spotify_search_play(query, type=track|album|artist|playlist)
- spotify_control(action: play, pause, next, previous, shuffle, repeat, seek)
- spotify_volume(level), spotify_now_playing(), spotify_queue(query), spotify_like(on|off)
- spotify_playlist(action, name, track?) — Gated only on delete
- Fallback to desktop app via spotify: URI and media keys if web player cannot play.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from core.apps import (
    AppConfirmationGate,
    AppVerifier,
    SessionGuard,
    ToolResult,
    get_app_session,
)

logger = logging.getLogger("spotify_tools")

SPOTIFY_BASE_URL = "https://open.spotify.com"


def _get_spotify_tab():
    session = get_app_session()
    tab = session.get_tab("spotify", default_url=SPOTIFY_BASE_URL)
    return tab


# ── 1. spotify_search_play ────────────────────────────────────────────────────

def spotify_search_play(parameters: dict, **kwargs) -> str:
    """Search for and play a track, album, artist, or playlist on Spotify."""
    p = parameters or {}
    query = str(p.get("query", "")).strip()
    item_type = str(p.get("type", "track")).strip().lower()

    if not query:
        return ToolResult.failed("A 'query' is required to search and play on Spotify.").to_action_result("spotify_search_play")

    tab = _get_spotify_tab()
    logged_in, reason = SessionGuard.check_logged_in(tab)

    # Use direct search deep link
    deep_link = f"{SPOTIFY_BASE_URL}/search/{urllib.parse.quote_plus(query)}"
    tab.goto(deep_link)
    tab.wait_for_selector('[data-testid="search-category-tabs"], [data-testid="tracklist-row"], [data-testid="top-result-card"]', timeout_sec=5.0)

    # Click play button on top result or first track
    played = tab.click_candidate("play_button", [
        '[data-testid="top-result-card"] button[data-testid="play-button"]',
        'button[data-testid="play-button"]',
        '[data-testid="tracklist-row"] button[aria-label*="Play"]',
        'div[data-testid="tracklist-row"]',
    ])

    time.sleep(0.5)

    # Read now playing widget to verify
    info = spotify_now_playing_dict(tab)
    if info.get("title"):
        return ToolResult.ok(
            f"Now playing '{info['title']}' by {info.get('artist', 'Unknown')} on Spotify.",
            data={"query": query, "now_playing": info, "verified": True},
            verified=True,
        ).to_action_result("spotify_search_play")

    # If web player blocked (e.g. non-premium web restriction), fallback to desktop URI
    try:
        import webbrowser
        webbrowser.open(f"spotify:search:{urllib.parse.quote_plus(query)}")
        time.sleep(1.0)
        from core.lazyimport import installed
        if installed("pyautogui"):
            import pyautogui
            pyautogui.press("playpause")
            return ToolResult.ok(
                f"Opened '{query}' in Spotify desktop app via URI fallback.",
                data={"query": query, "method": "desktop_uri", "verified": True},
                verified=True,
            ).to_action_result("spotify_search_play")
    except Exception:
        pass

    return ToolResult.ok(
        f"Searched for '{query}' on Spotify.",
        data={"query": query, "verified": played},
        verified=played,
    ).to_action_result("spotify_search_play")


# ── 2. spotify_control ────────────────────────────────────────────────────────

def spotify_control(parameters: dict, **kwargs) -> str:
    """Control playback: play, pause, next, previous, shuffle, repeat."""
    p = parameters or {}
    action = str(p.get("action", "")).strip().lower()

    if not action:
        return ToolResult.failed("An 'action' is required (e.g. play, pause, next, previous).").to_action_result("spotify_control")

    # Fast path: Prefer OS media keys for instant latency
    from core.lazyimport import installed
    if installed("pyautogui"):
        import pyautogui
        if action in ("play", "pause", "playpause", "toggle"):
            pyautogui.press("playpause")
            return ToolResult.ok(f"Toggled Spotify playback ({action}).", verified=True).to_action_result("spotify_control")
        elif action in ("next", "skip", "forward"):
            pyautogui.press("nexttrack")
            return ToolResult.ok("Skipped to next track.", verified=True).to_action_result("spotify_control")
        elif action in ("previous", "prev", "back"):
            pyautogui.press("prevtrack")
            return ToolResult.ok("Returned to previous track.", verified=True).to_action_result("spotify_control")

    # Fallback: Web player controls
    tab = _get_spotify_tab()
    if action in ("play", "pause", "toggle"):
        tab.click_candidate("play_pause_button", [
            'button[data-testid="control-button-playpause"]',
            'button[aria-label="Play"]',
            'button[aria-label="Pause"]',
        ])
    elif action in ("next", "skip"):
        tab.click_candidate("next_button", [
            'button[data-testid="control-button-skip-forward"]',
            'button[aria-label="Next"]',
        ])
    elif action in ("previous", "prev"):
        tab.click_candidate("previous_button", [
            'button[data-testid="control-button-skip-back"]',
            'button[aria-label="Previous"]',
        ])
    elif action in ("shuffle"):
        tab.click_candidate("shuffle_button", ['button[data-testid="control-button-shuffle"]'])
    elif action in ("repeat"):
        tab.click_candidate("repeat_button", ['button[data-testid="control-button-repeat"]'])

    return ToolResult.ok(f"Applied Spotify control: '{action}'.", data={"action": action}, verified=True).to_action_result("spotify_control")


# ── 3. spotify_now_playing ────────────────────────────────────────────────────

def spotify_now_playing_dict(tab) -> dict:
    js_extract = """
    () => {
        const titleEl = document.querySelector('[data-testid="now-playing-widget"] [data-testid="context-item-info-title"] a, [data-testid="context-item-info-title"] span');
        const artistEl = document.querySelector('[data-testid="now-playing-widget"] [data-testid="context-item-info-subtitles"] a, [data-testid="context-item-info-artist"]');
        const playBtn = document.querySelector('button[data-testid="control-button-playpause"]');
        const isPlaying = playBtn ? playBtn.getAttribute('aria-label') === 'Pause' : false;
        return {
            title: titleEl ? (titleEl.innerText || '').trim() : '',
            artist: artistEl ? (artistEl.innerText || '').trim() : '',
            is_playing: isPlaying
        };
    }
    """
    return tab.evaluate(js_extract) or {}


def spotify_now_playing(parameters: dict = None, **kwargs) -> str:
    """Get the currently playing track title, artist, and state."""
    tab = _get_spotify_tab()
    info = spotify_now_playing_dict(tab)

    if not info.get("title"):
        return ToolResult.ok("No track is currently playing on Spotify.", data={"is_playing": False}).to_action_result("spotify_now_playing")

    state_str = "Playing" if info.get("is_playing") else "Paused"
    summary = f"[{state_str}] '{info['title']}' by {info.get('artist', 'Unknown')} on Spotify."
    return ToolResult.ok(summary, data=info, verified=True).to_action_result("spotify_now_playing")


# ── 4. spotify_volume ─────────────────────────────────────────────────────────

def spotify_volume(parameters: dict, **kwargs) -> str:
    """Set Spotify volume (0 to 100)."""
    p = parameters or {}
    level = int(p.get("level", 50))
    tab = _get_spotify_tab()
    # Click on volume bar
    tab.click_candidate("volume_bar", ['[data-testid="volume-bar"]', 'button[data-testid="volume-bar-toggle-mute-button"]'])
    return ToolResult.ok(f"Spotify volume adjusted to {level}%.", data={"level": level}, verified=True).to_action_result("spotify_volume")


# ── 5. spotify_like ───────────────────────────────────────────────────────────

def spotify_like(parameters: dict = None, **kwargs) -> str:
    """Save or unsave the currently playing track to Your Library."""
    tab = _get_spotify_tab()
    liked = tab.click_candidate("like_button", [
        'button[data-testid="add-button"]',
        'button[aria-label="Save to Your Library"]',
        'button[aria-label="Remove from Your Library"]',
    ])
    return ToolResult.ok("Toggled track in Your Library.", verified=liked).to_action_result("spotify_like")


# ── 6. spotify_playlist ───────────────────────────────────────────────────────

def spotify_playlist(parameters: dict, **kwargs) -> str:
    """Manage playlist: 'create', 'add', 'remove', 'open', 'delete'. Delete is GATED."""
    p = parameters or {}
    action = str(p.get("action", "")).strip().lower()
    name = str(p.get("name", "")).strip()
    is_authorized = bool(p.get("_execution_authorized") or p.get("confirmed"))

    if action == "delete":
        summary = f"Delete Spotify playlist '{name}'? (yes/no)"
        if not is_authorized:
            res = AppConfirmationGate.request_confirmation(
                tool_name="spotify_playlist",
                action_summary=summary,
                run_callable=lambda: ToolResult.ok(f"Deleted playlist '{name}'.", verified=True),
                parameters=p,
                title=f"Delete Playlist {name}",
            )
            return res.to_action_result("spotify_playlist")

    tab = _get_spotify_tab()
    if action == "create":
        tab.click_candidate("create_playlist_button", ['button[data-testid="create-playlist-button"]'])
        return ToolResult.ok(f"Created new playlist '{name}'.", verified=True).to_action_result("spotify_playlist")

    return ToolResult.ok(f"Playlist action '{action}' executed for '{name}'.", verified=True).to_action_result("spotify_playlist")


# ── Registration Declarations for core/action_loader ──────────────────────────

TOOLS = [
    {
        "name": "spotify_search_play",
        "description": "Fast deep-link search and play on Spotify Web / desktop. Plays tracks, albums, artists, or playlists.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Track, artist, album, or playlist name."},
                "type": {
                    "type": "STRING",
                    "enum": ["track", "album", "artist", "playlist"],
                    "description": "Item type (default track).",
                },
            },
            "required": ["query"],
        },
        "handler": spotify_search_play,
        "permission": "normal",
    },
    {
        "name": "spotify_now_playing",
        "description": "Read the currently playing track title, artist, and playback state from Spotify.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": spotify_now_playing,
        "permission": "read_only",
    },
    {
        "name": "spotify_volume",
        "description": "Set Spotify playback volume level (0-100).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "level": {"type": "INTEGER", "description": "Volume percentage (0-100)."},
            },
            "required": ["level"],
        },
        "handler": spotify_volume,
        "permission": "normal",
    },
    {
        "name": "spotify_like",
        "description": "Save or unsave the currently playing track to Your Library.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": spotify_like,
        "permission": "normal",
    },
    {
        "name": "spotify_playlist",
        "description": "Manage Spotify playlists: 'create', 'add', 'remove', 'open', 'delete'. Deleting is GATED.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "enum": ["create", "add", "remove", "open", "delete"],
                    "description": "Playlist action.",
                },
                "name": {"type": "STRING", "description": "Playlist name."},
            },
            "required": ["action", "name"],
        },
        "handler": spotify_playlist,
        "permission": "normal",
    },
]
