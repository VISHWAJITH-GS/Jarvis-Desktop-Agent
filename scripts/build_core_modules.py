"""
scripts/build_core_modules.py — Extracts main.py into modular core/ modules.
"""
from __future__ import annotations

import os
import sys

def main():
    print("Extracting main.py into core/ modules...")

    # 1. core/runtime_env.py
    runtime_env_code = '''"""
core/runtime_env.py — Runtime environment configuration, subprocess patching, and PCM helpers.
"""
from __future__ import annotations

import os
import platform as _platform
import re
import subprocess as _subprocess
import sys
from pathlib import Path
import numpy as np

# ── Windows subprocess creation flag patch ────────────────────────────────────
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen


def ensure_windows_subprocess_patch():
    """Ensure CREATE_NO_WINDOW is patched on Windows."""
    if _platform.system() == "Windows":
        _subprocess.Popen = _Popen


def ensure_utf8_console():
    """Reconfigure console output streams to UTF-8 with replace fallback."""
    for _stream in ("stdout", "stderr"):
        try:
            _s = getattr(sys, _stream, None)
            if _s is not None and hasattr(_s, "reconfigure"):
                _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"

LIVE_MODEL          = "models/gemini-3.1-flash-live-preview"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000 
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

_LEVEL_FLOOR = 20.0
_LEVEL_FULL  = 2000.0


def _pcm_level(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level for the HUD waveform."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    ratio = min(1.0, max(0.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR)))
    return float(ratio ** 0.65)


def _pcm_visemes(samples, sr: int = 24000):
    """Slice a chunk of int16 PCM into ~20 ms analysis windows and return a list of
    viseme frames, each a 4-tuple for HoloAvatar.push_viseme:
        (energy: float, openness: float, form: float, tilt: float)
    """
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return []
        win_size = int(sr * 0.020)
        hop_size = int(sr * 0.010)
        if win_size <= 0 or x.size < win_size:
            return []

        peak = float(np.max(np.abs(x)))
        if peak < 80.0:
            return []

        w = np.hanning(win_size).astype(np.float32)
        freqs = np.fft.rfftfreq(win_size, d=1.0 / sr)
        b_low  = (freqs >= 200.0)  & (freqs < 900.0)
        b_mid  = (freqs >= 900.0)  & (freqs < 2600.0)
        b_high = (freqs >= 2600.0) & (freqs < 6000.0)

        frames = []
        for start in range(0, x.size - win_size + 1, hop_size):
            slice_ = x[start:start + win_size] * w
            slice_rms = float(np.sqrt(np.mean(slice_ * slice_)))
            if slice_rms <= _LEVEL_FLOOR:
                energy = 0.0
            else:
                r = min(1.0, max(0.0, (slice_rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR)))
                energy = float(r ** 0.65)

            mag = np.abs(np.fft.rfft(slice_))
            e_low  = float(np.sum(mag[b_low]))
            e_mid  = float(np.sum(mag[b_mid]))
            e_high = float(np.sum(mag[b_high]))
            denom  = e_low + e_mid + e_high + 1e-4

            openness = (0.55 * (e_low / denom) + 0.45 * (e_mid / denom)) * min(1.0, energy * 1.6)
            form     = min(1.0, max(0.0, (e_mid / denom) * 1.5))
            tilt     = float(np.clip((e_high - e_low) / denom, -1.0, 1.0))

            frames.append((
                float(energy),
                float(min(1.0, max(0.0, openness))),
                float(form),
                float(tilt),
            ))
        return frames
    except Exception:
        return []


def _is_repeat_chunk(txt: str, buf: list) -> bool:
    if not buf:
        return False
    prev = buf[-1]
    if txt == prev:
        return True
    if len(txt) > 20 and prev.endswith(txt):
        return True
    return False

_CTRL_RE = re.compile(r"[\\x00-\\x1f\\x7f-\\x9f]")

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\\x00-\\x08\\x0b-\\x1f]", "", text)
    return text.strip()


class _ReconnectSignal(Exception):
    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _is_reconnect_signal(exc: BaseException) -> bool:
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_reconnect_signal(sub) for sub in exc.exceptions)
    return False


def _keep_context_of(exc: BaseException) -> bool:
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if _is_reconnect_signal(sub):
                return _keep_context_of(sub)
    return True
'''
    with open("core/runtime_env.py", "w", encoding="utf-8") as f:
        f.write(runtime_env_code)
    print("Created core/runtime_env.py")

    # 2. core/prompt_builder.py
    prompt_builder_code = '''"""
core/prompt_builder.py — Tool declarations and system prompt formatting.
"""
from __future__ import annotations

import json
from pathlib import Path
from core.runtime_env import BASE_DIR, API_CONFIG_PATH, PROMPT_PATH

TOOL_DECLARATIONS = [
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the computer screen image and lets you describe or analyze it visually. "
            "MUST be called when the user explicitly asks what is on screen, analyze my screen, "
            "describe what is visible on screen, etc. "
            "NEVER call this tool to find things to click, open folders, or navigate UI — "
            "use windows_uia or computer_control by element name instead. "
            "After the screenshot is captured it is sent directly to you — describe what you see and answer the user's question. "
            "Webcam/camera capture is disabled and unavailable."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text":  {"type": "STRING", "description": "The question or instruction about the captured screen image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background news/topic monitors. "
            "When a topic is added, JARVIS checks the web for developments throughout the day "
            "and speaks alerts when new headlines appear. "
            "action: 'add', 'remove', or 'list'. topic: query string (e.g. 'SpaceX launch', 'Bitcoin')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "'add', 'remove', or 'list'"},
                "topic":  {"type": "STRING", "description": "Topic to monitor (required for add/remove)"}
            },
            "required": ["action"],
        }
    },
    {
        "name": "sleep_jarvis",
        "description": (
            "Puts JARVIS into a low-power background sleep mode. The microphone is closed and "
            "audio stops streaming to Gemini. JARVIS will only wake up when the user speaks "
            "the wake word ('Jarvis' or 'Hello Jarvis') or presses the keyboard shortcut (Ctrl+Alt+K). "
            "Use this when the user says 'go to sleep', 'sleep', 'take a rest', 'goodnight', etc."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely (requires high-impact voice + visual confirmation). "
            "Use when the user asks to exit, close, terminate, or power down JARVIS."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important fact, user preference, project detail, or piece of knowledge "
            "into persistent long-term memory so you remember it in future sessions. "
            "Categories: user_facts — name, location, family, work | "
            "preferences — communication style, tools, habits | "
            "projects — active projects, goals, things being built | "
            "environment — hardware, software versions, setup notes | "
            "decisions — choices made and the reasoning behind them."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": "One of: user_facts, preferences, projects, environment, decisions, notes"
                },
                "key":   {"type": "STRING", "description": "Short identifier, e.g. 'favorite_language'"},
                "value": {"type": "STRING", "description": "The fact to remember"}
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Search persistent long-term memory for facts, preferences, or notes saved in previous "
            "sessions. Call this when the user asks 'do you remember...', asks about past preferences, "
            "or refers to something you might have stored earlier."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Keywords to search for in memory"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "undo",
        "description": (
            "Revert the most recent changes made to files or settings by an action or coding task. "
            "This only covers your own actions — it is not the Ctrl+Z of whatever "
            "program is on screen. Call with action='list' to inspect the stack first, or "
            "action='undo' to revert the most recent entry."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "'undo' to revert the last action, 'list' to see what can be undone"}
            },
            "required": [],
        },
    },
]


def _describe_tools(declarations) -> str:
    lines = []
    for d in declarations or ():
        try:
            name = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
            desc = (d.get("description") if isinstance(d, dict)
                    else getattr(d, "description", "")) or ""
        except Exception:
            continue
        if not name:
            continue
        desc = " ".join(str(desc).split())
        lines.append(f"- {name}: {desc[:150]}" if desc else f"- {name}")
    return "\\n".join(lines)


def _describe_limits(has_vision: bool, has_mic: bool) -> str:
    out = [
        "- Anything not listed above is outside your reach. Say so in one clause "
        "and offer the nearest thing you can actually do — never mime an action "
        "you cannot take, and never report a result you did not get.",
        "- You act on this machine only. You cannot reach the user's other "
        "devices, accounts or hardware except through the tools listed above.",
        "- You remember what is in the memory block and what has been said this "
        "session. Anything else you were told before is gone unless it was saved.",
    ]
    try:
        from core import platform_support
        out.extend(platform_support.describe_for_model(platform_support.detect()))
    except Exception as _e:
        print(f"[Limits] platform capabilities unavailable: {_e}")

    if has_vision:
        out.append(
            "- Your sight is not continuous. You see nothing until you call a "
            "vision tool, and then only that single frame at that moment — you "
            "cannot watch, monitor or notice something changing on screen.")
    else:
        out.append("- You have no sight at all in this build.")
    if has_mic:
        out.append(
            "- You hear nothing while the microphone is muted, and you cannot "
            "unmute it yourself.")
    return "\\n".join(out)


def _render_prompt(template: str, values: dict) -> str:
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _get_api_key() -> str:
    try:
        from memory.config_manager import get_gemini_key
        k = get_gemini_key()
        if k:
            return k
    except Exception:
        pass
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )
'''
    with open("core/prompt_builder.py", "w", encoding="utf-8") as f:
        f.write(prompt_builder_code)
    print("Created core/prompt_builder.py")

    # 3. core/tool_dispatcher.py
    tool_dispatcher_code = '''"""
core/tool_dispatcher.py — Tool execution engine and dispatch pipeline.
"""
from __future__ import annotations

import asyncio
import time
import traceback
from pathlib import Path
from typing import Any

from google.genai import types
from memory.memory_manager import update_memory, search_memory
from core import undo as undo_stack
from core import confirm as confirm_gate
from core import outcome
from core import platform_hooks


class ToolDispatcher:
    """Dispatches tool calls during a live voice/assistant turn."""

    def __init__(self, session_context: Any = None):
        self.ctx = session_context

    async def execute_tool(self, live: Any, fc: Any) -> types.FunctionResponse:
        live._record_activity()
        if hasattr(live, "voice_state"):
            live.voice_state.begin_command()
        try:
            return await self.execute_tool_inner(live, fc)
        finally:
            if hasattr(live, "voice_state"):
                live.voice_state.end_command()
            live._record_activity()

    async def execute_tool_inner(self, live: Any, fc: Any) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        _ts_now = time.strftime("%I:%M:%S %p")
        print(f"[{_ts_now}] [JARVIS] 🔧 {name}  {args}")
        _t_tool = time.monotonic()
        if live.ui:
            live.ui.set_state("THINKING")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if live.ui and not live.ui.muted and not (hasattr(live, "voice_state") and (live.voice_state.is_disabled or live.voice_state.is_sleeping)):
                live.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "recall_memory":
                result = search_memory(args.get("query", ""), limit=8)

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Things I can undo, most recent first:\\n"
                              + "\\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "I have not changed anything I can undo yet."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "screen_process":
                _now = time.monotonic()
                _cooldown = 4.0
                if live._vision_busy or (_now - live._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - live._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    live._vision_busy      = True
                    live._vision_last_time = _now
                    user_text = args.get("text", "What do you see on my screen?")
                    if live.ui and hasattr(live.ui, "show_screen_glow"):
                        live.ui.show_screen_glow(2500)
                    
                    cap_fn = platform_hooks.get_hook("capture_screen")
                    if cap_fn:
                        img_b, mime_t = await loop.run_in_executor(None, cap_fn)
                    else:
                        img_b, mime_t = b"", "image/jpeg"
                    print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                    live._pending_vision = (img_b, mime_t, user_text, "screen")
                    result = (
                        "[VISION_ACTIVE] Screen captured and attached to this "
                        "same exchange. Do not acknowledge and do not answer yet — the image "
                        "is arriving with this result. Reply once, from what you actually see "
                        "in it."
                    )

            elif name == "system_status":
                status_fn = platform_hooks.get_hook("get_system_status")
                if status_fn:
                    r = await loop.run_in_executor(None, status_fn)
                    result = str(r)
                else:
                    result = "System status hook unavailable."

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    add_fn = platform_hooks.get_hook("add_monitor")
                    result = await asyncio.to_thread(add_fn, topic) if add_fn else "Add monitor unavailable."
                elif action == "remove" and topic:
                    rem_fn = platform_hooks.get_hook("remove_monitor")
                    result = await asyncio.to_thread(rem_fn, topic) if rem_fn else "Remove monitor unavailable."
                elif action == "list":
                    lst_fn = platform_hooks.get_hook("list_monitors")
                    topics = await asyncio.to_thread(lst_fn) if lst_fn else []
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "sleep_jarvis":
                if not live._wake_enabled:
                    live._wake_enabled = True
                    from memory.config_manager import save_wake_word_enabled
                    save_wake_word_enabled(True)
                live.sleep(reason="voice command")
                result = (
                    f"Going to sleep. Say {live._wake_words_label()} "
                    f"or press Ctrl+Alt+K whenever you need me."
                )

            elif name == "shutdown_jarvis":
                def _run_confirmed_shutdown() -> str:
                    if live.ui:
                        live.ui.write_log("SYS: Shutdown confirmed — closing JARVIS.")
                    async def _do_shutdown():
                        await live._save_session_summary()
                        if live.session:
                            try:
                                await live.session.send_client_content(
                                    turns={"role": "user", "parts": [{"text": "Say a brief natural goodbye to the user."}]},
                                    turn_complete=True,
                                )
                            except Exception:
                                pass
                        await asyncio.sleep(1.5)
                        live.shutdown_listeners()
                        import os as _os
                        _os._exit(0)
                    if live._loop and not live._loop.is_closed():
                        asyncio.run_coroutine_threadsafe(_do_shutdown(), live._loop)
                    else:
                        live.shutdown_listeners()
                        import os as _os
                        _os._exit(0)
                    return "Shutting down JARVIS."

                result = confirm_gate.request_action(
                    action="shutdown_jarvis",
                    parameters={},
                    risk_level="high_impact",
                    title="Shut Down JARVIS",
                    detail="Completely close and exit the JARVIS voice assistant.",
                    run=_run_confirmed_shutdown,
                )

            elif live._action_registry and live._action_registry.has(name):
                if name == "file_processor" and not args.get("file_path") and live.ui and live.ui.current_file:
                    args["file_path"] = live.ui.current_file
                if (name == "dev_agent" and not args.get("folder")
                        and live.ui and live.ui.current_file
                        and Path(live.ui.current_file).is_dir()
                        and str(args.get("mode") or "").lower() != "status"):
                    args["folder"] = live.ui.current_file
                    args["mode"] = "improve"
                _ctx = {"player": live.ui, "speak": live.speak,
                        "response": None, "session_memory": None}
                live._ensure_executor()
                try:
                    r = await loop.run_in_executor(None, lambda: live._action_registry.run(name, args, _ctx))
                except RuntimeError as re_err:
                    if "shutdown" in str(re_err).lower():
                        live._ensure_executor()
                        r = await loop.run_in_executor(None, lambda: live._action_registry.run(name, args, _ctx))
                    else:
                        raise
                result = r or "Done."
                if (name == "web_search" and r
                        and not r.startswith("No results")
                        and not r.startswith("Search failed")):
                    _mode  = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    if live.ui:
                        live.ui.show_content(_label, outcome.unwrap(r))

            else:
                if live._plugin_registry and live._plugin_registry.has(name):
                    live._ensure_executor()
                    try:
                        r = await loop.run_in_executor(
                            None,
                            lambda: live._plugin_registry.run(name, args, player=live.ui, session_memory=None)
                        )
                    except RuntimeError as re_err:
                        if "shutdown" in str(re_err).lower():
                            live._ensure_executor()
                            r = await loop.run_in_executor(
                                None,
                                lambda: live._plugin_registry.run(name, args, player=live.ui, session_memory=None)
                            )
                        else:
                            raise
                    result = r or "Done."
                else:
                    result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            try:
                live.speak_error(name, e)
            except Exception:
                pass

        try:
            if live.ui and not live.ui.muted and not (hasattr(live, "voice_state") and (live.voice_state.is_disabled or live.voice_state.is_sleeping)):
                live.ui.set_state("LISTENING")
        except Exception:
            pass

        _ts_done = time.strftime("%I:%M:%S %p")
        print(f"[{_ts_done}] [JARVIS] 📤 {name} ({(time.monotonic() - _t_tool) * 1000:.0f} ms) "
              f"→ {str(result)[:80]}")

        _sched = None
        if live._action_registry:
            _sched = live._action_registry.scheduling(name)
        if not _sched and live._plugin_registry:
            _sched = live._plugin_registry.scheduling(name)
        _extra = {"scheduling": _sched} if _sched else {}
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result},
            **_extra
        )
'''
    with open("core/tool_dispatcher.py", "w", encoding="utf-8") as f:
        f.write(tool_dispatcher_code)
    print("Created core/tool_dispatcher.py")

    # 4. core/session_audio.py
    session_audio_code = '''"""
core/session_audio.py — Audio streaming, listening, viseme generation, and playback mixin.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
import numpy as np
import sounddevice as sd
from google.genai import types

from core.runtime_env import (
    CHANNELS, SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE, CHUNK_SIZE,
    _pcm_level, _pcm_visemes, _is_repeat_chunk, _clean_transcript
)
from core.echo import EchoGuard
from core.viseme import VisemeStream
from core import platform_hooks


class LiveAudioMixin:
    """Audio streaming, microphone capture, and playback pipelines."""

    def _tail_active(self) -> bool:
        """True if the assistant's own mouth audio is still draining through the speaker."""
        with self._speaking_lock:
            spk = self._is_speaking
        draining = (time.monotonic() - self._last_speaker_write) < 0.35
        return spk or draining

    def set_speaking(self, value: bool):
        """Thread-safe setter for speaker active status."""
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self._last_speaker_write = time.monotonic()
        if hasattr(self, "audio_manager") and self.audio_manager is not None:
            self.audio_manager.set_ducking(value)

    def _drain_out_queue(self) -> None:
        """Clear all buffered mic frames from the outgoing queue immediately."""
        q = getattr(self, "out_queue", None)
        if not q:
            return
        while True:
            try:
                q.get_nowait()
            except Exception:
                break

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            if hasattr(self, "voice_state") and (self.voice_state.is_disabled or self.voice_state.is_sleeping):
                continue
            if getattr(self.ui, "muted", False) or not getattr(self, "_awake", True):
                continue
            await self.session.send_realtime_input(
                audio=types.Blob(
                    data=msg["data"],
                    mime_type=msg.get("mime_type", "audio/pcm"),
                )
            )

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            if (hasattr(self, "voice_state") and self.voice_state.is_disabled) or getattr(self.ui, "muted", False):
                return

            if hasattr(self, "audio_manager") and self.audio_manager.transition_state != "idle":
                return

            is_sleeping = (
                (hasattr(self, "voice_state") and self.voice_state.is_sleeping)
                or (not self._awake)
            )
            if is_sleeping:
                det = self._wake_detector
                if det is None and self._wake_enabled:
                    self._ensure_wake_detector_async()
                    det = self._wake_detector
                if det is not None:
                    det.feed(indata.copy())
                return

            if self._phone_active:
                return

            if self._tail_active():
                if self.echo_guard.sample_is_echo(indata):
                    return

            self._last_user_speech = time.monotonic()
            self._record_activity()
            if not self.ui.muted and not self._phone_active:
                self.ui.set_audio_level(_pcm_level(indata))

            try:
                self.out_queue.put_nowait({"data": indata.tobytes(), "mime_type": "audio/pcm"})
            except asyncio.QueueFull:
                pass

        while True:
            try:
                input_dev = self.audio_manager.current_input_device_id
                with sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=input_dev,
                    callback=callback,
                ):
                    while True:
                        await asyncio.sleep(0.5)
            except Exception as e:
                print(f"[JARVIS] ⚠️ Mic error: {e}")
                await asyncio.sleep(1.0)

    async def _flush_pending_vision(self) -> bool:
        """Send any queued screen or camera frame as realtime content."""
        if self._pending_vision is None:
            return False
        img_bytes, mime_type, user_text, source = self._pending_vision
        self._pending_vision = None

        if hasattr(self.ui, "set_state"):
            self.ui.set_state("THINKING")

        print(f"[Vision] 🚀 Injecting {source} frame ({len(img_bytes):,} bytes) with query: '{user_text}'")

        try:
            await self.session.send_realtime_input(
                media_chunks=[
                    types.Blob(data=img_bytes, mime_type=mime_type),
                ]
            )
            await self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": user_text}]},
                turn_complete=True,
            )
            self._arm_stall()
            return True
        except Exception as e:
            print(f"[Vision] ⚠️ Frame injection failed: {e}")
            return False
        finally:
            self._vision_busy = False

    async def _receive_audio(self):
        vstream = VisemeStream(self.ui)
        cur_accum: list[str] = []
        cur_user:  list[str] = []

        try:
            async for response in self.session.receive():
                server_content = response.server_content
                if server_content is not None:
                    model_turn = server_content.model_turn
                    if model_turn is not None:
                        for part in model_turn.parts:
                            text = part.text
                            if text:
                                if not _is_repeat_chunk(text, cur_accum):
                                    cur_accum.append(text)
                                    partial_reply = "".join(cur_accum)
                                    if hasattr(self, "ui") and self.ui:
                                        if hasattr(self.ui, "stream_reply_text"):
                                            self.ui.stream_reply_text(partial_reply)
                                        if not self._is_speaking and not self.ui.muted:
                                            self.ui.set_state("SPEAKING")

                            audio = part.inline_data
                            if audio and audio.data:
                                self._clear_stall()
                                raw_bytes = audio.data
                                self.audio_in_queue.put_nowait(raw_bytes)
                                vstream.feed_bytes(raw_bytes)

                    user_turn = getattr(server_content, "user_turn", None)
                    if user_turn is not None:
                        for part in getattr(user_turn, "parts", []):
                            u_text = getattr(part, "text", "")
                            if u_text:
                                cur_user.append(u_text)
                                partial_user = "".join(cur_user)
                                if hasattr(self, "ui") and self.ui:
                                    if hasattr(self.ui, "stream_user_text"):
                                        self.ui.stream_user_text(partial_user)

                    if server_content.turn_complete:
                        vstream.finish_turn()
                        if hasattr(self, "ui") and self.ui:
                            if hasattr(self.ui, "stream_user_text"):
                                self.ui.stream_user_text("")
                            if hasattr(self.ui, "stream_reply_text"):
                                self.ui.stream_reply_text("")

                        full_in = "".join(cur_user).strip()
                        full_in = _clean_transcript(full_in)
                        if full_in:
                            self._record_activity()
                            self.ui.write_log(f"You: {full_in}")
                            self._session_log.append(f"User: {full_in}")
                            try:
                                from core import confirm as confirm_gate
                                if confirm_gate.pending():
                                    c_norm = full_in.lower().strip()
                                    if any(w in c_norm for w in ("yes", "confirm", "proceed", "do it", "approved", "sure", "go ahead")):
                                        confirm_gate.confirm_latest()
                                        self.ui.write_log("SYS: Confirmation accepted via voice.")
                                    elif any(w in c_norm for w in ("no", "cancel", "stop", "abort", "reject", "don't")):
                                        confirm_gate.reject_latest()
                                        self.ui.write_log("SYS: Confirmation rejected via voice.")
                            except Exception as e:
                                print(f"[Confirm] Error matching voice intent: {e}")

                        full_out = "".join(cur_accum).strip()
                        full_out = _clean_transcript(full_out)
                        if full_out:
                            self.ui.write_log(f"{self._asst_name}: {full_out}")
                            self._session_log.append(f"{self._asst_name}: {full_out}")

                        cur_accum.clear()
                        cur_user.clear()

                        if self._turn_done_event:
                            self._turn_done_event.set()

                        # Reopen mic after response
                        if not getattr(self.ui, "muted", False) and getattr(self, "_awake", True):
                            is_sleeping = (
                                (hasattr(self, "voice_state") and self.voice_state.is_sleeping)
                                or (not self._awake)
                            )
                            if not is_sleeping and not getattr(self.ui, "muted", False):
                                self.ui.set_state("LISTENING")

                    if server_content.interrupted:
                        vstream.clear()
                        cur_accum.clear()
                        cur_user.clear()
                        while not self.audio_in_queue.empty():
                            try:
                                self.audio_in_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                        self.set_speaking(False)
                        self.ui.set_audio_level(0.0)
                        self.ui.set_state("LISTENING")

                tool_call = response.tool_call
                if tool_call is not None:
                    self._clear_stall()
                    for fc in tool_call.function_calls:
                        f_resp = await self._execute_tool(fc)
                        await self.session.send_tool_response(function_responses=[f_resp])
                        self._arm_stall()
                        await self._flush_pending_vision()

        except Exception as e:
            vstream.clear()
            raise

    async def _play_audio(self):
        loop = asyncio.get_event_loop()
        viseme_hop = int(RECEIVE_SAMPLE_RATE * 0.010)

        def play_chunk(data_bytes):
            samples = np.frombuffer(data_bytes, dtype=np.int16)
            lvl = _pcm_level(samples)
            self.ui.set_audio_level(lvl)

            if getattr(self.ui, "muted", False) or (hasattr(self, "voice_state") and self.voice_state.is_disabled):
                return

            self.echo_guard.record_output(samples)
            frames = _pcm_visemes(samples, RECEIVE_SAMPLE_RATE)
            if frames:
                self.ui.push_visemes(frames, hop=viseme_hop)

            if self.audio_manager.playback_stream:
                try:
                    self.audio_manager.playback_stream.write(samples)
                    self._last_speaker_write = time.monotonic()
                except Exception as e:
                    print(f"[Playback] Stream write error: {e}")

        while True:
            chunk = await self.audio_in_queue.get()
            self.set_speaking(True)
            self._last_speaker_write = time.monotonic()
            try:
                await loop.run_in_executor(None, play_chunk, chunk)
            finally:
                self.audio_in_queue.task_done()
                if self.audio_in_queue.empty():
                    self.set_speaking(False)
                    self.ui.set_audio_level(0.0)
'''
    with open("core/session_audio.py", "w", encoding="utf-8") as f:
        f.write(session_audio_code)
    print("Created core/session_audio.py")

    # 5. core/session_monitors.py
    session_monitors_code = '''"""
core/session_monitors.py — Background monitors, proactive engine, briefings, and phone dashboard relay.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from memory.memory_manager import load_memory, pop_last_session, save_session_summary
from core import gemini
from core import platform_hooks


class LiveMonitorsMixin:
    """Background monitoring routines and dashboard synchronization."""

    async def _send_startup_briefing(self) -> None:
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        now = datetime.now()
        hour = now.hour
        if hour < 12:
            period = "Good morning"
        elif hour < 17:
            period = "Good afternoon"
        else:
            period = "Good evening"

        time_str = now.strftime("%I:%M %p").lstrip("0")
        date_str = now.strftime("%A, %B %d, %Y")
        greeting_text = f"{period}, sir! It is {time_str}, {date_str}."

        await asyncio.sleep(0.3)
        if not self.session:
            return

        lang_clause = " Reply in English only."
        name_clause = f" Address the user as {name}." if name else ""

        await asyncio.to_thread(pop_last_session)

        prompt = (
            f"Say exactly: '{greeting_text}'. "
            f"Do not include any motivational speech, talks, inspiration, advice, session recap, or extra commentary. "
            f"Speak little: exactly that short greeting with time and date and stop. Do not call any tools.{lang_clause}{name_clause}"
        )

        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"role": "user", "parts": [{"text": prompt}]},
            turn_complete=True,
        )
        print("[JARVIS] Motivating greeting sent.")

    async def _save_session_summary(self) -> None:
        log = self._session_log
        if len(log) < 3:
            return
        self._session_log = []

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\\n".join(log[-40:])
        prompt = (
            "Summarize this conversation in 1-2 sentences in English. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\\n\\n" + convo
        )
        try:
            summary = await asyncio.to_thread(
                gemini.text, prompt, gemini.SMART, None, 30_000,
            )
            if summary:
                save_session_summary(summary, "English")
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    async def _run_system_monitor(self) -> None:
        while True:
            await asyncio.sleep(10)
            if not hasattr(self, "_sys_monitor") or self._sys_monitor is None:
                continue
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert:
                continue

            clean_alert = alert.replace("[SYSTEM_ALERT]", "").strip()

            if hasattr(self.ui, "show_slide_alert"):
                self.ui.show_slide_alert(clean_alert, title="SYSTEM ALERT")

            is_min = getattr(self.ui, "is_jarvis_minimized", lambda: False)()
            is_mut = getattr(self.ui, "muted", False)
            if is_min and is_mut:
                self.ui.muted = False
                self.ui.write_log("SYS: JARVIS unmuted automatically for system alert.")

            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and (time.monotonic() - self._last_user_speech) >= 5:
                self._awake = True
                try:
                    if self.session:
                        msg = (
                            f"{alert}\\n\\n"
                            "Inform the user immediately and clearly about this system alert in English. "
                            "One brief sentence only."
                        )
                        await self.session.send_client_content(
                            turns={"role": "user", "parts": [{"text": msg}]},
                            turn_complete=True,
                        )
                    else:
                        self.speak(f"System alert: {clean_alert}")
                except Exception as e:
                    _ts = time.strftime("%I:%M:%S %p")
                    print(f"[{_ts}] [Monitor] ⚠️ Could not send alert: {e}")

    async def _run_background_monitor(self) -> None:
        await asyncio.sleep(300)
        while True:
            if self.session and self._awake:
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        chk_fn = platform_hooks.get_hook("monitor_check_all")
                        alerts = await asyncio.to_thread(chk_fn) if chk_fn else []
                        for alert in alerts:
                            msg = (
                                f"{alert}\\n\\n"
                                "Inform the user about this development naturally in English. "
                                "One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            print("[JARVIS] Monitor alert sent.")
                            await asyncio.sleep(6)
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)

    async def _run_proactive_mode(self) -> None:
        while True:
            await asyncio.sleep(60)
            if not self.session or not self._awake:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not hasattr(self, "_proactive") or self._proactive is None:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                lst_fn       = platform_hooks.get_hook("list_monitors")
                monitors     = await asyncio.to_thread(lst_fn) if lst_fn else []
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                print("[JARVIS] Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    async def _relay_phone_audio(self) -> None:
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                self._phone_active = False
                continue
            self._phone_active = True
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    if (hasattr(self, "voice_state") and self.voice_state.is_sleeping) or not self._awake:
                        self.wake(reason="remote command")
                    self._record_activity()
                    await self.session.send_client_content(
                        turns={"role": "user", "parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)
'''
    with open("core/session_monitors.py", "w", encoding="utf-8") as f:
        f.write(session_monitors_code)
    print("Created core/session_monitors.py")

    # 6. core/live_session.py
    # Now let's extract the core JarvisLive class composing these mixins
    with open("main.py", "r", encoding="utf-8") as f:
        src = f.read()

    # Search boundaries in main.py for JarvisLive implementation
    jl_start = src.find("class JarvisLive:")
    jl_end = src.find("def main():")
    jl_code = src[jl_start:jl_end]

    # Let's inspect the methods in JarvisLive and build core/live_session.py
    print(f"Extracted JarvisLive source slice: {len(jl_code.splitlines())} lines")

if __name__ == "__main__":
    main()
