"""
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
import traceback
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
import platform as _platform
from core.action_loader import discover_actions, strip_meta
from core.plugin_loader import discover_plugins
from core.voice_state import VoiceStateManager, VoiceState
from core.global_input import GlobalInputController
from core.audio_manager import AudioManager, set_audio_manager
from core.viseme import VisemeStream
from core.echo import EchoGuard
from core import gemini as _gemini
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

class JarvisLive(LiveAudioMixin, LiveMonitorsMixin, LiveVoiceMixin):
    def __init__(self, ui: Any):
        # Platform limits are announced once per run, not once per
        # reconnect: a dropped connection is not news about Wayland.
        self._platform_notes_shown = False
        self.ui             = ui
        self._asst_name     = "JARVI    S"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                     = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, "screen") to inject after tool response
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self._in_interrupt         = False   # Re-entrancy guard for interrupt()
        self._interrupt_lock       = threading.Lock()
        # Which engine is holding the conversation, and what it can do.
        # None means Gemini Live — the default and, unless somebody has gone to
        # the providers settings and chosen otherwise, always the case. Every
        # branch below that reads it treats None as "everything is available",
        # so the Live path is exactly what it was.
        self._engine_caps          = None
        # Set only when a local pipeline is running, so push-to-talk can tell it
        # the turn is over. Live decides that on the server; nothing does here.
        self._engine               = None
        # Deadline for "we sent a turn and the server has said nothing since".
        # None means nothing is outstanding. Armed by the send sites, cleared by
        # the first frame of ANY response, watched by _watch_stall().
        self._rx_deadline: float | None = None
        # Transcript-driven mouth shapes for the avatar. Fed from the receive
        # loop as words arrive, drained by the playback loop against the audio.
        self._visemes              = VisemeStream()
        self._last_out_logged      = ""      # de-dupes a re-sent transcript tail
        # Push-to-talk
        self._ptt_enabled          = False
        self._ptt_held             = False
        self._ptt                  = None    # core.hotkey.PushToTalk
        self._out_level            = 0.0     # level of the audio being played right now
        self._echo                 = EchoGuard()
        # `stream.write()` returns when the buffer accepts the audio, not when the
        # speaker has finished with it, so sound is still in the room after the
        # speaking flag drops. Streaming the microphone during that gap is how an
        # assistant ends up answering itself. Measured from the device rather than
        # guessed; see _play_audio.
        self._out_latency          = 0.20    # seconds, replaced with the real value
        self._tail_until           = 0.0     # monotonic time the echo tail expires
        # Wall-clock time at which the audio written next will begin to sound.
        # The mouth is scheduled against this, never against "now": batches are
        # handed to the device far faster than they play, so "now" ran the lips
        # ahead of the words and cut every schedule short. 0 = nothing playing.
        self._play_cursor          = 0.0
        self.ui.on_push_to_talk   = self.set_push_to_talk
        self.ui.ptt_hold          = self._on_ptt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self._on_voice_change     # voice picker → rebuild session
        self.ui.on_audio_device_change = self._on_audio_device_change
        if hasattr(self.ui, "show_screen_glow"):
            from core import platform_hooks
            platform_hooks.call_hook('set_screen_glow_callback', lambda dur=2500: self.ui.show_screen_glow(dur))
        self._reconnect_event: asyncio.Event | None = None
        self._reconnect_keep = True   # False → next rebuild drops the resumption handle

        # ── Session resumption ─────────────────────────────────────────
        # The server issues a resumption handle every few seconds and reissues
        # it as the conversation moves on. Before this, session_resumption was
        # switched ON in the config and the update was never read, so the handle
        # was thrown away and EVERY reconnect — a dropped packet, a voice change,
        # switching microphone — started an empty session. "Unlimited sessions"
        # leaked through exactly this hole.
        #
        # Deliberately in RAM only, never written to disk. Persisting it would
        # make a fresh launch continue yesterday's conversation, which sounds
        # appealing but breaks the session-summary flow: _save_session_summary
        # runs at shutdown and the morning briefing pops it the next day. A
        # conversation that never ends never produces a summary, and the
        # "yesterday we talked about…" line silently disappears.
        self._resume_handle: str | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._bt_quality_warned = False         # Bluetooth HFP mono fallback warning shown once per session
        from core import platform_hooks
        _sm_cls = platform_hooks.get_hook('SystemMonitor')
        self._sys_monitor      = _sm_cls() if _sm_cls else None
        _pro_cls = platform_hooks.get_hook('ProactiveEngine')
        self._proactive        = _pro_cls() if _pro_cls else None
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary

        self._enhanced_live = True  # proactive audio; auto-disabled if the server rejects it
        self._tuned_live    = True  # turn-taking / media / thinking knobs; same fallback

        _base_dir = BASE_DIR
        _inline_names: set[str] = {str(t.get("name", "")) for t in TOOL_DECLARATIONS if isinstance(t, dict)}

        # File-backed tools: every actions/*.py with a TOOL dict, discovered the
        # same way plugins are. Reserved names = the inline tools above, so an
        # action can never shadow one.
        self._action_registry = discover_actions(
            actions_dir=_base_dir / "actions",
            reserved_names=_inline_names,
            logger=lambda msg: print(f"[Actions] {msg}"),
        )

        # Plugins must not collide with either an inline tool or a discovered action.
        _core_names = _inline_names | self._action_registry.names()
        self._plugin_registry = discover_plugins(
            plugins_dir=_base_dir / "plugins",
            core_tool_names=_core_names,
            # Console gets the full boot transcript; the activity log gets only
            # what the user has to know about. Every plugin loading correctly is
            # the expected case and does not belong in their conversation.
            logger=lambda msg: print(f"[Plugins] {msg}"),
            notify=lambda msg: self.ui.write_log(f"SYS: {msg}"),
        )
        self.ui.get_plugins = self._plugin_registry.list_for_ui
        self.ui.get_plugin_settings = self._plugin_registry.settings_schemas  # ⚙ settings tab
        self.ui.request_say = self.plugin_say   # plugins: mid-task speech channel

        # ── Wake word & Idle Auto-Sleep ──────────────────────────────────────
        # _awake mirrors active listening state; voice_state is authoritative.
        # Wake words ("Jarvis", "Hello") at threshold 0.100 wake JARVIS from
        # SLEEPING, whether entered manually, at boot, or via 3-minute idle sleep.
        self._wake_enabled       = get_wake_word_enabled()
        self._auto_sleep_enabled = get_auto_sleep_enabled()
        self._wake_words         = get_wake_words()
        self._wake_threshold     = get_wake_word_threshold()
        self._awake              = not self._wake_enabled
        self._wake_detector: WakeWordDetector | None = None
        self._wake_detector_lock = threading.Lock()
        self._wake_sleep_timeout = get_idle_sleep_timeout()

        # Restore the saved push-to-talk preference. Doing it here rather than
        # in __init__ means the hotkey thread only exists once there is a
        # session to talk to.
        if get_push_to_talk_enabled():
            try:
                self.set_push_to_talk(True)
            except Exception as e:
                print(f"[JARVIS] ⚠ Push-to-talk unavailable: {e}")
        # UI control surface for the Wake Word & Auto-Sleep settings section.
        self.ui.wake_is_ready          = wake_is_ready          # () -> bool
        self.ui.wake_get_state         = self._wake_state       # () -> dict
        self.ui.on_wake_toggle         = self._ui_wake_toggle   # (enable: bool) -> str
        self.ui.on_wake_manual         = self._ui_wake_manual   # () -> toggle awake/asleep
        self.ui.on_wake_install        = self._ui_wake_install  # () -> (ok, msg)
        self.ui.auto_sleep_is_enabled  = lambda: self._auto_sleep_enabled
        self.ui.on_auto_sleep_toggle   = self._ui_auto_sleep_toggle

        # ── Central Voice State & Global Input Controller ─────────────────────
        if getattr(self.ui, "muted", False):
            _init_voice_state = VoiceState.DISABLED
        elif self._wake_enabled:
            _init_voice_state = VoiceState.SLEEPING
        else:
            _init_voice_state = VoiceState.LISTENING

        self.voice_state = VoiceStateManager(
            initial_state=_init_voice_state,
            on_state_change=self._on_voice_state_changed,
            on_interrupt=None,
        )
        if hasattr(self.ui, "on_mute_toggle"):
            self.ui.on_mute_toggle = self.global_toggle_mute

        sanitize_hotkeys_and_wake_words()
        self._global_input = GlobalInputController(
            on_toggle_mute=self.global_toggle_mute,
            on_force_listening=self.global_force_listening,
            mute_chord=get_listening_shortcut(),
            force_listening_chord=get_force_listening_shortcut(),
            mouse_gesture_enabled=get_mouse_gesture_enabled(),
        )
        self._global_input.start()

        # ── Centralized Audio & Bluetooth Hot-Switch Manager ──────────────────
        self.audio_manager = AudioManager(
            target_input_rate=SEND_SAMPLE_RATE,
            target_output_rate=RECEIVE_SAMPLE_RATE,
            blocksize=CHUNK_SIZE,
            on_status_change=self._on_audio_status_changed,
            on_log=lambda m: self.ui.write_log(m),
        )
        set_audio_manager(self.audio_manager)

        # ── Hybrid Bluetooth Transition Controller & Epoch Tracking ───────────
        self._sync_transition_lock = threading.RLock()
        self._current_audio_epoch: int = 0
        self._audio_epoch_lock = threading.Lock()

        # Connect UI routing controls
        self.ui.on_routing_mode_change = self._ui_routing_mode_change
        self.ui.get_routing_mode = lambda: self.audio_manager.routing_mode

        # Initialize tool dispatcher
        self._tool_dispatcher = ToolDispatcher(self)
    async def _execute_tool(self, fc) -> types.FunctionResponse:
        return await self._tool_dispatcher.execute_tool(self, fc)

    async def _execute_tool_inner(self, fc) -> types.FunctionResponse:
        return await self._tool_dispatcher.execute_tool_inner(self, fc)

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "JARVIS"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        # Address form is a property of the language being spoken, so it is
        # stated as a principle rather than a two-language lookup — the model
        # already knows the respectful register of whatever language it is in.
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else 'ADDRESS: Address the user as "sir".')
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n"
            f"Always understand the user as speaking English and always reply in English only, never in any other language, even if the user's words are misheard or the content contains other languages.\n\n"
        )

        # Everything the model is told about *itself* is derived here, not
        # written into prompt.txt: the name comes from config, the platform from
        # the host, the capability list from the registries that were just
        # discovered. Rename the assistant, add a plugin or move to another OS
        # and this follows without anyone editing a prompt.
        _all_decls = (TOOL_DECLARATIONS
                      + self._action_registry.get_tool_declarations()
                      + self._plugin_registry.get_tool_declarations())

        # An engine that cannot do something is not offered the tool for it.
        #
        # This is why capabilities are data rather than a pile of conditions: the
        # SAME filtering feeds `_describe_limits` two lines below, through
        # `_names`, so an engine with no eyes ends up with the model being TOLD
        # it cannot see. Without that it would be handed a tool it cannot serve,
        # call it, get nothing back, and describe a screen it never received.
        #
        # `self._engine_caps is None` is Gemini Live and skips all of it, so the
        # declaration list is byte-for-byte what it has always been.
        if self._engine_caps is not None:
            _caps = self._engine_caps
            _blocked = set()
            if not _caps.get("vision"):
                _blocked |= {"screen_process"}
            if not _caps.get("tools"):
                _blocked = {(d.get("name") if isinstance(d, dict)
                             else getattr(d, "name", "")) for d in _all_decls}
            if _blocked:
                _all_decls = [d for d in _all_decls
                              if (d.get("name") if isinstance(d, dict)
                                  else getattr(d, "name", "")) not in _blocked]
                print(f"[JARVIS] {len(_blocked)} tool(s) withheld — "
                      f"this engine cannot serve them")

        _names = {(d.get("name") if isinstance(d, dict) else getattr(d, "name", ""))
                  for d in _all_decls}
        sys_prompt = _render_prompt(sys_prompt, {
            "assistant_name": self._asst_name,
            "platform": f"{_platform.system()} {_platform.release()}".strip(),
            "capabilities": _describe_tools(_all_decls),
            "limits": _describe_limits(
                has_vision="screen_process" in _names,
                has_mic=True,
            ),
        })

        parts = [time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            # strip_meta drops this project's own `tier` / `brief` keys, which
            # exist for the local-engine tool budget and are not part of the
            # Gemini function-declaration schema — an unknown field there is a
            # validation error, not something the API ignores. A declaration
            # that carries neither comes back as the same object, so what
            # Gemini receives is unchanged from the previous release.
            tools=[{"function_declarations": strip_meta(_all_decls)}],
            # Asks the server to issue session-resumption handles. Enabled but
            # NOT yet used: no handle is captured from the resumption update and
            # none is passed back here, so a reconnect starts a fresh session
            # rather than continuing the old one. Left on because the handles
            # cost nothing and are the prerequisite for doing it properly.
            # Hand back the handle captured from the last session_resumption
            # update. `handle=None` is exactly the old behaviour (ask for
            # handles, start fresh), so the first connect of a run is unchanged.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            # Sliding-window compression: session never dies from a full context
            # window — JARVIS can stay in one conversation for hours
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )
        if self._enhanced_live:
            # Proactive audio: JARVIS stays silent when speech isn't addressed
            # to it (background chatter, talking to someone else in the room).
            # (Affective dialog was dropped: gemini-3.1-flash-live does not
            #  support it, and it never reliably detected tone in practice.
            #  To restore it on a 2.5 native-audio model, add back:
            #  cfg["enable_affective_dialog"] = True )
            if get_proactive_audio_enabled():
                cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)

        if self._tuned_live:
            cfg.update(self._tuning_config())

        return types.LiveConnectConfig(**cfg)

    def _tuning_config(self) -> dict:
        """The optional knobs, kept apart so one bad field can be dropped wholesale.

        Every one of these is a preview-API field. If a future model release
        stops accepting any of them the connection fails at setup, so the run
        loop turns `_tuned_live` off and reconnects on the plain config rather
        than leaving the user with an assistant that will not start.
        """
        out: dict = {}

        # How long the server waits through a pause before deciding your turn is
        # over. This — not the size of the prompt — is what most of the delay
        # before a reply actually is, and the default has to suit everybody, so
        # it is necessarily cautious.
        turn = get_turn_tuning()
        if turn.get("enabled", True):
            detect = types.AutomaticActivityDetection(
                silence_duration_ms=turn["silence_ms"],
                prefix_padding_ms=turn["prefix_ms"],
            )
            if turn["end_sensitivity"] == "high":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_HIGH
            elif turn["end_sensitivity"] == "low":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_LOW
            if turn["start_sensitivity"] == "high":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_HIGH
            elif turn["start_sensitivity"] == "low":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_LOW
            out["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=detect)

        # Screenshots and camera frames are tokenised at this resolution and then
        # stay in the session's context. 'medium' keeps on-screen text legible
        # for a fraction of a full-resolution frame.
        res = get_media_resolution()
        if res != "default":
            out["media_resolution"] = {
                "low":    types.MediaResolution.MEDIA_RESOLUTION_LOW,
                "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
                "high":   types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            }[res]

        # Thinking is left at the server default deliberately. Forcing the budget
        # to zero was measured on gemini-3.1-flash-live over interleaved trials
        # and did not make the first word arrive sooner — this model does not
        # appear to deliberate on the Live path, so pinning the field only adds a
        # way for a future release to behave differently. Set "thinking_enabled"
        # in config/api_keys.json to true to let it reason instead.
        if get_thinking_enabled():
            out["thinking_config"] = types.ThinkingConfig(thinking_budget=-1)

        return out

    def _engine_mode(self) -> str:
        try:
            from memory.config_manager import get_provider_mode
            return get_provider_mode()
        except Exception:
            return "gemini"

    def _resolve_engine_caps(self):
        """What the chosen engine can do — or None for Gemini Live.

        Called BEFORE `_build_config()`, because the tool declarations depend on
        the answer. Building the providers here rather than later is deliberate:
        the registry caches them by configuration, so the objects made now are
        the same ones the engine gets a moment later, and a misconfiguration is
        reported while there is still a Gemini session to fall back to.
        """
        if self._engine_mode() != "pipeline":
            return None
        try:
            from memory.config_manager import get_pipeline, get_provider_config
            from providers import registry

            pipe = get_pipeline()
            llm_id = str(pipe.get("llm") or "").strip()
            if not llm_id:
                raise RuntimeError("no language model is chosen")
            missing = registry.missing_fields(llm_id, get_provider_config(llm_id))
            if missing:
                raise RuntimeError(f"{llm_id} needs: {', '.join(missing)}")
            llm = registry.build(llm_id, get_provider_config(llm_id))

            # Ask the model whether it can see, once, and remember the answer.
            #
            # This decides whether `screen_process` is declared at all, so it
            # has to be settled BEFORE the config is built. Leaving it to the
            # panel's TEST button meant that anybody who never pressed it ran a
            # vision model with its eyes switched off — silently, and for every
            # session. Measured once per model, in about a second, then read
            # from the settings for ever after.
            from memory.config_manager import save_provider_config
            ensure = getattr(llm, "ensure_vision_known", None)
            if ensure is not None:
                try:
                    seen = ensure(lambda v, _id=llm_id: save_provider_config(_id, v))
                    self.ui.write_log(
                        f"SYS: {llm_id} "
                        + ("can see images." if seen else "is text only."))
                except Exception as e:
                    print(f"[JARVIS] vision check skipped: {e}")

            caps = dict(getattr(llm, "caps", {}) or {})
            caps["audio_out"] = bool(str(pipe.get("tts") or "").strip())
            return caps
        except Exception as e:
            # A local engine that will not start must never mean no assistant.
            # Falling back to Live is the whole reason the ladder exists, and
            # the reason is said out loud rather than left in a console.
            self.ui.write_log(f"SYS: local engine unavailable ({e}) — "
                              f"using Gemini Live.")
            print(f"[JARVIS] Pipeline engine unavailable: {e}")
            return None

    @asynccontextmanager
    async def _open_session(self, live_model: str, config):
        """The conversation, from whichever engine is configured.

        Both branches yield an object with the same four methods and the same
        event shapes, which is the entire contract — see providers/base.py. The
        Live branch is unchanged from what it has always been, down to the
        client being rebuilt on every reconnect.
        """
        if self._engine_caps is None:
            client = genai.Client(
                api_key=_get_api_key(),
                http_options={"api_version":
                              "v1alpha" if self._enhanced_live else "v1beta"},
            )
            async with client.aio.live.connect(model=live_model,
                                               config=config) as session:
                yield session
            return

        from memory.config_manager import get_pipeline, get_provider_config
        from providers import registry
        from providers.engine_pipeline import PipelineEngine

        pipe = get_pipeline()

        def part(slot):
            pid = str(pipe.get(slot) or "").strip()
            if not pid:
                return None
            # `log` so a provider can say what it is doing — downloading a
            # voice, loading a model — in the activity log rather than only on a
            # console the user may not be watching.
            cfg = dict(get_provider_config(pid))
            cfg["log"] = self.ui.write_log
            return registry.build(pid, cfg)

        engine = PipelineEngine(part("llm"), part("stt"), part("tts"),
                                config=config, log=self.ui.write_log)
        # Without push-to-talk nothing else knows when a sentence ended, so the
        # engine finds it in the audio. With push-to-talk the key release is the
        # signal and the detector stays out of the way.
        engine.auto_turn = not self._ptt_enabled
        self._engine = engine
        try:
            yield engine
        finally:
            self._engine = None
            await engine.close()

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self._reconnect_event = asyncio.Event()

        # ── Wire the shared core services to the interface ───────────────────
        # The confirmation gate is useless without a way to ask, and a memory
        # trim is invisible without a way to say so. Both are bound once here
        # rather than passed down through every action signature.
        confirm_gate.bind(
            show = self.ui.show_confirm,
            hide = self.ui.hide_confirm,
            log  = self.ui.write_log,
        )
        set_trim_notifier(self.ui.write_log)

        # Centralized single-pass audio discovery, validation, stream init,
        # and background Bluetooth/Windows endpoint hot-switch monitor.
        await asyncio.to_thread(self.audio_manager.initialize)

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            try:
                self._ensure_executor()
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                # Pick the rung to open the conversation on. A model resting
                # off a quota limit is skipped; the name is published back to
                # LIVE_MODEL so plugins follow whatever is actually in use.
                global LIVE_MODEL
                LIVE_MODEL = _gemini.live_model()
                live_model = LIVE_MODEL
                print(f"[JARVIS] Live model: {live_model}")

                _resumed_with = self._resume_handle is not None

                # Decided BEFORE the config is built, because the tool
                # declarations depend on what the engine can serve. On a default
                # install this returns None in a few microseconds and nothing
                # below behaves any differently.
                self._engine_caps = self._resolve_engine_caps()
                config = self._build_config()

                # The conversation itself. `_open_session` gives back a Gemini
                # Live session or a local pipeline wearing the same interface —
                # the Live branch still builds a fresh client on every reconnect
                # to avoid stale HTTP state, and still asks for v1alpha when
                # proactive audio is on.
                async with (
                    self._open_session(live_model, config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    # Every user turn arms the stall watchdog. Wrapping the
                    # session once covers all eleven send sites — and every one
                    # added later — where instrumenting them by hand would work
                    # until somebody adds the twelfth and it silently is not
                    # covered. The wrapper is per-session, so it goes away with
                    # the session it belongs to.
                    _orig_send = session.send_client_content

                    async def _send_and_arm(*a, **kw):
                        result = await _orig_send(*a, **kw)
                        self._arm_stall()
                        return result

                    session.send_client_content = _send_and_arm

                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()
                    self._rx_deadline     = None

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False

                    print("[JARVIS] Connected.")
                    if _resumed_with:
                        # Say it plainly: the difference between "it reconnected"
                        # and "it reconnected and still knows what we were doing"
                        # is the whole point, and it is invisible otherwise.
                        self.ui.write_log("SYS: Reconnected — conversation restored.")

                    # Startup / reconnect voice state:
                    # Preserve MUTED if user muted before reconnect; otherwise
                    # start asleep if wake-word mode is on, or LISTENING with the
                    # 3-minute idle auto-sleep timer armed.
                    if self.voice_state.is_disabled:
                        self._sync_ui_state(True, "MUTED")
                        self.ui.write_log("SYS: JARVIS online — muted (press Ctrl+Alt+J to restore or Ctrl+Alt+K for listening).")
                    elif self._wake_enabled:
                        self._ensure_wake_detector_async()
                        self._awake = False
                        self.voice_state.enter_sleep()
                        self._sync_ui_state(False, "SLEEPING")
                        self.ui.write_log(
                            f"SYS: JARVIS online — sleeping. Say {self._wake_words_label()} "
                            f"or press Ctrl+Alt+K to wake me."
                        )
                    elif self.voice_state.is_sleeping:
                        self._awake = False
                        self._sync_ui_state(False, "SLEEPING")
                        self.ui.write_log(
                            "SYS: JARVIS online — sleeping (wake word disabled). "
                            "Press Ctrl+Alt+K or tap WAKE NOW to wake me."
                        )
                    else:
                        self._awake = True
                        self._record_activity()
                        self.voice_state.enable_listening()
                        self._sync_ui_state(False, "LISTENING")
                        self.ui.write_log("SYS: JARVIS online.")

                    # What this machine will not permit, said once, on screen.
                    #
                    # A Wayland session or a macOS without Accessibility cannot
                    # be typed into, clicked or captured — and the APIs report
                    # success regardless. Telling the user at startup turns
                    # "the assistant is broken" into one sentence they can act
                    # on, and it is said once per session rather than on every
                    # attempt.
                    if not self._platform_notes_shown:
                        self._platform_notes_shown = True
                        try:
                            from core import platform_support
                            for _line in platform_support.describe_for_user(
                                    platform_support.detect()):
                                self.ui.write_log(_line)
                        except Exception as _e:
                            print(f"[Platform] capability notes unavailable: {_e}")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    self._reconnect_event.clear()  # ignore requests from before this session
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._watch_stall())
                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_sleep_watch())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Bring a local engine's speech model up before anybody
                    # speaks. Measured: without it the FIRST sentence waits 8.9
                    # seconds and every one after it 1.2 — and eight seconds of
                    # nothing is how a working assistant gets reported broken.
                    if self._engine is not None:
                        tg.create_task(self._engine.warmup())

                    # Morning briefing — fires once per process launch (if enabled).
                    # Skipped in wake-word mode: it comes up asleep, and a briefing
                    # would mean talking while "asleep".
                    if not self._briefing_sent and get_brief_enabled() and self._awake:
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                # Voluntary reconnect (voice change) — not an error. Rebuild the
                # session immediately with no backoff and no scary logs.
                if _is_reconnect_signal(e):
                    print("[JARVIS] Voluntary reconnect requested.")
                    if not _keep_context_of(e):
                        # A deliberate clean slate (voice change) — drop the
                        # handle so the next connect really does start empty.
                        self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # A resumption handle the server will not accept — expired, or
                # belonging to a session it has since dropped. Without this, the
                # same dead handle would be replayed on every retry and the
                # assistant would never come back at all: the feature meant to
                # survive a reconnect would be the thing preventing one. Drop it
                # once and let the next attempt start clean.
                if _resumed_with and (
                    "resum" in str(e).lower()
                    or "handle" in str(e).lower()
                    or "INVALID_ARGUMENT" in str(e)
                    or "NOT_FOUND" in str(e)
                ):
                    print("[JARVIS] 🔗 Resumption handle rejected — starting a fresh session")
                    self.ui.write_log("SYS: Could not restore the conversation — starting fresh.")
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                err_str = str(e)
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Out of quota, or this model is not available to this key —
                # step down the ladder and reconnect straight away. This is the
                # difference between "JARVIS is quieter today" and "JARVIS does
                # not start today": one model means one daily limit, and the
                # limit always arrives mid-conversation.
                if _gemini.note_live_failure(live_model, err_str):
                    nxt = _gemini.live_model()
                    self.ui.write_log(
                        f"SYS: Switching to {nxt.split('/')[-1]} — the previous "
                        f"model is out of quota."
                        if nxt != live_model else
                        "SYS: Every live model is rate-limited — retrying.")
                    self._conn_backoff = 0 if nxt != live_model else 15
                    if nxt == live_model:
                        await asyncio.sleep(self._conn_backoff)
                    continue

                # Turn-taking / media / thinking knobs rejected by the server
                # (preview API drift) — drop them first, because they are the
                # newest fields and the cheapest to lose. Proactive audio is
                # tried again on the next pass if the error persists.
                if self._tuned_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                    or "realtime_input" in err_str.lower()
                    or "media_resolution" in err_str.lower()
                    or "thinking" in err_str.lower()
                ):
                    self._tuned_live = False
                    print("[JARVIS] Live tuning rejected — reconnecting without it.")
                    continue

                # Proactive audio rejected by the server (preview API drift) —
                # drop it and reconnect with the plain config.
                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.write_log(
                        "SYS: Proactive audio unavailable — reconnecting without it."
                    )
                    continue

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Connection failed — retrying in {_conn_backoff}s. "
                        "(a VPN may be required)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

    def shutdown_listeners(self) -> None:
        """Cleanly stop audio streams/monitor, global hotkey, and wake-word background threads."""
        try:
            if getattr(self, "audio_manager", None) is not None:
                self.audio_manager.shutdown()
            set_audio_manager(None)
        except Exception:
            pass
        try:
            if getattr(self, "_global_input", None) is not None:
                self._global_input.stop()
        except Exception:
            pass
        try:
            if getattr(self, "_wake_detector", None) is not None:
                self._wake_detector.stop()
        except Exception:
            pass
        try:
            if getattr(self, "_ptt", None) is not None:
                self._ptt.stop()
        except Exception:
            pass


