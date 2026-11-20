"""
core/platform_hooks.py — Inversion-of-Control Hook Registry for Desktop & System Actions.

Enforces strict architectural boundaries: `core/` never imports from `actions/` or `gui/`.
Instead, actions register their platform-specific handlers with this registry.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

_HOOKS: Dict[str, Callable[..., Any]] = {}


def register_hook(name: str, handler: Callable[..., Any]) -> None:
    """Register a platform or domain handler under `name`."""
    _HOOKS[name] = handler


def unregister_hook(name: str) -> None:
    """Remove a registered handler."""
    _HOOKS.pop(name, None)


def get_hook(name: str) -> Optional[Callable[..., Any]]:
    """Retrieve a registered handler by name."""
    return _HOOKS.get(name)


def has_hook(name: str) -> bool:
    """Check if a handler is registered."""
    return name in _HOOKS


def call_hook(name: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """Call a registered handler safely. Returns `default` if not registered or if an error occurs."""
    handler = _HOOKS.get(name)
    if handler is None:
        return default
    try:
        return handler(*args, **kwargs)
    except Exception:
        return default


def get_active_window_title() -> str:
    """Return the foreground window title via standard Win32 API."""
    import platform
    if platform.system() != "Windows":
        return ""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return ""
        buff = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
        return buff.value
    except Exception:
        return ""

