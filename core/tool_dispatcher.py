"""
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
            key = args.get("key", "")
            value = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if (
                live.ui
                and not live.ui.muted
                and not (
                    hasattr(live, "voice_state") and (live.voice_state.is_disabled or live.voice_state.is_sleeping)
                )
            ):
                live.ui.set_state("LISTENING")
            return types.FunctionResponse(id=fc.id, name=name, response={"result": "ok", "silent": True})

        loop = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "recall_memory":
                result = search_memory(args.get("query", ""), limit=8)

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = (
                        (
                            "Things I can undo, most recent first:\n"
                            + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(items))
                        )
                        if items
                        else "I have not changed anything I can undo yet."
                    )
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
                    live._vision_busy = True
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
                topic = args.get("topic", "").strip()
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
                result = f"Going to sleep. Say {live._wake_words_label()} or press Ctrl+Alt+K whenever you need me."

            elif name == "shutdown_jarvis":

                def _run_confirmed_shutdown() -> str:
                    if live.ui:
                        live.ui.write_log("SYS: Shutdown confirmed — closing JARVIS.")

                    async def _do_shutdown():
                        await live._save_session_summary()
                        if live.session:
                            try:
                                await live.session.send_client_content(
                                    turns={
                                        "role": "user",
                                        "parts": [{"text": "Say a brief natural goodbye to the user."}],
                                    },
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

            elif name in ("whatsapp_send", "send_message") and confirm_gate.has_recent_execution(name, params=args, seconds=60.0):
                result = "Message has already been sent."
            elif live._action_registry and live._action_registry.has(name):
                if name == "file_processor" and not args.get("file_path") and live.ui and live.ui.current_file:
                    args["file_path"] = live.ui.current_file
                if (
                    name == "dev_agent"
                    and not args.get("folder")
                    and live.ui
                    and live.ui.current_file
                    and Path(live.ui.current_file).is_dir()
                    and str(args.get("mode") or "").lower() != "status"
                ):
                    args["folder"] = live.ui.current_file
                    args["mode"] = "improve"
                _ctx = {"player": live.ui, "speak": live.speak, "response": None, "session_memory": None}
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
                if name == "web_search" and r and not r.startswith("No results") and not r.startswith("Search failed"):
                    _mode = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    if live.ui:
                        live.ui.show_content(_label, outcome.unwrap(r))

            else:
                if live._plugin_registry and live._plugin_registry.has(name):
                    live._ensure_executor()
                    try:
                        r = await loop.run_in_executor(
                            None, lambda: live._plugin_registry.run(name, args, player=live.ui, session_memory=None)
                        )
                    except RuntimeError as re_err:
                        if "shutdown" in str(re_err).lower():
                            live._ensure_executor()
                            r = await loop.run_in_executor(
                                None, lambda: live._plugin_registry.run(name, args, player=live.ui, session_memory=None)
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
            if (
                live.ui
                and not live.ui.muted
                and not (
                    hasattr(live, "voice_state") and (live.voice_state.is_disabled or live.voice_state.is_sleeping)
                )
            ):
                live.ui.set_state("LISTENING")
        except Exception:
            pass

        _ts_done = time.strftime("%I:%M:%S %p")
        print(f"[{_ts_done}] [JARVIS] 📤 {name} ({(time.monotonic() - _t_tool) * 1000:.0f} ms) → {str(result)[:80]}")

        _sched = None
        if live._action_registry:
            _sched = live._action_registry.scheduling(name)
        if not _sched and live._plugin_registry:
            _sched = live._plugin_registry.scheduling(name)
        _extra = {"scheduling": _sched} if _sched else {}
        return types.FunctionResponse(id=fc.id, name=name, response={"result": result}, **_extra)
