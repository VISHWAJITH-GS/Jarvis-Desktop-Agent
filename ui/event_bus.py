"""
ui/event_bus.py — Thread-safe Event Bus & Signal Dispatcher for Mark-LVI.

Provides decoupled event publishing and subscribing between worker threads
(tools, actions, plugins, audio loops) and the Qt Main Thread.
Follows AGENTS.md rules: Worker threads NEVER create or touch Qt widgets directly.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ui.event_bus")


class UIEventBus:
    """Thread-safe event pub-sub bus."""

    _instance: Optional[UIEventBus] = None
    _lock = threading.Lock()

    def __init__(self):
        self._listeners: Dict[str, List[Callable[[Any], None]]] = {}
        self._listeners_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> UIEventBus:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def subscribe(self, event_name: str, callback: Callable[[Any], None]) -> None:
        """Register a callback for an event."""
        with self._listeners_lock:
            if event_name not in self._listeners:
                self._listeners[event_name] = []
            if callback not in self._listeners[event_name]:
                self._listeners[event_name].append(callback)

    def unsubscribe(self, event_name: str, callback: Callable[[Any], None]) -> None:
        """Unregister a callback."""
        with self._listeners_lock:
            if event_name in self._listeners:
                self._listeners[event_name] = [cb for cb in self._listeners[event_name] if cb != callback]

    def emit(self, event_name: str, data: Any = None) -> None:
        """Publish an event to all registered listeners safely."""
        with self._listeners_lock:
            callbacks = list(self._listeners.get(event_name, []))

        for cb in callbacks:
            try:
                cb(data)
            except Exception as e:
                logger.error(f"Error in listener for {event_name}: {e}")

    # Standard event helpers
    def log(self, text: str) -> None:
        self.emit("log", text)

    def alert(self, message: str, title: str = "SYSTEM ALERT") -> None:
        self.emit("alert", {"message": message, "title": title})

    def content(self, title: str, text: str) -> None:
        self.emit("content", {"title": title, "text": text})


def get_ui_event_bus() -> UIEventBus:
    return UIEventBus.get_instance()
