"""
Sample rates, formats and the conversions between them.

WHY THIS IS SHARED RATHER THAN REPEATED
    Three rates meet in this app and none of them agree. The microphone opens at
    16 kHz because that is what the Live API wants; the speaker opens at 24 kHz
    because that is what the Live API sends; and every speech engine has its own
    idea — Piper builds most voices at 22.05 kHz, Windows SAPI answers at
    whatever the voice was recorded at, macOS `say` gives you what you ask for,
    ElevenLabs will hand over 24 kHz if you ask precisely.

    Getting that wrong does not raise. It plays the sentence at the wrong speed
    and pitch — a 22.05 kHz clip through a 24 kHz stream is nine percent fast
    and slightly chipmunked, which sounds like a bad voice rather than a bug,
    and so gets reported as "the local voice is terrible".

    So the conversion lives in one place, and every provider states its own rate
    rather than assuming the app's.

NO NEW DEPENDENCY
    numpy is already required by the app. Resampling is linear interpolation,
    which for speech between neighbouring rates is inaudible against the
    synthesiser's own artefacts, and it costs no library, no build step and no
    platform-specific wheel.
"""

from __future__ import annotations

import io
import struct
import wave
from typing import Iterator

# What main.py opens its streams at — see SEND_SAMPLE_RATE / RECEIVE_SAMPLE_RATE.
# Named here so a provider never has to guess, and so changing one of them is a
# single edit rather than a hunt.
MIC_RATE = 16_000
SPEAKER_RATE = 24_000
SAMPLE_WIDTH = 2  # 16-bit signed little-endian
CHANNELS = 1


