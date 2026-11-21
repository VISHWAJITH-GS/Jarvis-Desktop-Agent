"""
Screen capture for JARVIS vision.

Provides `_capture_screen()` and its helpers (compression, screen-edge glow
notification). main.py grabs a display frame on demand and injects it into the
main Gemini Live session. All webcam/camera access has been removed.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

try:
    import mss
    import mss.tools

    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image

    _PIL = True
except ImportError:
    _PIL = False


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE = _base_dir()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


_IMG_MAX_W = 1280
_IMG_MAX_H = 720
_JPEG_Q = 82


def _compress(img_bytes: bytes, source_format: str = "PNG") -> tuple[bytes, str]:
    if not _PIL:
        return img_bytes, f"image/{source_format.lower()}"

    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Vision] ⚠️  Image compress failed: {e}")
        return img_bytes, f"image/{source_format.lower()}"


_glow_notifier = None


def set_screen_glow_callback(fn) -> None:
    """Register a callable(duration_ms) that displays a red glowing border around the screen edge."""
    global _glow_notifier
    _glow_notifier = fn


def trigger_screen_glow(duration_ms: int = 2500) -> None:
    """Trigger red glowing border along screen edges indicating JARVIS is seeing the display."""
    if _glow_notifier:
        try:
            _glow_notifier(duration_ms)
        except Exception as e:
            print(f"[Vision] ⚠️ Screen glow notify error: {e}")


def _capture_screen() -> tuple[bytes, str]:
    """Capture the primary display as a compressed JPEG image."""
    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")

    # Notify UI that screen is being observed / captured
    trigger_screen_glow(2500)

    with mss.mss() as sct:
        monitors = sct.monitors  # [0] = all combined, [1..n] = real screens
        target = monitors[1] if len(monitors) > 1 else monitors[0]
        shot = sct.grab(target)
        png = mss.tools.to_png(shot.rgb, shot.size)

    return _compress(png, "PNG")
