"""
core/runtime_env.py — Runtime environment configuration, subprocess patching, and PCM helpers.
"""
from __future__ import annotations

import os
import platform as _platform
import re
import subprocess as _subprocess
import sys
from pathlib import Path
import numpy as np

# ── Windows subprocess creation flag patch ────────────────────────────────────
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen


def ensure_windows_subprocess_patch():
    """Ensure CREATE_NO_WINDOW is patched on Windows."""
    if _platform.system() == "Windows":
        _subprocess.Popen = _Popen


def ensure_utf8_console():
    """Reconfigure console output streams to UTF-8 with replace fallback."""
    for _stream in ("stdout", "stderr"):
        try:
            _s = getattr(sys, _stream, None)
            if _s is not None and hasattr(_s, "reconfigure"):
                _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"

LIVE_MODEL          = "models/gemini-3.1-flash-live-preview"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000 
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

_LEVEL_FLOOR = 20.0
_LEVEL_FULL  = 2000.0


def _pcm_level(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level for the HUD waveform."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    ratio = min(1.0, max(0.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR)))
    return float(ratio ** 0.65)


def _pcm_visemes(samples, sr: int = 24000):
    """Slice a chunk of int16 PCM into ~20 ms analysis windows and return a list of
    viseme frames, each a 4-tuple for HoloAvatar.push_viseme:
        (energy: float, openness: float, form: float, tilt: float)
    """
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return []
        win_size = int(sr * 0.020)
        hop_size = int(sr * 0.010)
        if win_size <= 0 or x.size < win_size:
            return []

        peak = float(np.max(np.abs(x)))
        if peak < 80.0:
            return []

        w = np.hanning(win_size).astype(np.float32)
        freqs = np.fft.rfftfreq(win_size, d=1.0 / sr)
        b_low  = (freqs >= 200.0)  & (freqs < 900.0)
        b_mid  = (freqs >= 900.0)  & (freqs < 2600.0)
        b_high = (freqs >= 2600.0) & (freqs < 6000.0)

        frames = []
        for start in range(0, x.size - win_size + 1, hop_size):
            slice_ = x[start:start + win_size] * w
            slice_rms = float(np.sqrt(np.mean(slice_ * slice_)))
            if slice_rms <= _LEVEL_FLOOR:
                energy = 0.0
            else:
                r = min(1.0, max(0.0, (slice_rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR)))
                energy = float(r ** 0.65)

            mag = np.abs(np.fft.rfft(slice_))
            e_low  = float(np.sum(mag[b_low]))
            e_mid  = float(np.sum(mag[b_mid]))
            e_high = float(np.sum(mag[b_high]))
            denom  = e_low + e_mid + e_high + 1e-4

            openness = (0.55 * (e_low / denom) + 0.45 * (e_mid / denom)) * min(1.0, energy * 1.6)
            form     = min(1.0, max(0.0, (e_mid / denom) * 1.5))
            tilt     = float(np.clip((e_high - e_low) / denom, -1.0, 1.0))

            frames.append((
                float(energy),
                float(min(1.0, max(0.0, openness))),
                float(form),
                float(tilt),
            ))
        return frames
    except Exception:
        return []


def _is_repeat_chunk(txt: str, buf: list) -> bool:
    if not buf:
        return False
    prev = buf[-1]
    if txt == prev:
        return True
    if len(txt) > 20 and prev.endswith(txt):
        return True
    return False

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()


class _ReconnectSignal(Exception):
    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _is_reconnect_signal(exc: BaseException) -> bool:
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_reconnect_signal(sub) for sub in exc.exceptions)
    return False


def _keep_context_of(exc: BaseException) -> bool:
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if _is_reconnect_signal(sub):
                return _keep_context_of(sub)
    return True
