"""
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
from actions.background_monitor import add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all
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
    # Startup crash recovery for Focus Mode (recovers hosts and DNS if previous process died mid-focus)
    try:
        from plugins import _focus_mode
        _focus_mode.restore_crash_state()
    except Exception as e:
        print(f"[JARVIS] Focus mode crash recovery note: {e}")

    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        platform_hooks.register_hook("request_session_reconnect", jarvis.request_reconnect)
        platform_hooks.register_hook("speak", jarvis.speak)
        try:
            asyncio.run(jarvis.run())
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            print("\n🔴 Shutting down...")
        except Exception as e:
            if not getattr(ui, "_is_closed", False):
                print(f"\n[JARVIS] Session stopped: {e}")
        finally:
            jarvis.shutdown_listeners()

    threading.Thread(target=runner, daemon=True, name="jarvis-voice-worker").start()
    try:
        ui.root.mainloop()
    finally:
        ui._is_closed = True


if __name__ == "__main__":
    main()
