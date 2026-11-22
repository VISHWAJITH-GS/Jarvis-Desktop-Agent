"""
core/session_audio.py — Audio streaming, listening, playback, and vision mixin.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import time
import traceback
from typing import Any
import numpy as np
from google.genai import types

from core.runtime_env import (
    SEND_SAMPLE_RATE,
    RECEIVE_SAMPLE_RATE,
    CHUNK_SIZE,
    _pcm_level,
    _pcm_visemes,
    _is_repeat_chunk,
    _clean_transcript,
    _ReconnectSignal,
)
from core import confirm as confirm_gate

_REPEAT_MIN = 12
_VIS_HOP = 480  # 20 ms between frames, i.e. 50 shapes a second
_FIRST_SOUND = CHUNK_SIZE / RECEIVE_SAMPLE_RATE  # ~43 ms
_CURSOR_SLACK = 0.15


class LiveAudioMixin:
    """Audio streaming, microphone capture, and playback pipelines."""

    def _tail_active(self) -> bool:
        """True while the speakers may still be finishing our last sentence."""
        return time.monotonic() < self._tail_until

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
            # Gemini 3.x Live rejects the old realtime_input.media_chunks field
            # (what `media=...` maps to) and closes the socket with a 1007. Send
            # mic / phone PCM through the new `audio` field instead. Queue items
            # are {"data": <bytes>, "mime_type": <str>} from _listen_audio and
            # the phone relay.
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
            # ── Global Voice State & Mute Gate ───────────────────────────────
            if (hasattr(self, "voice_state") and self.voice_state.is_disabled) or getattr(self.ui, "muted", False):
                return

            # Drop frames while stream transition is in progress
            if hasattr(self, "audio_manager") and self.audio_manager.transition_state != "idle":
                return

            # ── Low-Power Sleep / Wake-Word Gate ─────────────────────────────
            # While SLEEPING (entered via 3-minute idle timeout, wake-word mode,
            # or manual sleep), mic audio NEVER leaves the machine to Gemini.
            # Frames go only to the local wake-word detector ("Jarvis"/"Hello")
            # on its own background thread; Ctrl+Alt+K also wakes globally.
            is_sleeping = (hasattr(self, "voice_state") and self.voice_state.is_sleeping) or (not self._awake)
            if is_sleeping:
                det = self._wake_detector
                if det is None and self._wake_enabled:
                    self._ensure_wake_detector_async()
                elif det is not None:
                    if getattr(det, "paused", False):
                        det.resume()
                    det.feed(indata)
                return
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking

            # ── Barge-in ─────────────────────────────────────────────────────
            # While JARVIS talks the mic is not streamed, but it is still worth
            # listening to locally: if the user starts speaking, cut the answer
            # short the way a person would stop when interrupted.
            #
            # The whole difficulty is echo — on speakers the mic hears JARVIS.
            # So the test is not "is the mic loud" but "is the mic louder than
            # the echo of what we are playing right now", sustained long enough
            # that a cough or a keystroke cannot trigger it.
            if jarvis_speaking:
                # Nothing is streamed while JARVIS talks.
                #
                # Interrupting by voice used to live here: `EchoGuard` can pick a
                # user out from under our own echo, and `core/echo.py` still does
                # that for the tail below. Re-enabling is small — classify each
                # block here and call interrupt() after `required_blocks` of
                # agreement — but it depends on the listener's room, so it stays
                # out until it can be tried on real hardware.
                return

            # ── Echo tail ────────────────────────────────────────────────────
            # The speaking flag has dropped but the speakers have not finished.
            # Sending this to the model is how an assistant hears itself, decides
            # it was addressed, and answers its own last sentence. The microphone
            # stays OPEN — the guard only drops blocks that are our own voice, so
            # replying the instant it stops still works.
            if self._tail_active():
                try:
                    if not self._echo.is_user_speech(indata, SEND_SAMPLE_RATE, _pcm_level(indata)):
                        return
                    self._tail_until = 0.0  # a real voice ends the tail early
                except Exception:
                    return
            elif self._echo._hist:
                self._echo.reset()

            # ── Push-to-talk ─────────────────────────────────────────────────
            # When it is on the microphone is closed by default and the chord
            # opens it, which is the whole point: nothing leaves the machine
            # unless you are holding the key.
            if self._ptt_enabled and not self._ptt_held:
                return

            if not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(self.out_queue.put_nowait, {"data": data, "mime_type": "audio/pcm"})
                # Feed the live mic level to the HUD so the waveform reacts to
                # the user's actual voice while listening. Purely cosmetic — any
                # failure here must never disturb the mic.
                try:
                    self.ui.set_audio_level(_pcm_level(indata))
                except Exception:
                    pass

        # Hand microphone capture to centralized AudioManager (supports hot-switching
        # and sample-rate resampling without duplicating streams or reconnecting Gemini).
        try:
            self.audio_manager.set_input_callback(callback)
            if not self.audio_manager.initialized:
                await asyncio.to_thread(self.audio_manager.initialize, callback)
            status = self.audio_manager.get_status()
            print(
                f"[JARVIS] 🎤 Mic active via AudioManager: {status['input_device']} "
                f"({status['input_sample_rate']} Hz, {status['input_status']})"
            )
            while True:
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            self.audio_manager.set_input_callback(None)
            raise
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _flush_pending_vision(self) -> bool:
        """Send a captured frame immediately after its tool response.

        The frame is already in hand by the time `screen_process` returns — the
        capture happened inside the tool call. The old flow still made the model
        speak a turn first and only injected the image on that turn's
        turn_complete, which cost a whole extra round trip AND produced two
        spoken answers: one improvised without the picture, then the real one.
        Sending it here means the model has the tool result and the image before
        it generates anything, so the user gets one answer, sooner.
        """
        if not (self._pending_vision and self.session):
            return False

        import base64 as _b64

        img_b, mime_t, question, angle = self._pending_vision
        self._pending_vision = None
        b64 = _b64.b64encode(img_b).decode("ascii")
        print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")

        # Label the source. Without it the image arrives carrying nothing but
        # the user's own sentence, and a screenshot of this app — which has a
        # face in the middle of it — got read as a photo of the user. What the
        # label *means* is explained once, in the generated [SELF] block.
        src = "[IMAGE SOURCE: SCREEN CAPTURE]"
        await self.session.send_client_content(
            turns={
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": mime_t, "data": b64}},
                    {"text": f"{src}\n\n{question}"},
                ],
            },
            turn_complete=True,
        )

        self._vision_busy = False
        return True

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():
                    # Anything at all from the server proves the return path is
                    # alive, so the stall clock stops here rather than in any of
                    # the branches below — a tool call, a transcript or a bare
                    # keepalive all count.
                    self._clear_stall()

                    # ── Session resumption ───────────────────────────────────
                    # The server sends this periodically. `resumable` goes false
                    # while a turn is mid-flight — replaying a handle from that
                    # moment is what the flag exists to prevent — so only
                    # resumable handles are kept. This is three lines and it is
                    # the entire fix for "every reconnect forgets everything".
                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        if getattr(_sru, "resumable", False) and getattr(_sru, "new_handle", None):
                            if self._resume_handle is None:
                                print("[JARVIS] 🔗 Session resumption armed")
                            self._resume_handle = _sru.new_handle

                    _goaway = getattr(response, "go_away", None)
                    if _goaway is not None:
                        tl = getattr(_goaway, "time_left", None)
                        print(
                            f"[JARVIS] 🔄 Server GoAway signal received (time_left={tl}) — performing clean reconnect"
                        )
                        raise _ReconnectSignal(keep_context=True)

                    if response.data:
                        if self._interrupted or (
                            hasattr(self, "voice_state")
                            and (self.voice_state.is_disabled or self.voice_state.is_sleeping)
                        ):
                            pass  # discard: interrupted, muted, or sleeping
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            cur_gen = self.voice_state.generation_id if hasattr(self, "voice_state") else 0
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait((cur_gen, _audio_data[_i : _i + _SLICE]))

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            # A turn that involves a tool call passes through
                            # several turn_completes, and the API re-sends the
                            # tail of the transcript across them. Comparing only
                            # against the previous chunk missed that — once
                            # out_buf had been flushed and emptied, the repeat
                            # sailed straight back in, which logged the answer
                            # twice AND made the avatar mouth it twice.
                            if txt and not _is_repeat_chunk(txt, out_buf):
                                out_buf.append(txt)
                                # Hand the words to the mouth as they arrive, so
                                # the avatar can form the consonants the audio
                                # alone cannot show. Pure string work — it adds
                                # nothing measurable to the response path.
                                self._visemes.feed_text(txt)
                                if hasattr(self, "ui") and self.ui:
                                    partial_reply = " ".join(out_buf).strip()
                                    if hasattr(self.ui, "stream_reply_text"):
                                        self.ui.stream_reply_text(partial_reply)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._record_activity()
                                try:
                                    from core.skill_learning.learning_manager import LearningManager
                                    lm = LearningManager.get_instance()
                                    if lm and lm.is_learning_active():
                                        lm.record_narration(txt)
                                except Exception:
                                    pass
                                if hasattr(self, "voice_state"):
                                    self.voice_state.start_processing()
                                if hasattr(self, "ui") and self.ui:
                                    partial_user = " ".join(in_buf).strip()
                                    if hasattr(self.ui, "stream_user_text"):
                                        self.ui.stream_user_text(partial_user)

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf = []
                                out_buf = []
                                self._visemes.reset()
                                if hasattr(self, "ui") and self.ui:
                                    if hasattr(self.ui, "stream_user_text"):
                                        self.ui.stream_user_text("")
                                    if hasattr(self.ui, "stream_reply_text"):
                                        self.ui.stream_reply_text("")
                                if hasattr(self, "voice_state") and not self.voice_state.is_disabled:
                                    self.voice_state.finish_speaking()
                                continue

                            full_in = " ".join(in_buf).strip()
                            in_buf = []
                            if full_in:
                                from core.speech_filter import should_drop_transcript
                                is_speaking = self._tail_active() or (
                                    hasattr(self, "voice_state") and self.voice_state.is_speaking
                                )
                                drop, drop_reason = should_drop_transcript(full_in, is_speaking_or_tail=is_speaking)
                                if drop:
                                    continue

                                self._last_out_logged = ""  # new exchange
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")

                                # Direct resolution for pending confirmations
                                try:
                                    intercepted = confirm_gate.intercept_confirmation(
                                        full_in,
                                        player=self.ui,
                                        speak=self.speak if hasattr(self, "speak") else None,
                                    )
                                    if intercepted is not None:
                                        print(f"[Confirm] Voice confirmation handled directly: {full_in}")
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
                                            await self.session.send_client_content(
                                                turns={"role": "user", "parts": [{"text": feedback_text}]},
                                                turn_complete=True,
                                            )
                                except Exception as c_err:
                                    print(f"[Confirm] Voice check error: {c_err}")

                                if self._dashboard:
                                    asyncio.create_task(
                                        self._dashboard.broadcast(
                                            {
                                                "type": "log",
                                                "speaker": "user",
                                                "text": full_in,
                                                "ts": datetime.now().isoformat(),
                                            }
                                        )
                                    )
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            full_out = confirm_gate.strip_model_confirmation_phrases(full_out)
                            # Second line of defence: even if a repeat slips
                            # into a *fresh* buffer after a flush, never log the
                            # same answer (or a tail of it) twice in a row.
                            if full_out and len(full_out) >= _REPEAT_MIN and self._last_out_logged:
                                if full_out in self._last_out_logged:
                                    full_out = ""
                            if full_out:
                                self._last_out_logged = full_out
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                if self._dashboard:
                                    asyncio.create_task(
                                        self._dashboard.broadcast(
                                            {
                                                "type": "log",
                                                "speaker": "jarvis",
                                                "text": full_out,
                                                "ts": datetime.now().isoformat(),
                                            }
                                        )
                                    )
                            out_buf = []

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(function_responses=fn_responses)
                        await self._flush_pending_vision()
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started (AudioManager)")

        if not self.audio_manager.initialized:
            await asyncio.to_thread(self.audio_manager.initialize)

        self._out_latency = self.audio_manager.output_latency
        st = self.audio_manager.get_status()
        print(
            f"[JARVIS] 🔊 Output active: {st['output_device']} ({st['output_sample_rate']} Hz, "
            f"latency {self._out_latency * 1000:.0f} ms)"
        )

        try:
            while True:
                try:
                    raw_item = await asyncio.wait_for(self.audio_in_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    if self._turn_done_event and self._turn_done_event.is_set() and self.audio_in_queue.empty():
                        self.set_speaking(False)
                        if hasattr(self, "voice_state") and not self.voice_state.is_disabled:
                            self.voice_state.finish_speaking()
                        self._turn_done_event.clear()
                    continue

                # Check generation ID and voice state to prevent stale audio playback
                cur_gen = self.voice_state.generation_id if hasattr(self, "voice_state") else 0
                is_dis = (
                    hasattr(self, "voice_state") and (self.voice_state.is_disabled or self.voice_state.is_sleeping)
                ) or getattr(self.ui, "muted", False)
                if isinstance(raw_item, tuple):
                    item_gen, chunk = raw_item
                    if item_gen != cur_gen or is_dis or self._interrupted:
                        continue
                else:
                    chunk = raw_item
                    if is_dis or self._interrupted:
                        continue

                self.set_speaking(True)
                if hasattr(self, "voice_state") and not is_dis:
                    self.voice_state.start_speaking()

                # Batch all immediately-available chunks of the current generation
                batch = bytearray(chunk)
                while len(batch) < 9600:  # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        next_item = self.audio_in_queue.get_nowait()
                        if isinstance(next_item, tuple):
                            next_gen, next_chunk = next_item
                            if next_gen == cur_gen and not self._interrupted and not is_dis:
                                batch.extend(next_chunk)
                        else:
                            if not self._interrupted and not is_dis:
                                batch.extend(next_item)
                    except asyncio.QueueEmpty:
                        break

                if self._interrupted or is_dis:
                    continue

                # Drive the HUD waveform and the avatar's mouth from JARVIS's
                # own voice. The batch is up to 200 ms long, so we hand over a
                # *schedule* of 20 ms viseme frames instead of a single averaged
                # level and let the HUD play it out in step with the audio.
                try:
                    pcm = np.frombuffer(bytes(batch), dtype=np.int16)
                    hop = _VIS_HOP / RECEIVE_SAMPLE_RATE
                    frames = _pcm_visemes(pcm, sr=RECEIVE_SAMPLE_RATE)
                    # When does this batch become audible? The stream was
                    # started at launch and its callback has been pulling
                    # silence ever since, so the first bytes of a reply reach
                    # the speaker about one callback period later — NOT one
                    # buffer later. `stream.latency` reports the buffer's
                    # capacity, which is how much can be queued ahead, and on
                    # Windows that is commonly 300-500 ms. Anchoring on it put
                    # the entire schedule a buffer late; that is the half second
                    # of lag, and it grew with whatever the device reported.
                    #
                    # After the anchor nothing needs measuring: the device
                    # consumes at exactly realtime, so each batch sounds one
                    # batch-duration after the one before it. The cursor is
                    # re-anchored only when it leaves the range physically
                    # possible — behind `now` means the device drained and this
                    # batch starts a fresh stretch of speech, while further
                    # ahead than the buffer can hold means it has drifted.
                    now = time.time()
                    horizon = self._out_latency + _CURSOR_SLACK
                    if not (now <= self._play_cursor <= now + horizon):
                        self._play_cursor = now + _FIRST_SOUND
                    at = self._play_cursor
                    # Advance by the batch's own duration whether or not it
                    # yielded frames, so a block too short to analyse cannot
                    # shift everything after it out of step with the audio.
                    self._play_cursor += pcm.size / RECEIVE_SAMPLE_RATE
                    if frames:
                        frames = self._visemes.frames(frames, hop)
                        self.ui.push_visemes(frames, hop, at)
                        # Barge-in needs to know what we are playing, not just
                        # how loud: the guard subtracts this from the microphone.
                        self._out_level = max(f[0] for f in frames)
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE, self._out_level)
                    else:
                        lvl = _pcm_level(pcm)
                        self.ui.set_audio_level(lvl)
                        self._out_level = lvl
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE, lvl)
                except Exception:
                    pass

                try:
                    await asyncio.to_thread(self.audio_manager.write_output, bytes(batch))
                except (RuntimeError, asyncio.CancelledError):
                    break  # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)

    # ── Morning briefing ────────────────────────────────────────────────────────
