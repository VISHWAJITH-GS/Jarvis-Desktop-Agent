"""
core/session_voice.py — Voice state, wake word, PTT, mute, and reconnection mixin.
"""

from __future__ import annotations
import asyncio
import threading
import time
from typing import Any
from core.runtime_env import (
    _ReconnectSignal,
    _is_reconnect_signal,
    _keep_context_of,
    SEND_SAMPLE_RATE,
    RECEIVE_SAMPLE_RATE,
    CHUNK_SIZE,
)
from core.wake_word import WakeWordDetector, is_ready as wake_is_ready, install_and_download as wake_install
from core.voice_state import VoiceState
from core import audio_devices
from memory.config_manager import (
    get_input_device,
    get_output_device,
    save_wake_word_enabled,
    save_auto_sleep_enabled,
    get_push_to_talk_enabled,
    get_idle_sleep_timeout,
    DEFAULT_IDLE_SLEEP_TIMEOUT,
)

IDLE_SLEEP_TIMEOUT = 180.0
WAKE_SLEEP_TIMEOUT = 180.0
_STALL_LIMIT = 45.0
_TAIL_MARGIN = 0.25


class LiveVoiceMixin:
    """Voice state management, wake word, and control handling."""

    def _on_audio_status_changed(self, status: dict) -> None:
        """Push verified AudioManager status snapshots to the HUD thread-safely."""
        try:
            self._out_latency = float(status.get("output_latency", self._out_latency) or self._out_latency)
        except Exception:
            pass
        if hasattr(self.ui, "update_audio_status"):
            self.ui.update_audio_status(status)
        if hasattr(self.ui, "refresh_audio_devices"):
            self.ui.refresh_audio_devices()

    # ── Wake word & idle sleep state machine ─────────────────────────────────

    def _wake_words_label(self) -> str:
        words = getattr(self, "_wake_words", None) or ["Jarvis"]
        if len(words) == 1:
            return f"'{words[0]}'"
        return " or ".join(f"'{w}'" for w in words)

    def _record_activity(self) -> None:
        """Reset the idle auto-sleep timer on speech, commands, or wake."""
        now = time.monotonic()
        self._last_user_speech = now
        if hasattr(self, "voice_state"):
            self.voice_state.touch_activity(now)

    def _sync_ui_state(self, muted: bool, hud_state: str) -> None:
        """Thread-safely synchronize mute flag and HUD state without clobbering."""
        if not hasattr(self, "ui") or self.ui is None:
            return
        if hasattr(self.ui, "sync_mute_state"):
            self.ui.sync_mute_state(muted, hud_state)
        else:
            self.ui.muted = muted
            self.ui.set_state(hud_state)

    def _drain_out_queue(self) -> None:
        q = getattr(self, "out_queue", None)
        if not q:
            return
        while True:
            try:
                q.get_nowait()
            except Exception:
                break

    def _wake_state(self) -> dict:
        # A loaded, running detector is definitively ready; otherwise fall back
        # to the cheap on-disk model-file check (no Model construction).
        ready = bool(self._wake_detector and self._wake_detector.ready) or wake_is_ready()
        awake = not self.voice_state.is_sleeping if hasattr(self, "voice_state") else bool(self._awake)
        return {
            "enabled": self._wake_enabled,
            "awake": awake,
            "ready": ready,
            "wake_words": list(self._wake_words),
            "threshold": self._wake_threshold,
        }

    def _ensure_wake_detector(self) -> bool:
        """Load the detector once (model loads on first start). Idempotent & thread-safe."""
        if not self._wake_enabled:
            return False
        with self._wake_detector_lock:
            if self._wake_detector is None:
                self._wake_detector = WakeWordDetector(
                    on_detect=self._on_wake_detected,
                    threshold=self._wake_threshold,
                    wake_words=self._wake_words,
                    logger=lambda m: print(f"[Wake] {m}"),
                    notify=lambda m: self.ui.write_log(f"SYS: {m}"),
                )
            if not self._wake_detector.ready:
                return self._wake_detector.start()
            if getattr(self._wake_detector, "paused", False):
                self._wake_detector.resume()
            return True

    def _ensure_wake_detector_async(self) -> None:
        """Start the wake-word detector without blocking the asyncio loop or hotkey thread."""
        if not self._wake_enabled:
            return
        det = getattr(self, "_wake_detector", None)
        if det is not None and det.ready:
            return
        threading.Thread(
            target=self._ensure_wake_detector,
            daemon=True,
            name="wake-detector-init",
        ).start()

    def _on_wake_detected(self, word: str = "wake word") -> None:
        """Called from the detector thread when an active wake word is heard."""
        if hasattr(self, "voice_state") and self.voice_state.is_disabled:
            return  # Never wake from muted state via wake word
        label = f"wake word '{word}'" if word and word != "wake word" else "wake word"
        self.wake(reason=label)

    def wake(self, reason: str = "wake word") -> None:
        if hasattr(self, "voice_state"):
            if self.voice_state.is_disabled:
                return
            changed = self.voice_state.wake_up()
            if not changed and self._awake:
                self._record_activity()
                return
        elif self._awake:
            return
        self._awake = True
        self._record_activity()  # start the 3-minute auto-sleep clock fresh
        if not hasattr(self, "voice_state") and not self.ui.muted:
            self.ui.set_state("LISTENING")
        self.ui.write_log(f"SYS: Awake — {reason}.")
        if "wake word" in str(reason).lower() and getattr(self, "session", None):
            self.speak(
                "The user called 'Jarvis' to wake you up. Acknowledge briefly and naturally, like 'Yes, Sir?' or 'At your service, Sir.'"
            )

    def sleep(self, reason: str = "idle for 3 minutes") -> None:
        if hasattr(self, "voice_state"):
            if self.voice_state.is_disabled:
                return  # Do not override muted state with sleep
            changed = self.voice_state.enter_sleep()
            if not changed and not self._awake:
                return
        elif not self._awake:
            return
        self._awake = False
        self._drain_out_queue()
        self.set_speaking(False)
        if self._wake_enabled:
            self._ensure_wake_detector_async()
            with self._wake_detector_lock:
                if self._wake_detector is not None:
                    self._wake_detector.reset_buffers()
                    self._wake_detector.resume()
            wake_hint = f"Say {self._wake_words_label()} or press Ctrl+Alt+K to wake me."
        else:
            wake_hint = "Press Ctrl+Alt+K or tap WAKE NOW to wake me."
        if not hasattr(self, "voice_state"):
            self.ui.set_state("SLEEPING")
        self.ui.write_log(f"SYS: Sleeping — {reason}. {wake_hint}")

    async def _run_sleep_watch(self) -> None:
        """Auto-sleep after inactivity (default 3 minutes), unless auto-sleep is disabled."""
        while True:
            await asyncio.sleep(2)
            if not getattr(self, "_auto_sleep_enabled", False) or (
                self._wake_sleep_timeout is not None and self._wake_sleep_timeout <= 0
            ):
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                self._record_activity()
                continue
            if hasattr(self, "voice_state"):
                if self.voice_state.should_auto_sleep(self._wake_sleep_timeout):
                    mins = int(round(self._wake_sleep_timeout / 60.0))
                    label = f"idle for {mins} minute{'s' if mins != 1 else ''}"
                    self.sleep(reason=label)
            elif self._awake and (time.monotonic() - self._last_user_speech) >= self._wake_sleep_timeout:
                self.sleep(reason="idle for 3 minutes")

    # ── Wake word & Auto-sleep: UI callbacks (called from the Qt thread) ────

    def _ui_wake_toggle(self, enable: bool) -> str:
        """Enable/disable wake word from the settings UI. Returns a status token:
        'enabled' | 'disabled' | 'need_download'."""
        if enable:
            if not wake_is_ready():
                return "need_download"
            self._wake_enabled = True
            save_wake_word_enabled(True)
            self._ensure_wake_detector_async()
            self.sleep(reason="wake word enabled")
            return "enabled"
        else:
            self._wake_enabled = False
            save_wake_word_enabled(False)
            with self._wake_detector_lock:
                if self._wake_detector is not None:
                    try:
                        self._wake_detector.stop()
                    except Exception:
                        pass
                    self._wake_detector = None
            self.wake(reason="wake word disabled")
            return "disabled"

    def _ui_auto_sleep_toggle(self, enable: bool) -> bool:
        """Enable or disable auto-sleep."""
        self._auto_sleep_enabled = bool(enable)
        save_auto_sleep_enabled(self._auto_sleep_enabled)
        if self._auto_sleep_enabled:
            self._wake_sleep_timeout = get_idle_sleep_timeout() or DEFAULT_IDLE_SLEEP_TIMEOUT
            self.ui.write_log("SYS: Auto-sleep enabled (3 min idle timeout).")
        else:
            self._wake_sleep_timeout = 0.0
            self.ui.write_log("SYS: Auto-sleep disabled (always awake).")
            if hasattr(self, "voice_state") and self.voice_state.is_sleeping:
                self.wake(reason="auto-sleep disabled")
        return self._auto_sleep_enabled

    def _ui_wake_manual(self) -> None:
        """Manual sleep/wake button in the UI."""
        is_sleeping = self.voice_state.is_sleeping if hasattr(self, "voice_state") else (not self._awake)
        if is_sleeping:
            self.wake(reason="you tapped wake")
        else:
            self.sleep(reason="you tapped sleep")

    def _ui_wake_install(self) -> tuple[bool, str]:
        """Download openwakeword + the model (runs in a UI worker thread)."""
        # Triggered by the user pressing the button, so its progress is exactly
        # what they are waiting to see.
        return wake_install(logger=lambda m: print(f"[Wake] {m}"), notify=lambda m: self.ui.write_log(f"SYS: {m}"))

    # ── Hybrid Bluetooth Audio Routing & Transition Control ──────────────────

    def _ui_routing_mode_change(self, mode: str) -> None:
        """Called when user changes audio routing mode via UI."""
        if not hasattr(self, "audio_manager"):
            return
        self.audio_manager.set_routing_mode(mode)
        self.ui.write_log(f"SYS: Audio routing mode set to '{mode}'.")
        if mode == "built_in_only":
            threading.Thread(
                target=lambda: self.audio_manager.switch_input(
                    self.audio_manager.builtin_mic_name
                    or audio_devices.find_builtin_microphone()
                    or audio_devices.DEFAULT_LABEL
                ),
                daemon=True,
                name="routing-mode-builtin",
            ).start()
        elif mode == "bluetooth_only":
            threading.Thread(
                target=lambda: self.audio_manager.switch_input(
                    self.audio_manager.bluetooth_mic_name
                    or audio_devices.find_bluetooth_microphone()
                    or audio_devices.DEFAULT_LABEL
                ),
                daemon=True,
                name="routing-mode-bt",
            ).start()
        elif mode == "auto_hybrid":
            is_sleeping = (hasattr(self, "voice_state") and self.voice_state.is_sleeping) or (not self._awake)
            target = "sleep" if is_sleeping else "conversation"
            self._trigger_async_transition(target, reason="mode_change_auto_hybrid")

    def _trigger_async_transition(self, target: str, reason: str = "") -> None:
        """
        Coordinates asynchronous hybrid transition between sleeping (laptop mic)
        and conversation (OPPO Bluetooth mic) modes.
        Acquires exclusive transition lock, pauses wake-word / increments epoch,
        switches stream in AudioManager, verifies frames and A2DP state.
        """
        if not hasattr(self, "audio_manager") or self.audio_manager.routing_mode != "auto_hybrid":
            return

        def _do_transition():
            with self._sync_transition_lock:
                with self._audio_epoch_lock:
                    self._current_audio_epoch += 1

                if target == "conversation":
                    with self._wake_detector_lock:
                        if self._wake_detector is not None:
                            self._wake_detector.pause()

                    ok, dev_name = self.audio_manager.switch_to_conversation_input()
                    if ok:
                        self.ui.write_log(f"SYS: 🎧 Bluetooth mic active ({dev_name}) for conversation.")
                    else:
                        self.ui.write_log(f"SYS: ⚠ Bluetooth mic unavailable; using {dev_name}.")

                elif target == "sleep":
                    self._drain_out_queue()
                    ok, dev_name = self.audio_manager.switch_to_sleep_input()

                    with self._wake_detector_lock:
                        if self._wake_detector is not None:
                            self._wake_detector.reset_buffers()
                            self._wake_detector.resume()
                        elif self._wake_enabled:
                            self._ensure_wake_detector_async()

                    self.ui.write_log(f"SYS: 😴 Stereo music restored. Listening for wake word on {dev_name}.")

        threading.Thread(
            target=_do_transition,
            daemon=True,
            name=f"hybrid-audio-transition-{target}",
        ).start()

    def plugin_say(self, instruction: str) -> None:
        """
        Thread-safe speech channel for plugins: lets a plugin ask JARVIS to
        say something short WHILE its run() is still executing (plugins block
        their executor thread, so they can't speak through the tool response
        until they finish). The instruction is injected into the Live session
        exactly like a proactive check-in; Gemini phrases it naturally in the
        user's language. Silently a no-op when no session is connected.
        """
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return

        async def _say():
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": instruction}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")

    # ── stall watchdog ───────────────────────────────────────────────────────

    def _arm_stall(self) -> None:
        """A user turn has gone out; start the clock if one is not already running.

        Deliberately does NOT push an existing deadline forward. The question is
        "how long since the server last said anything while we were waiting",
        not "how long since the most recent send" — otherwise a burst of sends
        into a dead socket would keep resetting the very timer meant to catch it.
        """
        if self._rx_deadline is None:
            self._rx_deadline = time.monotonic() + _STALL_LIMIT

    def _clear_stall(self) -> None:
        """The server spoke, so the return path is alive."""
        self._rx_deadline = None

    async def _watch_stall(self) -> None:
        """Rebuild the session when a sent turn goes unanswered past the limit.

        The reconnect keeps the resumption handle, so the conversation survives:
        this costs a couple of seconds and a line in the log, where before it
        cost the rest of the session with no indication anything was wrong.
        """
        while True:
            await asyncio.sleep(2)
            deadline = self._rx_deadline
            if deadline is None or time.monotonic() < deadline:
                continue
            self._clear_stall()
            print(
                f"[JARVIS] ⚠️  No response for {_STALL_LIMIT:.0f}s — the live session is not answering. Rebuilding it."
            )
            self.ui.write_log("SYS: The model stopped answering — reconnecting.")
            self.request_reconnect(keep_context=True, reason="no response")
            return  # this session is going away; the new one gets its own watcher

    def request_reconnect(self, keep_context: bool = True, reason: str = ""):
        """Thread-safe: ask the run loop to tear down and rebuild the Live
        session. Called from the Qt thread. No-op until the async loop and
        reconnect event exist.

        `keep_context=False` drops the resumption handle so the new session
        starts empty — only for changes the server cannot apply to a resumed
        session."""
        loop = getattr(self, "_loop", None)
        ev = self._reconnect_event
        self._reconnect_keep = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def request_engine_switch(self, new_engine: str):
        """Thread-safe: Switch active engine (gemini_live <-> local) without restart, keeping context."""
        from memory import config_manager
        target = config_manager.ENGINE_LOCAL if str(new_engine).lower() in ("local", "pipeline", "ollama") else config_manager.ENGINE_GEMINI_LIVE
        config_manager.set_active_engine(target)
        dest = config_manager.get_audio_destination_label()
        eng_name = target.replace('_', ' ').title()
        self.ui.write_log(f"SYS: Engine switched to {eng_name}. Audio destination: {dest}")
        self._just_switched_engine = True
        self.request_reconnect(keep_context=True, reason=f"engine switch to {target}")

    def _on_engine_change(self):
        """Engine changed from UI settings drawer. Rebuild session with context preserved."""
        from memory import config_manager
        dest = config_manager.get_audio_destination_label()
        engine = config_manager.get_active_engine().replace('_', ' ').title()
        self.ui.write_log(f"SYS: Applying engine change ({engine}). Audio destination: {dest}")
        self._just_switched_engine = True
        self.request_reconnect(keep_context=True, reason="engine settings change")

    def _on_voice_change(self):
        """Voice picker applied.

        The voice is baked into the session at connect time, so a rebuild is
        required. It is rebuilt WITHOUT the resumption handle on purpose:
        resuming restores the server's own session state, and the safe reading
        is that it restores the voice with it — which would make the picker
        appear to do nothing. Losing context here is acceptable because changing
        voice is a deliberate, rare act; losing it on a dropped packet was not."""
        self.request_reconnect(keep_context=False, reason="new voice")

    def _on_audio_device_change(self):
        """Microphone or speaker changed. Hot-switch streams in-place via AudioManager
        without tearing down the active Gemini Live session."""
        _in_name = get_input_device()
        _out_name = get_output_device()
        self.ui.write_log(
            f"SYS: Switching audio devices in-place — Mic: '{_in_name or 'System default'}', "
            f"Speaker: '{_out_name or 'System default'}'"
        )
        mgr = getattr(self, "audio_manager", None)
        if mgr is not None:
            threading.Thread(
                target=mgr.apply_saved_preferences,
                daemon=True,
                name="audio-apply-prefs",
            ).start()
        else:
            self.request_reconnect(keep_context=True, reason="audio device")

    def _ensure_executor(self):
        """Give the loop a working thread pool before every connect.

        Reported: a reconnect died with "cannot schedule new futures after
        shutdown" while resolving Gemini's address — the loop's default pool
        had been shut down underneath a loop that was still running. Name
        resolution, every tool and every `to_thread` go through that pool, so
        once it is gone the assistant can never connect again and retries the
        same failure forever. Whatever shut it, a fresh pool here turns that
        into one lost attempt instead of a dead session.

        The loop owns a pool of our own rather than asyncio's default, sized so
        a long coding job or a slow tool cannot starve address lookups."""
        from concurrent.futures import ThreadPoolExecutor

        loop = asyncio.get_running_loop()
        ex = getattr(loop, "_default_executor", None)
        dead = ex is not None and (getattr(ex, "_shutdown", False) or getattr(ex, "_broken", False))
        if ex is None or dead or getattr(loop, "_executor_shutdown_called", False):
            if dead:
                print("[JARVIS] ⚠️  Thread pool was shut down — replacing it")
                self.ui.write_log("SYS: Recovered from a stopped worker pool.")
            # Without this flag reset, asyncio refuses the new pool as well.
            if getattr(loop, "_executor_shutdown_called", False):
                loop._executor_shutdown_called = False
            loop.set_default_executor(ThreadPoolExecutor(max_workers=32, thread_name_prefix="jarvis"))

    async def _watch_reconnect(self):
        """Session-scoped task: when a voluntary reconnect is requested, raise a
        signal that unwinds the TaskGroup so the run loop rebuilds the session."""
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep = self._reconnect_keep
        reason = getattr(self, "_reconnect_reason", "") or "settings"
        self.ui.write_log(
            f"SYS: Applying {reason} — reconnecting" + ("..." if keep else " (starting a fresh conversation)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    async def _watch_audio_device_changes(self):
        """Session-scoped monitor: detects OS audio endpoint changes (e.g. Bluetooth headphones
        connected or disconnected after startup) and seamlessly updates audio routing."""
        try:
            from core import audio_devices

            initial_spk_id, initial_spk_name = audio_devices.get_current_default_endpoint("output")
            last_snapshot = audio_devices.get_connected_endpoints_snapshot()
            check_count = 0

            while True:
                await asyncio.sleep(1.5)
                check_count += 1

                # 1. Check if default Windows playback device changed
                cur_spk_id, cur_spk_name = audio_devices.get_current_default_endpoint("output")
                default_changed = bool(cur_spk_id and initial_spk_id and cur_spk_id != initial_spk_id)

                # 2. Check every ~3s if device topology changed
                topology_changed = False
                if check_count % 2 == 0:
                    cur_snapshot = audio_devices.get_connected_endpoints_snapshot()
                    if cur_snapshot and last_snapshot and cur_snapshot != last_snapshot:
                        diff = cur_snapshot - last_snapshot
                        saved_spk = get_output_device()
                        if saved_spk and saved_spk != audio_devices.DEFAULT_LABEL:
                            if any(saved_spk.lower() in item.lower() for item in diff):
                                topology_changed = True
                        else:
                            if any(audio_devices.is_bluetooth_device(item) for item in diff):
                                topology_changed = True
                    last_snapshot = cur_snapshot

                if default_changed or topology_changed:
                    # Let Windows finish registering audio endpoints
                    await asyncio.sleep(1.0)
                    audio_devices.invalidate_cache()

                    new_spk_id, new_spk_name = audio_devices.get_current_default_endpoint("output")
                    active_label = new_spk_name or cur_spk_name or "New Device"

                    print(f"[JARVIS] 🎧 Audio device change detected ('{active_label}') — switching audio routing")
                    self.ui.write_log(
                        f"SYS: 🎧 Audio device change detected ('{active_label}') — switching audio routing."
                    )
                    if hasattr(self.ui, "refresh_audio_devices"):
                        self.ui.refresh_audio_devices()

                    self.request_reconnect(keep_context=True, reason="audio device change")
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[JARVIS] Audio device watcher notice: {e}")

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None or not getattr(self._dashboard, "_running", False):
            try:
                from dashboard.server import DashboardServer

                if self._dashboard is None:
                    self._dashboard = DashboardServer()
                    self._dashboard.set_connect_callback(self._on_phone_connected)
                if not getattr(self._dashboard, "_running", False) and self._loop and not self._loop.is_closed():
                    asyncio.run_coroutine_threadsafe(self._dashboard.serve(), self._loop)
                    asyncio.run_coroutine_threadsafe(self._process_dashboard_commands(), self._loop)
            except Exception as e:
                self.ui.write_log(f'SYS: Dashboard unavailable ({e}). Run: pip install fastapi "uvicorn[standard]" cryptography')
                return None
        # The firewall is fixed HERE, not at startup: this is the moment the
        # user asked for the phone dashboard, so a Windows admin prompt now has
        # an obvious reason. Once per run, and a no-op when nothing is blocked.
        try:
            from dashboard.server import PORT, SECURE_PORT, fix_network_access

            threading.Thread(
                target=fix_network_access, args=(PORT, SECURE_PORT), daemon=True, name="dashboard-firewall"
            ).start()
        except Exception as e:
            print(f"[Dashboard] Firewall check skipped: {e}")
        key = self._dashboard.new_key()
        url = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        # The bind error travels with the URL because the UI has no reference to
        # this object — it only ever sees what this callback returns.
        return (url, key, f"{url}/auto-login?key={key}", manual, getattr(self._dashboard, "_bind_error", "") or "")

    def filter_incoming_transcript(self, text: str) -> tuple[bool, str]:
        """Check whether incoming user speech transcript is an audio artifact or noise."""
        from core.speech_filter import should_drop_transcript
        is_speaking = self._tail_active() or (
            hasattr(self, "voice_state") and self.voice_state.is_speaking
        )
        return should_drop_transcript(text, is_speaking_or_tail=is_speaking)

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        drop, _ = self.filter_incoming_transcript(text)
        if drop:
            return
        try:
            from core.telemetry_service import get_event_bus
            get_event_bus().emit_event("RECV", f"request received · {len(text)} chars", level="info")
            get_event_bus().emit_status_pill(agent_state="THINKING")
        except Exception:
            pass
        # Respect sleep mode: a typed command must not be answered while asleep.
        is_sleeping = (
            self.voice_state.is_sleeping if hasattr(self, "voice_state") else (self._wake_enabled and not self._awake)
        )
        if is_sleeping:
            self.ui.write_log(
                f"SYS: I'm asleep — say {self._wake_words_label()}, press Ctrl+Alt+K, or tap WAKE NOW first."
            )
            return
        self._record_activity()

        # Intercept confirmations directly in code BEFORE the model sees them
        try:
            from core import confirm
            intercepted = confirm.intercept_confirmation(
                text,
                player=self.ui,
                speak=self.speak if hasattr(self, "speak") else None,
            )
            if intercepted is not None:
                self.ui.write_log(f"You: {text}")
                if self.session:
                    res_text = getattr(intercepted, "message", None) or (
                        intercepted.to_speech_text()
                        if hasattr(intercepted, "to_speech_text")
                        else str(intercepted)
                    )
                    feedback_text = (
                        f"[ACTION_RESULT] User voice confirmation executed '{getattr(intercepted, 'action', '')}'. "
                        f"Result: {res_text}. Report this status truthfully; do NOT re-call '{getattr(intercepted, 'action', '')}'."
                    )
                    asyncio.run_coroutine_threadsafe(
                        self.session.send_client_content(
                            turns={"role": "user", "parts": [{"text": feedback_text}]},
                            turn_complete=True,
                        ),
                        self._loop,
                    )
                return
        except Exception as c_err:
            print(f"[Confirm] Interception error: {c_err}")

        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(turns={"role": "user", "parts": [{"text": text}]}, turn_complete=True),
            self._loop,
        )

    def _tail_active(self) -> bool:
        """True while the speakers may still be finishing our last sentence."""
        return time.monotonic() < self._tail_until

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        try:
            from core.ducking import notify_speech_activity

            notify_speech_activity(bool(value))
        except Exception:
            pass
        if value:
            self._tail_until = 0.0
        else:
            # Hold the guard open across the device's own output latency plus a
            # margin for the room. The microphone is NOT muted during it — the
            # guard still lets a genuine reply through, so answering instantly
            # still works. Only our own echo is dropped.
            self._tail_until = time.monotonic() + self._out_latency + _TAIL_MARGIN
        if not value:
            # The echo history is deliberately NOT cleared here: the tail above
            # still needs it to recognise our own voice. It is dropped when the
            # tail expires. What the guard learned about the room always stays.
            self._out_level = 0.0
        if value:
            self.ui.set_state("SPEAKING")
        else:
            is_muted = getattr(self.ui, "muted", False) or (
                hasattr(self, "voice_state") and self.voice_state.is_disabled
            )
            is_sleeping = (hasattr(self, "voice_state") and self.voice_state.is_sleeping) or (
                not getattr(self, "_awake", True)
            )
            if not is_muted and not is_sleeping:
                self.ui.set_state("LISTENING")

    def set_push_to_talk(self, enabled: bool) -> str:
        """Turn hold-to-talk on or off. Returns the scope actually achieved."""
        from core.hotkey import PushToTalk

        self._ptt_enabled = bool(enabled)
        self._ptt_held = False
        if not enabled:
            if self._ptt is not None:
                self._ptt.stop()
                self._ptt = None
            return "off"

        if self._ptt is None:
            self._ptt = PushToTalk(self._on_ptt)
        scope = self._ptt.start()
        # A window-scoped chord is a real limitation, not a detail — say it once
        # in the log so nobody wonders why it does nothing while another app is
        # focused. Reporting it must never be able to undo the thing it reports.
        try:
            self.ui.write_log(
                f"SYS: Push-to-talk on — hold {self._ptt.label}"
                + ("." if scope == "global" else " (works while this window is focused).")
            )
        except Exception:
            pass
        return scope

    def _on_ptt(self, held: bool) -> None:
        """Chord pressed or released — may arrive on the hotkey thread."""
        self._ptt_held = held
        if held:
            # Holding the key is also a way to wake from sleep.
            if hasattr(self, "voice_state") and self.voice_state.is_sleeping:
                self.wake(reason="push-to-talk")
            elif self._wake_enabled and not self._awake:
                self._awake = True
                self._record_activity()
            else:
                self._record_activity()
        # Releasing the key ends the turn. On Live the server works that out from
        # the audio; a local pipeline has no server, so the release IS the
        # signal — which is also why it is the more reliable of the two ways to
        # talk to one. Scheduled onto the loop because this may arrive on the
        # hotkey thread.
        if not held and self._engine is not None and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._engine.flush_microphone(SEND_SAMPLE_RATE), self._loop)
            except Exception as e:
                print(f"[JARVIS] PTT flush failed: {e}")
        try:
            if hasattr(self, "voice_state") and self.voice_state.is_disabled:
                return
            if held:
                state = "LISTENING"
            else:
                is_sleeping = (
                    self.voice_state.is_sleeping
                    if hasattr(self, "voice_state")
                    else (self._wake_enabled and not self._awake)
                )
                state = "SLEEPING" if is_sleeping else "LISTENING"
            self.ui.set_state(state)
        except Exception:
            pass

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        with self._interrupt_lock:
            if self._in_interrupt:
                return
            self._in_interrupt = True
        try:
            was_speaking = bool(
                self._is_speaking
                or (hasattr(self, "voice_state") and (self.voice_state.is_speaking or self.voice_state.is_processing))
            )
            q = self.audio_in_queue
            drained = 0
            if q:
                while True:
                    try:
                        q.get_nowait()
                        drained += 1
                    except Exception:
                        break
                if drained:
                    print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
            if was_speaking or drained > 0:
                self._interrupted = True
            if hasattr(self, "voice_state"):
                self.voice_state.emergency_stop_speech()
            self.set_speaking(False)
            try:
                from core.ducking import restore_background_audio

                restore_background_audio()
            except Exception:
                pass
            # The words we were about to mouth are never going to be spoken now.
            self._visemes.reset()
            self._play_cursor = 0.0  # next batch starts a fresh timeline
            if self._turn_done_event:
                self._turn_done_event.clear()
            if hasattr(self, "ui") and self.ui:
                if hasattr(self.ui, "stream_user_text"):
                    self.ui.stream_user_text("")
                if hasattr(self.ui, "stream_reply_text"):
                    self.ui.stream_reply_text("")
            # Draining the queue stops the speakers, but a local engine is still
            # generating the rest of the answer and would keep filling it back up.
            # Live has nothing to cancel here — the server stops on its own.
            if self._engine is not None:
                try:
                    self._engine.interrupt()
                except Exception:
                    pass
            # Only log in activity log if speech or audio processing was actually interrupted
            if was_speaking or drained > 0:
                self.ui.write_log("SYS: Interrupted — speech stopped.")
        finally:
            with self._interrupt_lock:
                self._in_interrupt = False

    def global_toggle_mute(self) -> VoiceState:
        """
        System-wide Toggle Mute / Sleep (Ctrl + Alt + J or L+R Mouse Gesture).
        - From active (LISTENING, PROCESSING, SPEAKING) -> switches to MUTED.
        - From MUTED -> switches to SLEEPING.
        - From SLEEPING -> remains in SLEEPING (sticky; no state change).
        """
        prev_state = self.voice_state.state

        if prev_state == VoiceState.SLEEPING:
            # Sticky sleep: never change state when Ctrl+Alt+J is pressed in SLEEPING
            return VoiceState.SLEEPING

        was_speaking = bool(self._is_speaking or self.voice_state.is_speaking or self.voice_state.is_processing)
        new_state = self.voice_state.handle_mute_or_sleep()

        if new_state == VoiceState.DISABLED:
            if was_speaking:
                self.interrupt()
            self._drain_out_queue()
            self._sync_ui_state(True, "MUTED")
            self.ui.write_log("SYS: 🔇 Muted. Press Ctrl+Alt+J again for sleep, or Ctrl+Alt+K to listen.")
        elif new_state == VoiceState.SLEEPING:
            self._awake = False
            self._sync_ui_state(False, "SLEEPING")
            if self._wake_enabled:
                self._ensure_wake_detector_async()
                self.ui.write_log(f"SYS: 😴 Sleeping. Say {self._wake_words_label()} or press Ctrl+Alt+K to wake.")
            else:
                self.ui.write_log("SYS: 😴 Sleeping. Press Ctrl+Alt+K or tap WAKE NOW to wake.")
        return new_state

    def global_toggle_listening(self) -> VoiceState:
        """Backward-compatible alias for global_toggle_mute (Ctrl + Alt + J)."""
        return self.global_toggle_mute()

    def global_force_listening(self) -> VoiceState:
        """
        System-wide Force Listening (Ctrl + Alt + K).
        Immediately forces JARVIS into LISTENING regardless of whether it is
        MUTED, SLEEPING, SPEAKING, PROCESSING, or already LISTENING. Remains
        globally active even when JARVIS is asleep. Repeated presses while
        already in LISTENING keep it in LISTENING and never mute or sleep.
        """
        prev_state = self.voice_state.state
        was_speaking = bool(self._is_speaking or self.voice_state.is_speaking or self.voice_state.is_processing)
        if was_speaking:
            self.interrupt()
        else:
            self._interrupted = False

        self._awake = True
        new_state = self.voice_state.force_listening()
        # Explicitly sync UI even if state was already LISTENING (e.g. clearing stale flags)
        self._sync_ui_state(False, "LISTENING")
        self._record_activity()

        if prev_state == VoiceState.DISABLED:
            self.ui.write_log("SYS: 🎙 Force Listening (Ctrl+Alt+K) — unmuted and active.")
        elif prev_state == VoiceState.SLEEPING:
            self.ui.write_log("SYS: 🎙 Force Listening (Ctrl+Alt+K) — woken from sleep.")
        elif was_speaking:
            self.ui.write_log("SYS: 🎙 Force Listening (Ctrl+Alt+K) — interrupted speech, now listening.")
        else:
            self.ui.write_log("SYS: 🎙 Listening (Ctrl+Alt+K) — active.")
        return new_state

    def global_emergency_stop(self) -> VoiceState:
        """Alias bound to Ctrl + Alt + K -> forces LISTENING immediately."""
        return self.global_force_listening()

    def _on_voice_state_changed(self, old_state: VoiceState, new_state: VoiceState) -> None:
        if new_state == VoiceState.DISABLED:
            self._sync_ui_state(True, "MUTED")
        elif new_state == VoiceState.SLEEPING:
            self._awake = False
            self._sync_ui_state(False, "SLEEPING")
        elif new_state == VoiceState.LISTENING:
            self._awake = True
            self._sync_ui_state(False, "LISTENING")
        elif new_state == VoiceState.PROCESSING:
            self._awake = True
            self._sync_ui_state(False, "PROCESSING")
        elif new_state == VoiceState.SPEAKING:
            self._awake = True
            self._sync_ui_state(False, "SPEAKING")

        # Hybrid Bluetooth Audio Architecture transition triggers
        if hasattr(self, "audio_manager") and self.audio_manager.routing_mode == "auto_hybrid":
            if old_state == VoiceState.SLEEPING and new_state in (
                VoiceState.LISTENING,
                VoiceState.PROCESSING,
                VoiceState.SPEAKING,
            ):
                self._trigger_async_transition("conversation", reason=f"voice_state_{new_state.name.lower()}")
            elif old_state != VoiceState.SLEEPING and new_state == VoiceState.SLEEPING:
                self._trigger_async_transition("sleep", reason="voice_state_sleeping")

        # Notify remote dashboard of state transition if running
        dash = getattr(self, "_dashboard", None)
        loop = getattr(self, "_loop", None)
        if dash and loop and not loop.is_closed():
            dash_state = (
                "sleeping"
                if new_state == VoiceState.SLEEPING
                else "muted"
                if new_state == VoiceState.DISABLED
                else "active"
            )
            try:
                asyncio.run_coroutine_threadsafe(
                    dash.broadcast({"type": "status", "state": dash_state}),
                    loop,
                )
            except Exception:
                pass

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(turns={"role": "user", "parts": [{"text": text}]}, turn_complete=True),
            self._loop,
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        try:
            self.ui.write_log(f"ERR: {tool_name} — {short}")
        except Exception:
            pass
        self.speak(f"Sir, {tool_name} encountered an error. {short}")
