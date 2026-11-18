"""
actions/spotify_control.py — Deterministic Spotify Music Search and Playback Controller for JARVIS.

Full Lifecycle & Strict Verification:
1. Distinguishes 'play' (find, match title & artist, navigate, initiate, and verify specific track)
   from 'resume' (continue currently paused track).
2. Identifies ONLY genuine Spotify process windows (never Chrome/Edge/VS Code) and properly
   classifies idle window titles ('Spotify', 'Spotify Free', 'Spotify Premium') as NOT playing.
3. Distinguishes successful navigation from verified active playback.
4. Never claims playback succeeded without verifying the active track matches the request.
5. Returns clear, actionable error messages when selection or playback cannot be verified.
"""
from __future__ import annotations

import platform
import re
import subprocess
import time
import urllib.parse
from typing import Optional

from core import outcome
from core.lazyimport import LazyModule, installed

_PYAUTOGUI = installed("pyautogui")
pyautogui = LazyModule("pyautogui")

_IS_WINDOWS = platform.system() == "Windows"
_WIN_HIDE = {"creationflags": subprocess.CREATE_NO_WINDOW} if _IS_WINDOWS else {}

# Window titles Spotify shows when NO track is actively playing.
_IDLE_TITLES = frozenset({
    "",
    "spotify",
    "spotify free",
    "spotify premium",
    "spotify desktop",
    "spotify - web player",
    "spotify web player",
    "advertisement",
})

_STOP_WORDS = frozenset({
    "the", "a", "an", "of", "by", "feat", "ft", "song", "track",
    "official", "video", "audio", "lyrics", "remastered", "version",
    "single", "edit", "mix", "radio", "spotify",
})


def is_spotify_idle_title(title: str) -> bool:
    """Return True if the Spotify window title represents an idle/paused state (not a playing track)."""
    clean = re.sub(r"\s+", " ", str(title or "")).strip().lower()
    if clean in _IDLE_TITLES:
        return True
    if clean.startswith("spotify free") or clean.startswith("spotify premium"):
        return True
    return False


