"""
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
