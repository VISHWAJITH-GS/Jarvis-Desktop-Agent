"""
actions/jarvis_control.py — Self-control and Jarvis display window management.

Capabilities:
  1. mute / unmute: explicit variants for speaker (stop speech) and microphone (stop listening).
  2. sleep / wake: enter standby (wake word only) and wake up.
  3. minimize / restore: minimize HUD to taskbar/tray and restore back to normal.
  4. shutdown_jarvis: orderly shutdown of Jarvis only (stop tasks, flush logs/memory, close session).
  5. display controls: list_displays, minimize_display, restore_display, close_display, close_all_displays
     via gui.display_registry. Never touches external application windows.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from core import outcome
from gui import display_registry
from memory import config_manager

logger = logging.getLogger(__name__)


def jarvis_control(parameters: Optional[Dict[str, Any]] = None, player=None, session_memory=None, **kwargs) -> str:
    """Execute a self-control or Jarvis display lifecycle action."""
    params = dict(parameters or {})
    params.update(kwargs)

    raw_action = str(params.get("action") or "").strip().lower()
    raw_target = str(params.get("target") or params.get("name") or params.get("display") or "").strip().lower()

    # Normalize action aliases
    action = raw_action
    if action in ("mute_speaker", "mute_speech", "mute_audio"):
        action = "mute"
        raw_target = "speaker"
    elif action in ("mute_mic", "mute_microphone", "stop_listening"):
        action = "mute"
        raw_target = "microphone"
    elif action in ("unmute_speaker", "unmute_speech", "unmute_audio"):
        action = "unmute"
        raw_target = "speaker"
    elif action in ("unmute_mic", "unmute_microphone", "start_listening"):
        action = "unmute"
        raw_target = "microphone"
    elif action in ("minimize_jarvis", "minimize_hud"):
        action = "minimize"
    elif action in ("restore_jarvis", "restore_hud", "back_to_normal"):
        action = "restore"
    elif action in ("close_jarvis", "exit_jarvis", "quit_jarvis"):
        action = "shutdown_jarvis"

    # ── 1. Mute ───────────────────────────────────────────────────────────────
    if action == "mute":
        target = raw_target or "speaker"
        if target in ("mic", "microphone", "listening", "input"):
            if player and hasattr(player, "mute_mic"):
                player.mute_mic()
            elif player and hasattr(player, "mic_muted"):
                player.mic_muted = True
            if player and hasattr(player, "write_log"):
                player.write_log("SYS: Microphone muted. Wake word remains active.")
            return "Microphone is now muted. Wake word is still active, sir."

        elif target in ("speaker", "voice", "audio", "sound", "output", "speech"):
            if player and hasattr(player, "mute_speaker"):
                player.mute_speaker()
            elif player and hasattr(player, "speaker_muted"):
                player.speaker_muted = True
            elif player and hasattr(player, "set_muted"):
                player.set_muted(True)
            if player and hasattr(player, "write_log"):
                player.write_log("SYS: Speaker muted. Voice responses silenced.")
            return "Speaker is now muted. Voice responses are silenced, sir."

        elif target in ("both", "all", "full", "fully"):
            if player and hasattr(player, "mute_speaker"):
                player.mute_speaker()
            elif player and hasattr(player, "set_muted"):
                player.set_muted(True)
            if player and hasattr(player, "mute_mic"):
                player.mute_mic()
            elif player and hasattr(player, "mic_muted"):
                player.mic_muted = True
            if player and hasattr(player, "write_log"):
                player.write_log("SYS: Fully muted (speaker and microphone).")
            return "Jarvis is now fully muted, sir."

        else:
            if player and hasattr(player, "mute_speaker"):
                player.mute_speaker()
            elif player and hasattr(player, "set_muted"):
                player.set_muted(True)
            return "Speaker is now muted, sir."

    # ── 2. Unmute ─────────────────────────────────────────────────────────────
    if action == "unmute":
        target = raw_target or "speaker"
        if target in ("mic", "microphone", "listening", "input"):
            if player and hasattr(player, "unmute_mic"):
                player.unmute_mic()
            elif player and hasattr(player, "mic_muted"):
                player.mic_muted = False
            if player and hasattr(player, "write_log"):
                player.write_log("SYS: Microphone unmuted. Listening active.")
            return "Microphone is now active and listening, sir."

        elif target in ("speaker", "voice", "audio", "sound", "output", "speech"):
            if player and hasattr(player, "unmute_speaker"):
                player.unmute_speaker()
            elif player and hasattr(player, "speaker_muted"):
                player.speaker_muted = False
            elif player and hasattr(player, "set_muted"):
                player.set_muted(False)
            if player and hasattr(player, "write_log"):
                player.write_log("SYS: Speaker unmuted.")
            return "Speaker is now unmuted, sir."

        elif target in ("both", "all", "full", "fully"):
            if player and hasattr(player, "unmute_speaker"):
                player.unmute_speaker()
            elif player and hasattr(player, "set_muted"):
                player.set_muted(False)
            if player and hasattr(player, "unmute_mic"):
                player.unmute_mic()
            elif player and hasattr(player, "mic_muted"):
                player.mic_muted = False
            if player and hasattr(player, "write_log"):
                player.write_log("SYS: Fully unmuted (speaker and microphone).")
            return "Jarvis is now fully unmuted, sir."

        else:
            if player and hasattr(player, "unmute_speaker"):
                player.unmute_speaker()
            elif player and hasattr(player, "set_muted"):
                player.set_muted(False)
            return "Speaker is now unmuted, sir."

    # ── 3. Sleep / Wake ───────────────────────────────────────────────────────
    if action in ("sleep", "standby", "go_to_sleep"):
        if player and hasattr(player, "sleep_jarvis"):
            player.sleep_jarvis()
        if player and hasattr(player, "write_log"):
            player.write_log("SYS: Entering standby sleep mode.")
        return "Going to sleep. Say Jarvis or press Ctrl+Alt+K whenever you need me."

    if action in ("wake", "wake_up"):
        if player and hasattr(player, "wake_jarvis"):
            player.wake_jarvis()
        if player and hasattr(player, "write_log"):
            player.write_log("SYS: Waking up from standby.")
        return "I am awake and listening, sir."

    # ── 4. Minimize / Restore ─────────────────────────────────────────────────
    if action in ("minimize", "minimize_window", "minimize_hud", "dock"):
        if player and hasattr(player, "minimize_jarvis"):
            player.minimize_jarvis()
        elif player and hasattr(player, "_win") and hasattr(player._win, "_jarvis_window_sig"):
            player._win._jarvis_window_sig.emit("minimize")
        return "Minimized Jarvis window, sir."

    if action in ("restore", "restore_window", "restore_hud", "back_to_normal", "maximize"):
        if player and hasattr(player, "restore_jarvis"):
            player.restore_jarvis()
        elif player and hasattr(player, "_win") and hasattr(player._win, "_jarvis_window_sig"):
            player._win._jarvis_window_sig.emit("restore")
        return "Restored Jarvis window back to normal, sir."

    # ── 5. Shutdown Jarvis (Orderly assistant shutdown) ──────────────────────
    if action in ("shutdown_jarvis", "shutdown", "close_yourself"):
        # Confirmation guard:
        is_authorized = bool(params.get("_execution_authorized") or params.get("confirmed"))
        if not is_authorized:
            from core import confirm
            return confirm.request_action(
                action="jarvis_control",
                parameters={"action": "shutdown_jarvis"},
                risk_level="confirmation",
                title="Shut Down JARVIS",
                detail="Orderly shutdown of Jarvis voice assistant only (flush logs, save memory, exit).",
                run=lambda: jarvis_control({"action": "shutdown_jarvis", "_execution_authorized": True}, player=player),
            )

        if player and hasattr(player, "write_log"):
            player.write_log("SYS: Orderly shutdown confirmed — closing JARVIS.")

        # Orderly shutdown sequence
        import threading
        def _do_orderly_shutdown():
            time.sleep(1.0)
            if player and hasattr(player, "close"):
                try:
                    player.close()
                except Exception:
                    pass
            import os
            os._exit(0)

        threading.Thread(target=_do_orderly_shutdown, daemon=True).start()
        return "Shutting down JARVIS. Goodbye, sir."

    # ── 6. Display Window Lifecycle (Strictly Jarvis-created displays) ────────
    if action in ("list_displays", "list_display", "displays"):
        displays = display_registry.list_displays()
        if not displays:
            return "Sir, there are no active Jarvis display windows open right now."
        lines = []
        for d in displays:
            st = " (minimized)" if d.get("minimized") else ""
            unsaved = " [UNSAVED CONTENT]" if d.get("has_unsaved_content") else ""
            lines.append(f"• Display {d['id']}: '{d['title']}' ({d['kind']}){st}{unsaved}")
        return "Active Jarvis displays:\n" + "\n".join(lines)

    if action in ("minimize_display", "minimize_disp"):
        target = raw_target or (params.get("display_id") or params.get("id") or "")
        return display_registry.minimize_display(target, player=player)

    if action in ("restore_display", "restore_disp", "focus_display"):
        target = raw_target or (params.get("display_id") or params.get("id") or "")
        return display_registry.restore_display(target, player=player)

    if action in ("close_display", "close_disp"):
        target = raw_target or (params.get("display_id") or params.get("id") or "")
        force = bool(params.get("force") or params.get("confirmed") or params.get("_execution_authorized"))
        return display_registry.close_display(target, player=player, force=force)

    if action in ("close_all_displays", "close_all"):
        return display_registry.close_all_displays(player=player)

    # ── 7. Status ─────────────────────────────────────────────────────────────
    if action in ("status", "get_status"):
        spk_st = "muted" if (player and getattr(player, "speaker_muted", False)) else "active"
        mic_st = "muted" if (player and getattr(player, "mic_muted", False)) else "listening"
        disp_cnt = len(display_registry.list_displays())
        return f"Jarvis is operational. Speaker is {spk_st}, microphone is {mic_st}. Active displays: {disp_cnt}."

    # ── 8. STT Custom Vocabulary / Hotwords ───────────────────────────────────
    if action in ("add_hotword", "add_vocabulary", "add_stt_word"):
        word = str(params.get("word") or raw_target or "").strip()
        if not word:
            return "Please specify a word or phrase to add to the speech vocabulary, sir."
        added = config_manager.add_stt_hotword(word)
        if added:
            if player and hasattr(player, "write_log"):
                player.write_log(f"SYS: Added '{word}' to STT custom vocabulary.")
            return f"Added '{word}' to speech recognition vocabulary, sir."
        return f"'{word}' is already in your speech recognition vocabulary, sir."

    if action in ("remove_hotword", "remove_vocabulary", "delete_hotword"):
        word = str(params.get("word") or raw_target or "").strip()
        if not word:
            return "Please specify a word or phrase to remove, sir."
        removed = config_manager.remove_stt_hotword(word)
        if removed:
            if player and hasattr(player, "write_log"):
                player.write_log(f"SYS: Removed '{word}' from STT custom vocabulary.")
            return f"Removed '{word}' from speech recognition vocabulary, sir."
        return f"'{word}' was not found in your speech recognition vocabulary, sir."

    if action in ("list_hotwords", "list_vocabulary", "get_hotwords"):
        words = config_manager.get_stt_hotwords()
        if not words:
            return "No custom speech vocabulary words configured, sir."
        formatted = ", ".join(words)
        if player and hasattr(player, "write_log"):
            player.write_log(f"SYS: STT Vocabulary: {formatted}")
        return f"Current speech recognition vocabulary contains: {formatted}."

    return f"Unknown jarvis_control action: '{raw_action}'."


TOOL = {
    "name": "jarvis_control",
    "description": (
        "JARVIS Self-Control, Display Window Management, and Speech Vocabulary: Controls Jarvis's own "
        "system state (mute/unmute speaker or microphone, sleep, wake, minimize to taskbar/tray, "
        "restore back to normal, shutdown Jarvis orderly), controls Jarvis-created display windows, "
        "and manages custom speech recognition vocabulary (add_hotword, remove_hotword, list_hotwords). "
        "Strictly operates on Jarvis's own state and displays; never touches external applications."
    ),
    "brief": "Control Jarvis state, display windows, and speech recognition vocabulary.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": [
                    "mute",
                    "unmute",
                    "sleep",
                    "wake",
                    "minimize",
                    "restore",
                    "shutdown_jarvis",
                    "list_displays",
                    "minimize_display",
                    "restore_display",
                    "close_display",
                    "close_all_displays",
                    "status",
                    "add_hotword",
                    "remove_hotword",
                    "list_hotwords",
                ],
                "description": "The self-control, display, or vocabulary action to perform.",
            },
            "target": {
                "type": "STRING",
                "description": (
                    "Action target: for mute/unmute specify 'speaker', 'microphone', or 'both'; "
                    "for display commands specify display name or ID; "
                    "for add_hotword/remove_hotword specify the word or phrase."
                ),
            },
            "word": {
                "type": "STRING",
                "description": "The vocabulary word or phrase for add_hotword/remove_hotword.",
            },
        },
        "required": ["action"],
    },
    "handler": jarvis_control,
}