def _get_spotify_pids() -> set[int]:
    """Return PIDs belonging strictly to the Spotify desktop process."""
    import psutil
    pids: set[int] = set()
    for p in psutil.process_iter(["pid", "name"]):
        try:
            name = (p.info.get("name") or "").lower()
            if name in ("spotify.exe", "spotify"):
                pids.add(int(p.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
            continue
    return pids


def is_spotify_running() -> bool:
    """Check if Spotify application process is currently running."""
    return bool(_get_spotify_pids())


def _find_spotify_hwnd() -> int:
    """Find the visible main window handle (HWND) belonging strictly to spotify.exe."""
    if not _IS_WINDOWS:
        return 0
    try:
        import win32gui
        import win32process

        pids = _get_spotify_pids()
        if not pids:
            return 0

        matches: list[tuple[int, str]] = []

        def _enum_win(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return
            if pid not in pids:
                return
            title = win32gui.GetWindowText(hwnd).strip()
            if title and title not in ("Angle", "Default IME", "MSCTFIME UI", "GDI+ Window"):
                matches.append((hwnd, title))

        win32gui.EnumWindows(_enum_win, None)
        # Prefer a window showing an active track title ("Artist - Song")
        for hwnd, title in matches:
            if not is_spotify_idle_title(title):
                return hwnd
        return matches[0][0] if matches else 0
    except Exception:
        return 0


def get_spotify_window_title() -> str:
    """Return Spotify's main window title (strictly from spotify.exe, never other Electron/Chrome apps)."""
    if not _IS_WINDOWS:
        return ""
    try:
        import win32gui
        hwnd = _find_spotify_hwnd()
        if hwnd:
            return win32gui.GetWindowText(hwnd).strip()
        return ""
    except Exception:
        return ""


def get_spotify_active_track() -> str:
    """Return the currently playing track title (e.g. 'Michael Jackson - Beat It'), or '' if idle/paused."""
    title = get_spotify_window_title()
    if is_spotify_idle_title(title):
        return ""
    return title


def _normalize_tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", str(text or "").lower()))
    return {t for t in tokens if len(t) >= 2 and t not in _STOP_WORDS}


def verify_track_match(
    active_title: str,
    requested_track: str,
    matched_title: str = "",
    artist: str = "",
) -> bool:
    """
    Strictly verify that `active_title` is a real playing track (never 'Spotify Free')
    and matches the requested track/candidate title (and artist when present).
    """
    if not active_title or is_spotify_idle_title(active_title):
        return False

    low_active = re.sub(r"\s+", " ", active_title).strip().lower()
    active_tokens = _normalize_tokens(active_title)

    # Check direct substring match against requested track or resolved candidate title
    for ref in (requested_track, matched_title):
        clean_ref = re.sub(r"\s+", " ", str(ref or "")).strip().lower()
        if clean_ref and len(clean_ref) >= 2 and clean_ref in low_active:
            return True

    # Token overlap match for formatting differences (e.g. punctuation, remaster tags)
    for ref in (requested_track, matched_title):
        ref_tokens = _normalize_tokens(ref)
        if not ref_tokens:
            continue
        overlap = len(ref_tokens & active_tokens)
        if overlap == len(ref_tokens) or (len(ref_tokens) >= 2 and overlap / len(ref_tokens) >= 0.6):
            return True

    # If artist was explicitly requested and appears alongside at least one track word
    if artist:
        artist_tokens = _normalize_tokens(artist)
        req_tokens = _normalize_tokens(requested_track)
        if artist_tokens and (artist_tokens & active_tokens) and (req_tokens & active_tokens):
            return True

    return False


def _focus_spotify_window() -> bool:
    """Bring the Spotify desktop window to foreground."""
    if _IS_WINDOWS:
        try:
            import win32con
            import win32gui
            hwnd = _find_spotify_hwnd()
            if hwnd:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
                return True
        except Exception:
            pass
    try:
        from actions import windows_uia
        win = windows_uia.find_window_by_name("Spotify")
        if win:
            win.set_focus()
            return True
    except Exception:
        pass
    return False


def launch_spotify() -> bool:
    """Launch or focus Spotify application."""
    if is_spotify_running():
        if _focus_spotify_window():
            return True

    try:
        if _IS_WINDOWS:
            subprocess.run(["cmd.exe", "/c", "start", "", "spotify:"], **_WIN_HIDE, timeout=5)
            time.sleep(1.5)
            return True
        else:
            subprocess.Popen(["spotify"], **_WIN_HIDE)
            time.sleep(1.5)
            return True
    except Exception as e:
        print(f"[Spotify] Launch error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TRACK SEARCH & SELECTION (No API Key Required)
# ══════════════════════════════════════════════════════════════════════════════

def search_spotify_track(query: str, artist: str = "") -> Optional[dict]:
    """
    Search Spotify for the requested track using public web discovery and match title & artist.
    Returns:
      {"title": "Beat It", "artist": "Michael Jackson", "track_id": "...", "uri": "spotify:track:..."}
    """
    search_term = f"site:open.spotify.com/track {query}"
    if artist and artist.lower() not in query.lower():
        search_term += f" {artist}"

    try:
        from actions.web_search import _ddg_search
        results = _ddg_search(search_term, max_results=6)
    except Exception as e:
        print(f"[Spotify] DDG track search error: {e}")
        results = []

    candidates = []
    for r in results:
        url = r.get("url", "")
        title_raw = r.get("title", "")
        m = re.search(r"track/([a-zA-Z0-9]{22})", url)
        if m:
            track_id = m.group(1)
            # Clean title (e.g. "Beat It - song and lyrics by Michael Jackson | Spotify")
            t_clean = re.split(r"\s*[-|]\s*", title_raw)[0].replace("song and lyrics by", "").strip()
            candidates.append({
                "title": t_clean or query,
                "track_id": track_id,
                "url": url,
                "uri": f"spotify:track:{track_id}",
                "snippet": r.get("snippet", ""),
                "raw_title": title_raw,
            })

    if not candidates:
        return None

    q_lower = query.lower()
    art_lower = artist.lower()
    best_candidate = candidates[0]
    best_score = -1
    for c in candidates:
        corpus = (c["title"] + " " + c.get("raw_title", "") + " " + c["snippet"]).lower()
        score = 0
        if q_lower in c["title"].lower():
            score += 4
        elif q_lower in corpus:
            score += 2
        if art_lower:
            if art_lower in corpus:
                score += 3
            else:
                score -= 1
        if score > best_score:
            best_score = score
            best_candidate = c

    return best_candidate


def _try_click_uia_play_button() -> bool:
    """Attempt to click the primary 'Play' button inside Spotify via Windows UI Automation."""
    if not _IS_WINDOWS:
        return False
    try:
        from actions import windows_uia
        win = windows_uia.find_window_by_name("Spotify")
        if not win:
            return False
        for btn in win.descendants(control_type="Button"):
            try:
                name = (btn.window_text() or "").strip()
                if name.lower() == "play" or name.lower().startswith("play "):
                    btn.invoke()
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _poll_for_verified_playback(
    requested_track: str,
    matched_title: str,
    artist: str,
    timeout_s: float = 2.0,
) -> tuple[bool, str]:
    """Poll Spotify's active window title until verified playback starts or timeout expires."""
    deadline = time.monotonic() + timeout_s
    last_active = ""
    while time.monotonic() <= deadline:
        active = get_spotify_active_track()
        if active:
            last_active = active
            if verify_track_match(active, requested_track, matched_title, artist):
                return True, active
        time.sleep(0.25)
    return False, last_active


def play_track(track_name: str, artist: str = "") -> dict:
    """
    Explicitly find, select, initiate, and VERIFY playback of a specific track.
    Distinguishes successful navigation from verified playback and NEVER claims
    playback succeeded unless confirmed.
    """
    track_query = track_name.strip()
    if not track_query:
        return {
            "success": False,
            "verified": False,
            "navigated": False,
            "status": "failed",
            "action": "play_track",
            "reason": "No track name specified to play.",
        }

    # Record active playing track before navigation (idle titles like 'Spotify Free' -> '')
    active_before = get_spotify_active_track()

    # 1. Search and resolve direct track URI
    candidate = search_spotify_track(track_query, artist=artist)
    target_uri = candidate.get("uri") if candidate else None
    matched_title = candidate.get("title") if candidate else track_query

    # 2. Launch / Focus Spotify
    if not launch_spotify():
        return {
            "success": False,
            "verified": False,
            "navigated": False,
            "status": "failed",
            "action": "play_track",
            "requested_track": track_query,
            "reason": "Could not launch Spotify desktop application.",
        }
    time.sleep(0.4)

    # 3. Navigate directly to track URI (or in-app search URI fallback)
    navigated = False
    try:
        if target_uri and _IS_WINDOWS:
            print(f"[Spotify] Navigating to target URI: {target_uri}")
            subprocess.run(["cmd.exe", "/c", "start", "", target_uri], **_WIN_HIDE, timeout=5)
            navigated = True
        else:
            encoded = urllib.parse.quote(f"{track_query} {artist}".strip())
            uri = f"spotify:search:{encoded}"
            if _IS_WINDOWS:
                subprocess.run(["cmd.exe", "/c", "start", "", uri], **_WIN_HIDE, timeout=5)
            else:
                subprocess.Popen(["xdg-open", uri])
            navigated = True
    except Exception as e:
        return {
            "success": False,
            "verified": False,
            "navigated": False,
            "status": "failed",
            "action": "play_track",
            "requested_track": track_query,
            "reason": f"Failed to navigate Spotify to '{track_query}': {e}",
        }

    # 4. Wait briefly to see if URI activation already started playback
    is_verified, active_after = _poll_for_verified_playback(
        track_query, matched_title, artist, timeout_s=1.8
    )

    # 5. If not yet verified playing, explicitly trigger playback via UIA Play button / keyboard
    if not is_verified:
        _focus_spotify_window()
        time.sleep(0.3)
        clicked_play = _try_click_uia_play_button()
        if not clicked_play:
            _send_media_key("enter")

        is_verified, active_after = _poll_for_verified_playback(
            track_query, matched_title, artist, timeout_s=1.8
        )

    # 6. Secondary trigger attempt if Spotify stayed idle after URI navigation
    if not is_verified and not active_after:
        _focus_spotify_window()
        time.sleep(0.2)
        _send_media_key("space")
        is_verified, active_after = _poll_for_verified_playback(
            track_query, matched_title, artist, timeout_s=1.5
        )

    # 7. Strict verification outcome — never claim success when unverified!
    if is_verified:
        return {
            "success": True,
            "verified": True,
            "navigated": navigated,
            "status": "done",
            "action": "play_track",
            "track": matched_title or track_query,
            "artist": artist,
            "current_track": active_after,
            "uri": target_uri or "",
            "message": f"Now playing '{active_after}' on Spotify.",
        }

    # Navigation happened, but playback did NOT verify as the requested track
    raw_window_title = get_spotify_window_title()
    if active_after:
        if active_after == active_before:
            reason = (
                f"Navigated to '{matched_title or track_query}', but Spotify is still playing "
                f"'{active_after}' instead of the requested track."
            )
        else:
            reason = (
                f"Navigated to '{matched_title or track_query}', but Spotify is playing "
                f"'{active_after}', which could not be verified as the requested track."
            )
    else:
        idle_hint = (
            " (Spotify Free accounts may restrict direct track selection)"
            if "free" in raw_window_title.lower()
            else ""
        )
        reason = (
            f"Navigated to '{matched_title or track_query}' in Spotify, but playback did not start "
            f"(Spotify remains idle/paused{idle_hint})."
        )

    return {
        "success": False,
        "verified": False,
        "navigated": navigated,
        "status": "failed",
        "action": "play_track",
        "track": matched_title or track_query,
        "artist": artist,
        "current_track": active_after,
        "requested_track": track_query,
        "uri": target_uri or "",
        "reason": reason,
    }


def _send_media_key(key_name: str) -> None:
    """Send a media or keyboard key safely without failing on PyAutoGUI corner fail-safe."""
    if _IS_WINDOWS:
        vk_map = {
            "playpause": 0xB3,
            "nexttrack": 0xB0,
            "prevtrack": 0xB1,
            "enter": 0x0D,
            "space": 0x20,
        }
        vk = vk_map.get(key_name.lower())
        if vk is not None:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                user32.keybd_event(vk, 0, 0, 0)
                time.sleep(0.03)
                user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP
                return
            except Exception:
                pass
    try:
        pyautogui.press(key_name)
    except Exception as e:
        print(f"[Spotify] Key '{key_name}' trigger error: {e}")


def resume_playback() -> dict:
    """Resume currently paused track (distinct from playing a new song)."""
    launch_spotify()
    _send_media_key("playpause")
    time.sleep(0.8)
    active = get_spotify_active_track()
    return {
        "success": True,
        "verified": bool(active),
        "status": "done",
        "action": "resume_playback",
        "current_track": active or "current track",
        "message": f"Resumed Spotify playback ({active})." if active else "Sent resume command to Spotify.",
    }


def pause_playback() -> dict:
    """Pause playback."""
    _send_media_key("playpause")
    time.sleep(0.5)
    active = get_spotify_active_track()
    return {
        "success": True,
        "verified": not bool(active),
        "status": "done",
        "action": "pause_playback",
        "current_track": active,
        "message": "Spotify playback paused.",
    }


def next_track() -> dict:
    """Skip to next track."""
    _send_media_key("nexttrack")
    time.sleep(1.0)
    active = get_spotify_active_track()
    return {
        "success": True,
        "verified": bool(active),
        "status": "done",
        "action": "next_track",
        "current_track": active or "next track",
        "message": f"Skipped to next track: {active}." if active else "Skipped to next track on Spotify.",
    }


def previous_track() -> dict:
    """Go back to previous track."""
    _send_media_key("prevtrack")
    time.sleep(1.0)
    active = get_spotify_active_track()
    return {
        "success": True,
        "verified": bool(active),
        "status": "done",
        "action": "previous_track",
        "current_track": active or "previous track",
        "message": f"Returned to previous track: {active}." if active else "Returned to previous track on Spotify.",
    }


def get_playback_state() -> dict:
    """Get currently playing track title and playback state."""
    active = get_spotify_active_track()
    is_playing = bool(active)
    return {
        "success": True,
        "verified": True,
        "status": "done",
        "action": "get_playback_state",
        "is_playing": is_playing,
        "current_track": active if is_playing else "None / Paused",
        "message": f"Currently playing: '{active}'" if is_playing else "Spotify is paused or idle.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED TOOL HANDLER
# ══════════════════════════════════════════════════════════════════════════════

def spotify_control(parameters: dict = None, player=None, **kwargs) -> str:
    """
    Unified auto-discovered tool handler for Spotify.
    Never claims playback succeeded unless strictly verified.
    """
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    query = (params.get("query") or params.get("song") or params.get("track") or params.get("music") or "").strip()
    artist = (params.get("artist") or "").strip()

    if not action:
        action = "play" if query else "open"

    if action in ("open", "launch"):
        ok = launch_spotify()
        if ok:
            return outcome.done("spotify_control", action="open", message="Spotify opened.")
        return outcome.failed("spotify_control", "Could not open Spotify.")

    if action in ("search", "find", "search_track"):
        if not query:
            return outcome.failed("spotify_control", "No track name specified to search.")
        cand = search_spotify_track(query, artist=artist)
        if cand:
            return outcome.done("spotify_control", action="search_track", track=cand["title"], artist=artist, uri=cand.get("uri", ""), verified=True)
        return outcome.failed("spotify_control", f"No Spotify tracks found matching '{query}'.")

    if action in ("play", "play_track", "search_and_play"):
        if not query:
            return outcome.failed("spotify_control", "No track name specified to play.")
        res = play_track(query, artist=artist)
        if not isinstance(res, dict):
            return outcome.failed("spotify_control", str(res))
        if res.get("status") == "failed" or not res.get("success", False):
            extra = {k: v for k, v in res.items() if k not in ("status", "reason", "message")}
            return outcome.failed(
                "spotify_control",
                res.get("reason", "Track playback could not be verified."),
                **extra,
            )
        if not res.get("verified", False):
            extra = {k: v for k, v in res.items() if k not in ("status", "reason", "message")}
            return outcome.unverified(
                "spotify_control",
                res.get("reason", "Could not verify that the requested song started playing."),
                **extra,
            )
        return outcome.done("spotify_control", **res)

    if action in ("resume", "continue"):
        res = resume_playback()
        return outcome.done("spotify_control", **res)

    if action in ("pause", "stop"):
        res = pause_playback()
        return outcome.done("spotify_control", **res)

    if action in ("next", "next_track", "skip"):
        res = next_track()
        return outcome.done("spotify_control", **res)

    if action in ("previous", "prev", "previous_track", "back"):
        res = previous_track()
        return outcome.done("spotify_control", **res)

    if action in ("get_state", "get_status", "status", "current"):
        res = get_playback_state()
        return outcome.done("spotify_control", **res)

    if action == "volume_up":
        for _ in range(3):
            pyautogui.hotkey("ctrl", "up")
        return outcome.done("spotify_control", action="volume_up", message="Spotify volume increased.")

    if action == "volume_down":
        for _ in range(3):
            pyautogui.hotkey("ctrl", "down")
        return outcome.done("spotify_control", action="volume_down", message="Spotify volume decreased.")

    return outcome.failed("spotify_control", f"Unknown spotify action: '{action}'")


TOOL = {
    "name": "spotify_control",
    "description": (
        "Controls Spotify desktop music player with verified track selection: "
        "- play: search for and explicitly start a specific track (e.g. 'Beat It', 'One Name', query='Beat It', artist='Michael Jackson'). Replaces any current song and verifies playback. "
        "- resume: continue paused playback of the current track without searching. "
        "- pause: pause playback. "
        "- next: skip to next track. "
        "- previous: return to previous track. "
        "- get_state: query currently playing track and artist. "
        "Always call this tool for music playback requests."
    ),
    "brief": "Search, select, play specific tracks, resume, pause, and verify Spotify playback.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "play | resume | pause | next | previous | get_state | open | volume_up | volume_down",
            },
            "query": {
                "type": "STRING",
                "description": "Song name or track title to find and play (e.g. 'Beat It', 'One Name').",
            },
            "artist": {
                "type": "STRING",
                "description": "Artist or band name (e.g. 'Michael Jackson', 'Anirudh').",
            },
        },
        "required": ["action"],
    },
    "handler": spotify_control,
}