def to_float32(pcm: bytes):
    """16-bit PCM -> float32 in [-1, 1], which is what speech models take."""
    import numpy as np

    if not pcm:
        return np.zeros(0, dtype=np.float32)
    # An odd byte count means a chunk was cut mid-sample. Dropping the stray
    # byte is right; letting numpy raise would lose the whole utterance over one
    # byte at the end of a buffer.
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def to_pcm16(samples) -> bytes:
    """float32 in [-1, 1] -> 16-bit PCM, clipped rather than wrapped.

    Wrapping is what happens if you cast without clipping, and it turns a moment
    of loudness into a burst of white noise — much worse than the clipping it
    replaces."""
    import numpy as np

    arr = np.asarray(samples, dtype=np.float32)
    return (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """16-bit PCM from one rate to another. Returns the input when they match,
    so the common case costs a comparison."""
    if src_rate == dst_rate or not pcm:
        return pcm
    import numpy as np

    src = to_float32(pcm)
    if src.size == 0:
        return b""
    n_out = int(round(src.size * (dst_rate / float(src_rate))))
    if n_out <= 0:
        return b""
    # endpoint=False keeps the spacing uniform; with endpoint=True the last
    # sample lands exactly on the final input sample and every interval before
    # it is stretched, which drifts on long clips.
    idx = np.linspace(0, src.size, num=n_out, endpoint=False)
    return to_pcm16(np.interp(idx, np.arange(src.size), src))


def to_mono(pcm: bytes, channels: int) -> bytes:
    """Average interleaved channels down to one. Some system voices answer in
    stereo even when asked for mono, and playing that as mono halves the pitch."""
    if channels <= 1 or not pcm:
        return pcm

    arr = to_float32(pcm)
    usable = (arr.size // channels) * channels
    return to_pcm16(arr[:usable].reshape(-1, channels).mean(axis=1))


def wav_to_pcm(data: bytes, want_rate: int = SPEAKER_RATE) -> bytes:
    """A WAV file (however it was produced) -> mono 16-bit PCM at `want_rate`.

    This is how every system speech engine hands audio back: Windows SAPI writes
    a wave file, macOS `say` writes one, espeak-ng writes one to stdout. Parsed
    with the standard library, so there is nothing to install and nothing that
    behaves differently per platform.
    """
    if not data:
        return b""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            frames = w.readframes(w.getnframes())
    except Exception:
        # Not a WAV, or a truncated one. Some engines write a header whose
        # length field is wrong when they stream; salvage the payload rather
        # than losing the sentence.
        return _salvage(data, want_rate)

    if width != SAMPLE_WIDTH:
        frames = _requantise(frames, width)
    frames = to_mono(frames, channels)
    return resample(frames, rate, want_rate)


def _requantise(frames: bytes, width: int) -> bytes:
    """8-, 24- or 32-bit PCM down to 16-bit. Rare, but a voice that reports 32
    is not a reason to play silence."""
    import numpy as np

    if width == 1:  # unsigned 8-bit
        arr = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    elif width == 3:  # packed 24-bit
        raw = np.frombuffer(frames, dtype=np.uint8)
        usable = (raw.size // 3) * 3
        b = raw[:usable].reshape(-1, 3).astype(np.int32)
        val = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        val = np.where(val & 0x800000, val - 0x1000000, val)
        arr = val.astype(np.float32) / 8388608.0
    elif width == 4:
        arr = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        return frames
    return to_pcm16(arr)


def _salvage(data: bytes, want_rate: int) -> bytes:
    """Find the `data` chunk by hand when the header will not parse."""
    i = data.find(b"data")
    if i < 0 or len(data) < 28:
        return b""
    try:
        rate = struct.unpack_from("<I", data, data.find(b"fmt ") + 12)[0]
        channels = struct.unpack_from("<H", data, data.find(b"fmt ") + 10)[0]
    except Exception:
        rate, channels = want_rate, 1
    body = data[i + 8 :]
    return resample(to_mono(body, channels or 1), rate or want_rate, want_rate)


def chunk(pcm: bytes, ms: int = 50, rate: int = SPEAKER_RATE) -> Iterator[bytes]:
    """Split PCM into slices for playback.

    Fifty milliseconds because that is the size main.py already cuts Live's
    audio into, and it is what bounds how quickly an interrupt can stop the
    speakers: playback stops at the next slice boundary, so the slice length is
    the worst-case delay between the user talking over the assistant and the
    assistant going quiet.
    """
    step = max(2, int(rate * (ms / 1000.0)) * SAMPLE_WIDTH)
    step -= step % SAMPLE_WIDTH
    for i in range(0, len(pcm), step):
        piece = pcm[i : i + step]
        if piece:
            yield piece


def to_wav(pcm: bytes, rate: int = MIC_RATE, channels: int = CHANNELS) -> bytes:
    """Raw PCM wrapped in a WAV header — what a transcription API expects to be
    uploaded, and what makes a captured utterance playable for debugging."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def duration_s(pcm: bytes, rate: int = MIC_RATE) -> float:
    return len(pcm) / float(rate * SAMPLE_WIDTH * CHANNELS) if rate else 0.0


# ── Whisper-specific Preprocessing & Hallucination Suppression ───────────────
import re

# Known hallucination patterns produced by Whisper on silence/low audio
WHISPER_HALLUCINATION_PATTERNS = [
    r"^(?:thank\s+you(?:\s+very\s+much)?(?:\s+for\s+watching)?|thanks\s+for\s+watching)[\.\!\?]?$",
    r"^(?:please\s+)?(?:like\s+and\s+)?subscribe[\.\!\?]?$",
    r"^subtitles?\s+by.*$",
    r"^transcribed\s+by.*$",
    r"^(?:amara\.org|ted\.com|aljazeera\.net|cnn|bbc)[\.\!\?]?$",
    r"^[\.\,\!\?\s\-\–\—\…]+$",
    r"^(?:\[|\()(?:music|applause|laughter|silence|blank_audio|whispering|coughing|sighs?)(?:\]|\))[\.\!\?]?$",
]

_HALLUCINATION_REGEXES = [re.compile(p, re.IGNORECASE) for p in WHISPER_HALLUCINATION_PATTERNS]


def is_whisper_hallucination(text: str) -> bool:
    """Return True if text matches known Whisper silence/noise hallucination patterns."""
    t = (text or "").strip()
    if not t:
        return True
    # Strip surrounding quotes and whitespace
    t_clean = t.strip("\"' \t\r\n")
    if not t_clean:
        return True

    # Check pattern matches
    for rgx in _HALLUCINATION_REGEXES:
        if rgx.match(t_clean):
            return True

    # Repetitive hallucination check (e.g. "you you you you you" or "thank you thank you thank you")
    words = [w.lower() for w in re.findall(r"\b[\w']+\b", t_clean)]
    if len(words) >= 4:
        # Check single repeated word
        if len(set(words)) == 1:
            return True
        # Check 2-word phrase repeated 3+ times
        if len(words) >= 6 and len(words) % 2 == 0:
            pairs = [f"{words[i]} {words[i+1]}" for i in range(0, len(words), 2)]
            if len(set(pairs)) == 1:
                return True

    return False


def preprocess_for_whisper(
    pcm: bytes,
    sample_rate: int = MIC_RATE,
    min_duration_s: float = 0.30,
    pad_start_ms: int = 150,
    pad_end_ms: int = 250,
    normalize: bool = True,
    remove_dc: bool = True,
):
    """Clean, resample to 16 kHz mono float32, pad boundaries, and normalize for Whisper.

    Refuses audio shorter than min_duration_s (returns empty float32 array) to prevent
    hallucinations on brief transient noises.
    """
    import numpy as np

    if not pcm:
        return np.zeros(0, dtype=np.float32)

    # Duration check before processing
    dur = duration_s(pcm, sample_rate)
    if dur < min_duration_s:
        return np.zeros(0, dtype=np.float32)

    # 1. Resample to 16 kHz if necessary
    pcm_16k = resample(pcm, sample_rate, 16_000) if sample_rate != 16_000 else pcm
    arr = to_float32(pcm_16k)
    if arr.size == 0:
        return np.zeros(0, dtype=np.float32)

    # 2. DC offset removal (centers waveform around 0 to avoid bias)
    if remove_dc:
        arr = arr - np.mean(arr)

    # 3. Gentle peak normalization (boost quiet speech without blowing up silence)
    if normalize:
        peak = float(np.max(np.abs(arr)))
        # Only normalize if there is audible signal (peak > 0.02); do not amplify pure room noise
        if peak > 0.02:
            scale = 0.88 / peak
            # Clamp amplification factor to avoid extreme gain on borderline noise
            scale = min(scale, 20.0)
            arr = arr * scale

    # 4. Context padding at start and end
    # Push-to-talk / VAD clippings often truncate the first consonant or trailing syllable.
    # 150ms start padding and 250ms end padding give Whisper's 30s spectrogram window full context.
    start_samples = int(16_000 * (pad_start_ms / 1000.0)) if pad_start_ms > 0 else 0
    end_samples = int(16_000 * (pad_end_ms / 1000.0)) if pad_end_ms > 0 else 0

    if start_samples > 0 or end_samples > 0:
        arr = np.pad(arr, (start_samples, end_samples), mode="constant", constant_values=0.0)

    return arr.astype(np.float32)

