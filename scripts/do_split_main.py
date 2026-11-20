"""
scripts/do_split_main.py — Script to extract main.py into core/ modules and a thin main.py.
"""
from __future__ import annotations

import re
from pathlib import Path

def main():
    with open("main.py", "r", encoding="utf-8") as f:
        src = f.read()

    lines = src.splitlines(keepends=True)
    if len(lines) < 1000:
        print("main.py already trimmed, fetching from git...")
        import subprocess
        res = subprocess.run(["git", "show", "HEAD:main.py"], capture_output=True, text=True, encoding="utf-8")
        lines = res.stdout.splitlines(keepends=True)

    print(f"Total lines in original main.py: {len(lines)}")

    # 1. core/session_voice.py
    # Lines from def _on_audio_status_changed (line 791) to def _build_config (line 1654)
    v_start = None
    v_end = None
    for i, l in enumerate(lines):
        if l.startswith("    def _on_audio_status_changed"):
            v_start = i
        elif l.startswith("    def _build_config"):
            v_end = i
            break

    voice_body = lines[v_start:v_end]
    voice_header = '''"""
core/session_voice.py — Voice state, wake word, PTT, mute, and reconnection mixin.
"""
from __future__ import annotations
import asyncio
import threading
import time
from typing import Any
from core.runtime_env import _ReconnectSignal, _is_reconnect_signal, _keep_context_of
from core.wake_word import WakeWordDetector, is_ready as wake_is_ready, install_and_download as wake_install
from core.voice_state import VoiceState
from core import audio_devices
from memory.config_manager import (
    get_input_device, get_output_device, save_wake_word_enabled, save_auto_sleep_enabled,
    get_push_to_talk_enabled,
)

IDLE_SLEEP_TIMEOUT = 180.0
WAKE_SLEEP_TIMEOUT = 180.0
_STALL_LIMIT = 45.0

class LiveVoiceMixin:
    """Voice state management, wake word, and control handling."""
'''
    voice_content = [voice_header] + voice_body

    with open("core/session_voice.py", "w", encoding="utf-8") as f:
        f.writelines(voice_content)
    print(f"Created core/session_voice.py ({len(voice_content)} lines)")

    # 2. core/live_session.py
    # Composes LiveAudioMixin, LiveMonitorsMixin, LiveVoiceMixin
    # Includes: __init__, _build_config, _tuning_config, _engine_mode, _resolve_engine_caps, _open_session, run, shutdown_listeners
    # Lines before v_start: class JarvisLive to v_start
    # Lines from v_end (_build_config) to _execute_tool (line 1848)
    # Lines from _open_session (line 2881) to def main() (line 3261)
    
    jl_start = None
    exec_tool_start = None
    exec_tool_end = None
    open_sess_start = None
    main_start = None

    for i, l in enumerate(lines):
        if l.startswith("class JarvisLive:"):
            jl_start = i
        elif l.startswith("    async def _execute_tool("):
            exec_tool_start = i
        elif l.startswith("    async def _send_realtime("):
            exec_tool_end = i
        elif l.startswith("    def _engine_mode("):
            if open_sess_start is None:
                open_sess_start = i
        elif l.startswith("def main():"):
            main_start = i

    init_body = lines[jl_start:v_start]
    # Replace direct actions import inside __init__ with platform_hooks call
    init_cleaned = []
    for l in init_body:
        if "from actions import screen_processor" in l:
            init_cleaned.append("            from core import platform_hooks\n")
            init_cleaned.append("            platform_hooks.call_hook('set_screen_glow_callback', lambda dur=2500: self.ui.show_screen_glow(dur))\n")
        elif "screen_processor.set_screen_glow_callback" in l:
            continue
        elif "self._sys_monitor      = SystemMonitor()" in l:
            init_cleaned.append("        from core import platform_hooks\n")
            init_cleaned.append("        _sm_cls = platform_hooks.get_hook('SystemMonitor')\n")
            init_cleaned.append("        self._sys_monitor      = _sm_cls() if _sm_cls else None\n")
        elif "self._proactive        = ProactiveEngine()" in l:
            init_cleaned.append("        _pro_cls = platform_hooks.get_hook('ProactiveEngine')\n")
            init_cleaned.append("        self._proactive        = _pro_cls() if _pro_cls else None\n")
        elif "class JarvisLive:" in l:
            init_cleaned.append("class JarvisLive(LiveAudioMixin, LiveMonitorsMixin, LiveVoiceMixin):\n")
        else:
            init_cleaned.append(l)

    # Insert delegation for _execute_tool and _execute_tool_inner in JarvisLive
    tool_delegates = [
        "    async def _execute_tool(self, fc) -> types.FunctionResponse:\n",
        "        return await self._tool_dispatcher.execute_tool(self, fc)\n",
        "\n",
        "    async def _execute_tool_inner(self, fc) -> types.FunctionResponse:\n",
        "        return await self._tool_dispatcher.execute_tool_inner(self, fc)\n",
        "\n",
    ]

    config_body = lines[v_end:exec_tool_start]
    run_body = lines[open_sess_start:main_start]

    live_session_header = '''"""
core/live_session.py — Main JarvisLive coordinator and session lifecycle management.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import sounddevice as sd
from google import genai
from google.genai import types

from core.runtime_env import (
    BASE_DIR, API_CONFIG_PATH, PROMPT_PATH, LIVE_MODEL, CHANNELS,
    SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE, CHUNK_SIZE,
    _pcm_level, _pcm_visemes, _is_repeat_chunk, _clean_transcript,
    _ReconnectSignal, _is_reconnect_signal, _keep_context_of,
)
from core.prompt_builder import (
    TOOL_DECLARATIONS, _describe_tools, _describe_limits, _render_prompt,
    _load_system_prompt, _get_api_key,
)
from core.tool_dispatcher import ToolDispatcher
from core.session_audio import LiveAudioMixin
from core.session_monitors import LiveMonitorsMixin
from core.session_voice import LiveVoiceMixin, IDLE_SLEEP_TIMEOUT, WAKE_SLEEP_TIMEOUT
from core.action_loader import discover_actions
from core.plugin_loader import discover_plugins
from core.voice_state import VoiceStateManager, VoiceState
from core.global_input import GlobalInputController
from core.audio_manager import AudioManager, set_audio_manager
from core.viseme import VisemeStream
from core.echo import EchoGuard
from core.wake_word import WakeWordDetector, is_ready as wake_is_ready
from core import confirm as confirm_gate
from core import platform_hooks
from memory.memory_manager import load_memory, format_memory_for_prompt, set_trim_notifier
from memory.config_manager import (
    get_brief_enabled, get_media_resolution, get_proactive_audio_enabled,
    get_push_to_talk_enabled, get_thinking_enabled, get_turn_tuning, get_voice,
    get_wake_word_enabled, save_wake_word_enabled,
    get_auto_sleep_enabled, save_auto_sleep_enabled,
    get_wake_words, get_wake_word_threshold, get_idle_sleep_timeout,
    get_input_device, get_output_device,
    get_listening_shortcut, get_force_listening_shortcut,
    get_emergency_stop_shortcut, get_mouse_gesture_enabled,
    sanitize_hotkeys_and_wake_words,
)

'''
    live_session_content = [live_session_header] + init_cleaned + [
        "        # Initialize tool dispatcher\n",
        "        self._tool_dispatcher = ToolDispatcher(self)\n",
    ] + tool_delegates + config_body + run_body

    with open("core/live_session.py", "w", encoding="utf-8") as f:
        f.writelines(live_session_content)
    print(f"Created core/live_session.py ({len(live_session_content)} lines)")

    # 3. New main.py (thin composition module)
    main_code = '''"""
main.py — Main entrypoint and composition layer for Jarvis Desktop Agent.
"""
from __future__ import annotations

import asyncio
import sys
import threading

# ── 1. Configure runtime environment ─────────────────────────────────────────
from core.runtime_env import ensure_windows_subprocess_patch, ensure_utf8_console
ensure_windows_subprocess_patch()
ensure_utf8_console()

# ── 2. Register platform hooks for modular actions ───────────────────────────
from core import platform_hooks
from actions.screen_processor import _capture_screen, set_screen_glow_callback
from actions.system_monitor import SystemMonitor, get_system_status
from actions.proactive import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all
)
from actions.web_search import _news as _fetch_news_sync

platform_hooks.register_hook("capture_screen", _capture_screen)
platform_hooks.register_hook("set_screen_glow_callback", set_screen_glow_callback)
platform_hooks.register_hook("get_system_status", get_system_status)
platform_hooks.register_hook("SystemMonitor", SystemMonitor)
platform_hooks.register_hook("ProactiveEngine", ProactiveEngine)
platform_hooks.register_hook("add_monitor", add_monitor)
platform_hooks.register_hook("remove_monitor", remove_monitor)
platform_hooks.register_hook("list_monitors", list_monitors)
platform_hooks.register_hook("monitor_check_all", monitor_check_all)
platform_hooks.register_hook("fetch_news_sync", _fetch_news_sync)

# ── 3. Re-export public symbols expected by tests ────────────────────────────
from core.live_session import JarvisLive, IDLE_SLEEP_TIMEOUT, WAKE_SLEEP_TIMEOUT
from core.prompt_builder import TOOL_DECLARATIONS
from memory.config_manager import get_input_device, get_output_device
from ui import JarvisUI

__all__ = [
    "JarvisLive",
    "IDLE_SLEEP_TIMEOUT",
    "WAKE_SLEEP_TIMEOUT",
    "TOOL_DECLARATIONS",
    "get_input_device",
    "get_output_device",
    "main",
]


def main():
    """Main application entry point: creates UI, starts voice engine, runs Qt event loop."""
    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            print("\\n🔴 Shutting down...")
        except Exception as e:
            if not getattr(ui, "_is_closed", False):
                print(f"\\n[JARVIS] Session stopped: {e}")
        finally:
            jarvis.shutdown_listeners()

    threading.Thread(target=runner, daemon=True, name="jarvis-voice-worker").start()
    try:
        ui.root.mainloop()
    finally:
        ui._is_closed = True


if __name__ == "__main__":
    main()
'''
    with open("main.py", "w", encoding="utf-8") as f:
        f.write(main_code)
    print("Rewrote main.py as thin composition module")

if __name__ == "__main__":
    main()
