"""
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

        convo = "\n".join(log[-40:])
        prompt = (
            "Summarize this conversation in 1-2 sentences in English. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
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
                            f"{alert}\n\n"
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
                                f"{alert}\n\n"
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
