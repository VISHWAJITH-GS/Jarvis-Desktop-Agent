"""
core/audio_devices.py — pick which microphone and which speakers JARVIS uses.

WHY
    Both audio streams in main.py were opened without a `device=` argument, so
    they always took whatever the operating system called "default". On a laptop
    with a built-in mic, a webcam mic and a headset that is a coin toss — and on
    Windows the default *moves on its own* the moment you plug a headset in.
    "JARVIS can't hear me" almost always means "JARVIS is listening to the
    monitor's microphone".

WHY NAMES, NOT INDICES
    sounddevice identifies devices by integer index, and those indices shift
    whenever a device appears or disappears. Storing index 3 means that after
    unplugging a USB interface the saved setting silently points at something
    else. We store the device *name* and resolve it to an index at open time.

WHY THIS IS CACHED
    `sd.query_devices()` talks to the host audio API and can take a few hundred
    milliseconds on a Windows machine with many endpoints. Mark LV learned this
    lesson the expensive way — a 2.1-second `openwakeword` import on the Qt
    thread made the settings drawer look like it was broken. So the list is
    fetched once on a background thread at startup and served from cache.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# The label shown for "let the OS decide", and the value stored in config for
# it. Empty string, so an untouched install and a deliberately-default install
# are the same thing — nothing changes for anyone who never opens the picker.
DEFAULT_LABEL = "System default"
DEFAULT_VALUE = ""

_cache: dict[str, list[str]] | None = None
_cache_lock = threading.Lock()

# Which host API each direction settled on, so resolve() opens the same endpoint
# the picker listed. Filled in by _query().
_chosen_api: dict = {"input": None, "output": None}


# ── Why the raw list is unusable, and what is filtered out ───────────────────
#
# `sd.query_devices()` returns one entry per (device × host API), not one per
# device. Measured on a normal Windows machine: 41 entries for what the Windows
# sound settings show as 4 microphones and 4 speakers. The same Realtek
# microphone appears four times — once each under MME, DirectSound, WASAPI and
# WDM-KS — and none of the four is labelled to say which is which.
#
# Handing that to a person is not a choice, it is a quiz. So the list is reduced
# the way the operating system's own settings panel does it:
#
#   1. ONE host API per direction — chosen by measurement, not by reasoning.
#      See the preference note below.
#   2. No pseudo-devices. "Microsoft Sound Mapper", "Primary Sound Driver",
#      ALSA's "default"/"sysdefault"/"dmix" are aliases for "whatever the OS
#      picks" — which is precisely the "System default" entry already at the top
#      of the list. Offering them again as if they were hardware is noise.
#   3. No zero-channel or unnamed entries (WASAPI reports one of each).
#   4. Deduplicated by name.
#
# Nothing is hidden that a person could actually want: the same hardware is
# still there, listed once, under the name their operating system uses for it.

# ── Preference order, which is only a starting point ─────────────────────────
#
# Two earlier versions of this file were wrong in the same way: they decided
# which host API to use by reasoning about it instead of measuring it.
#
#   v1 ranked APIs by how clean their device names were and picked WASAPI.
#      WASAPI in shared mode does not resample — the hardware runs at 48 kHz,
#      this app streams 16 kHz in and 24 kHz out, and every open failed with
#      "Invalid sample rate". The picker looked right and did nothing.
#
#   v2 added a rate check and picked DirectSound, which passes that check on
#      both sides. PortAudio's DirectSound *output* is a silent sink: the
#      stream opens, every write returns success in ~0 ms, and nothing is ever
#      heard. Same failure, one layer deeper.
#
# So this list is a preference, not a promise. Which API actually gets used is
# decided below by _usable() (open it for real) and _transport_works() (does
# audio actually move), per direction. On this platform that lands on
# DirectSound for the microphone and MME for the speakers — a split that no
# amount of reasoning would have produced.
_PREFERRED_APIS = {
    "Windows": {
        "input": ("directsound", "mme", "wasapi"),
        "output": ("mme", "wasapi"),  # DirectSound output is a silent sink on Win11!
    },
    "Darwin": {
        "input": ("core audio",),
        "output": ("core audio",),
    },
    "Linux": {
        "input": ("pulse", "pipewire", "jack", "alsa"),
        "output": ("pulse", "pipewire", "jack", "alsa"),
    },
}

# ── "It opens" is not "it works" ─────────────────────────────────────────────
#
# Opening a stream successfully proves nothing. Measured, writing 2.0 s of audio:
#
#     device=None (MME)                    2.02 s   consumed in real time
#     DirectSound, any output device       0.00 s   swallowed instantly
#
# No flag or capability field reports this. The only thing that separates a real
# sink from a fake one is whether it consumes audio at the rate audio is
# consumed at — so that is what gets measured, once per host API per direction,
# on the background thread at startup, using silence.
#
# Each direction is probed **the way main.py actually uses it**. That is not a
# detail: DirectSound input passes a callback stream and fails a blocking read,
# so an earlier version of this probe rejected a microphone that works perfectly
# in the app. Probe the mode you ship, not the mode that is easier to write.
_PROBE_SECONDS = {"output": 0.6, "input": 0.35}

# Cache: {(api_name_or_None, kind): bool}
_probe_results: dict = {}


def _transport_works(idx: int, kind: str, api_key) -> bool:
    """Does this host API actually move audio, or only pretend to?

    Probed once per API per direction and cached. Output writes silence, so the
    probe is inaudible; input reads and discards."""
    if api_key in _probe_results:
        return _probe_results[api_key]

    ok = False
    try:
        import sounddevice as sd
        rate = _RATES.get(kind, 16000)
        secs = _PROBE_SECONDS.get(kind, 0.5)

        # Each direction is probed the way main.py actually uses it. That is not
        # a detail: DirectSound input passes a callback stream and fails a
        # blocking read, so probing the wrong mode rejected a microphone that
        # works perfectly in the app.
        if kind == "output":
            # main.py writes with stream.write() — a real sink is rate-limited
            # by the hardware clock, a fake one swallows the buffer instantly.
            st = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16",
                                    blocksize=1024, device=idx)
            st.start()
            t0 = time.monotonic()
            st.write(bytes(int(rate * secs) * 2))   # silence — inaudible
            elapsed = time.monotonic() - t0
            st.stop(); st.close()
            ok = elapsed > secs * 0.5
            if not ok:
                print(f"[Audio] output: host API reports success but moves no "
                      f"audio ({elapsed*1000:.0f} ms for {secs*1000:.0f} ms) "
                      f"— skipping it")
        else:
            # main.py reads through a callback — count what arrives.
            frames = [0]

            def _cb(indata, n, *_a):
                frames[0] += n

            st = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                blocksize=1024, device=idx, callback=_cb)
            st.start()
            time.sleep(secs)
            st.stop(); st.close()
            ok = frames[0] > rate * secs * 0.3
            # If a strict frame threshold failed (e.g. Bluetooth device taking a moment to wake up),
            # but the stream opened without error and delivered frames or opened clean, accept it as fallback.
            if not ok and frames[0] > 0:
                ok = True
            elif not ok and _usable(idx, kind):
                ok = True
    except Exception as e:
        print(f"[Audio] {kind} transport probe failed: {e}")
        ok = False

    _probe_results[api_key] = ok
    return ok


def _display_name(name: str, devices) -> str:
    """MME truncates device names to 31 characters, so the API that actually
    carries the audio may not be the one that can spell. If another host API
    knows a longer name that starts with this one, show that instead — the user
    reads 'Realtek HD Audio 2nd output (Realtek(R) Audio)' while the stream runs
    on the endpoint called 'Realtek HD Audio 2nd output (Re'."""
    if len(name) < 30:
        return name
    best = name
    for dev in devices:
        other = (dev.get("name") or "").strip()
        if len(other) > len(best) and other.startswith(name):
            best = other
    return best

# The rates the app's Gemini pipeline uses. Hardware devices that cannot open
# natively at these exact rates (common for Bluetooth A2DP/HFP & WASAPI at
# 44100/48000 Hz) are negotiated via find_supported_rate() and resampled cleanly.
_RATES = {"input": 16000, "output": 24000}
_FALLBACK_RATES = {
    "input": (16000, 48000, 44100, 32000, 24000, 8000),
    "output": (24000, 48000, 44100, 32000, 16000),
}
_portaudio_lock = threading.RLock()
_active_streams_count = 0


def register_active_stream(delta: int) -> None:
    """Track open sounddevice streams so PortAudio is NEVER terminated mid-stream."""
    global _active_streams_count
    with _portaudio_lock:
        _active_streams_count = max(0, _active_streams_count + int(delta))


def resample_pcm_int16(samples, src_rate: int, dst_rate: int):
    """Fast, zero-dependency mono int16 PCM resampler using numpy linear interpolation.
    Returns input unchanged when src_rate == dst_rate."""
    import numpy as np

    arr = np.asarray(samples, dtype=np.int16)
    if arr.ndim > 1:
        arr = arr[:, 0]
    if src_rate == dst_rate or arr.size == 0 or src_rate <= 0 or dst_rate <= 0:
        return arr

    dst_len = max(1, int(round(arr.size * (float(dst_rate) / float(src_rate)))))
    if dst_len == arr.size:
        return arr

    src_x = np.linspace(0.0, 1.0, num=arr.size, endpoint=False, dtype=np.float64)
    dst_x = np.linspace(0.0, 1.0, num=dst_len, endpoint=False, dtype=np.float64)
    resampled = np.interp(dst_x, src_x, arr.astype(np.float64))
    return np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)


def find_supported_rate(idx: Optional[int], kind: str, preferred_rate: Optional[int] = None) -> Optional[int]:
    """Probe and return a working hardware sample rate for `idx` ('input' or 'output').

    Rejects Windows DirectSound output (known silent sink). Tries `preferred_rate`
    first, then the device's default_samplerate, then standard fallback rates.
    Returns None if the device cannot be opened at any supported rate.
    """
    try:
        import sounddevice as sd

        target = int(preferred_rate or _RATES.get(kind, 16000))

        # Reject DirectSound output (silent sink on Windows 11)
        if kind == "output" and idx is not None:
            try:
                apis = [a.get("name", "").lower() for a in sd.query_hostapis()]
                dev_info = sd.query_devices(idx)
                hostapi_id = dev_info.get("hostapi", -1)
                api_name = apis[hostapi_id] if 0 <= hostapi_id < len(apis) else ""
                if "directsound" in api_name:
                    return None
            except Exception:
                pass

        # Build ordered candidate rate list: target -> device default -> fallbacks
        candidates: list[int] = [target]
        try:
            dev_info = sd.query_devices(idx) if idx is not None else sd.query_devices(kind=kind)
            default_sr = int(round(float(dev_info.get("default_samplerate") or 0)))
            if default_sr > 0 and default_sr not in candidates:
                candidates.append(default_sr)
        except Exception:
            pass
        for r in _FALLBACK_RATES.get(kind, (48000, 44100, 16000)):
            if r not in candidates:
                candidates.append(r)

        for rate in candidates:
            st = None
            try:
                if kind == "input":
                    st = sd.InputStream(
                        samplerate=rate,
                        channels=1,
                        dtype="int16",
                        blocksize=1024,
                        device=idx,
                        callback=lambda *_a: None,
                    )
                else:
                    st = sd.RawOutputStream(
                        samplerate=rate,
                        channels=1,
                        dtype="int16",
                        blocksize=1024,
                        device=idx,
                    )
                st.start()
                return rate
            except Exception:
                continue
            finally:
                if st is not None:
                    try:
                        st.stop()
                        st.close()
                    except Exception:
                        pass
        return None
    except Exception:
        return None


def configure(input_rate: int, output_rate: int) -> None:
    """Tell this module the sample rates the audio streams will use, so the
    picker can rule out devices that cannot be opened at them.

    Drops any cached list: which devices are usable depends on the rate, so a
    list built under the old rates would be stale."""
    global _cache
    _RATES["input"]  = int(input_rate)
    _RATES["output"] = int(output_rate)
    with _cache_lock:
        _cache = None


def _usable(idx: int, kind: str) -> bool:
    """Can this device actually be opened at the target rate or a supported fallback rate?

    Rejects Windows DirectSound output (silent sink) and opens a real stream to
    guarantee the endpoint works (including Bluetooth devices running at 44.1/48 kHz)."""
    return find_supported_rate(idx, kind, _RATES.get(kind, 16000)) is not None

# Aliases for "the default device" and internal routing endpoints. Matched
# case-insensitively as substrings against the device name.
_PSEUDO_DEVICES = (
    "sound mapper",        # Windows MME
    "primary sound",       # Windows DirectSound ("Primary Sound Capture Driver")
    "sysdefault",          # ALSA
    "default",             # ALSA / PulseAudio alias
    "dmix", "dsnoop",      # ALSA software mixing plugins
    "surround",            # ALSA channel-layout permutations of one card
    "samplerate", "speexrate", "upmix", "vdownmix", "null",
)


def _is_pseudo(name: str) -> bool:
    low = name.lower()
    return any(tok in low for tok in _PSEUDO_DEVICES)


def _query() -> dict[str, list[str]]:
    """Return {'input': [names...], 'output': [names...]}. Never raises.

    Only real, selectable devices — see the note above."""
    out: dict[str, list[str]] = {"input": [], "output": []}
    try:
        import platform
        import sounddevice as sd

        devices = list(sd.query_devices())
        try:
            apis = [a.get("name", "") for a in sd.query_hostapis()]
        except Exception:
            apis = []

        pref_platform = _PREFERRED_APIS.get(platform.system(), {})

        def _collect(api_filter, kind) -> list[tuple[int, str]]:
            """(index, name) for named, non-pseudo devices on one side that can
            be opened at the rate that side runs at."""
            chan = "max_input_channels" if kind == "input" else "max_output_channels"
            found, seen = [], set()
            for idx, dev in enumerate(devices):
                name = (dev.get("name") or "").strip()
                if not name or _is_pseudo(name) or name in seen:
                    continue
                if dev.get(chan, 0) <= 0:
                    continue
                if api_filter is not None:
                    h_id = dev.get("hostapi", -1)
                    api = apis[h_id].lower() if 0 <= h_id < len(apis) else ""
                    if api_filter not in api:
                        continue
                if not _usable(idx, kind):
                    continue
                seen.add(name)
                found.append((idx, name))
            return found

        # Each direction picks its own host API. They are genuinely different
        # problems — on Windows the microphone works on DirectSound while the
        # speakers only work on MME — and a single global choice cannot be right
        # for both.
        for kind in ("input", "output"):
            pref_raw = pref_platform.get(kind, ()) if isinstance(pref_platform, dict) else pref_platform
            preferred_tuple: tuple = tuple(pref_raw) if isinstance(pref_raw, (tuple, list)) else ()
            for api_filter in list(preferred_tuple) + [None]:
                found = _collect(api_filter, kind)
                if not found:
                    continue
                # One probe per API per direction, cached, on this thread.
                if not _transport_works(found[0][0], kind, (api_filter, kind)):
                    continue
                _chosen_api[kind] = api_filter
                out[kind] = [_display_name(n, devices) for _i, n in found]
                break
            if out[kind]:
                print(f"[Audio] {kind}: using "
                      f"{_chosen_api[kind] or 'any host API'} "
                      f"({len(out[kind])} devices)")
        return out

    except Exception as e:
        print(f"[Audio] Device enumeration failed: {e}")
    return out


def prefetch() -> None:
    """Warm the cache on a background thread. Called once at startup so the
    settings drawer never pays for enumeration on the Qt thread."""
    def _work():
        global _cache
        result = _query()
        with _cache_lock:
            _cache = result
        print(f"[Audio] {len(result['input'])} input / "
              f"{len(result['output'])} output devices found")
    threading.Thread(target=_work, daemon=True, name="audio-devices").start()


def reset_portaudio_if_idle() -> bool:
    """Safely re-initialize PortAudio ONLY when ZERO sounddevice streams are open.

    Calling sd._terminate() while an InputStream or OutputStream is active causes
    deadlocks or native segfaults on Windows. This guard prevents that completely.
    """
    with _portaudio_lock:
        if _active_streams_count > 0:
            logger.debug(
                "[Audio] Skipping PortAudio reset while %d stream(s) are active",
                _active_streams_count,
            )
            return False
        try:
            import sounddevice as sd
            sd._terminate()
            sd._initialize()
            return True
        except Exception as e:
            logger.debug("[Audio] PortAudio reset notice: %s", e)
            return False


def invalidate_cache(reset_portaudio: bool = False) -> None:
    """Drop cached device queries so newly connected Bluetooth devices are discovered.

    PortAudio is ONLY reset when `reset_portaudio=True` AND no streams are active.
    """
    global _cache, _probe_results
    with _cache_lock:
        _cache = None
        _probe_results = {}
        _chosen_api["input"] = None
        _chosen_api["output"] = None
    with _bt_detection_lock:
        _bt_detection_cache.clear()
    if reset_portaudio:
        reset_portaudio_if_idle()


def list_devices(kind: str, refresh: bool = False) -> list[str]:
    """Device names for 'input' or 'output'. Falls back to a synchronous query
    if the prefetch has not landed yet — correctness over the cache."""
    global _cache
    if refresh:
        invalidate_cache(reset_portaudio=False)
    with _cache_lock:
        cached = _cache
    if cached is None:
        cached = _query()
        with _cache_lock:
            _cache = cached
    return list(cached.get(kind, []))


def get_input_devices(refresh: bool = False) -> list[str]:
    """Return all available input device names, starting with DEFAULT_LABEL."""
    return [DEFAULT_LABEL] + list_devices("input", refresh=refresh)


def get_output_devices(refresh: bool = False) -> list[str]:
    """Return all available output device names, starting with DEFAULT_LABEL."""
    return [DEFAULT_LABEL] + list_devices("output", refresh=refresh)


def get_device_capabilities(kind: str, refresh: bool = False) -> list[dict]:
    """Return structured device list with name, index, host_api, channels, default_samplerate, and bluetooth flag."""
    names = list_devices(kind, refresh=refresh)
    results: list[dict] = []
    try:
        import sounddevice as sd
        devices = list(sd.query_devices())
        apis = [a.get("name", "") for a in sd.query_hostapis()]
    except Exception:
        devices, apis = [], []

    for name in names:
        idx, rate = resolve_with_rate(name, kind)
        info = devices[idx] if (idx is not None and 0 <= idx < len(devices)) else {}
        h_idx = info.get("hostapi", -1)
        api_name = apis[h_idx] if 0 <= h_idx < len(apis) else "Unknown"
        results.append({
            "name": name,
            "index": idx,
            "host_api": api_name,
            "max_input_channels": int(info.get("max_input_channels", 0)),
            "max_output_channels": int(info.get("max_output_channels", 0)),
            "default_samplerate": int(round(float(info.get("default_samplerate") or 0))),
            "supported_samplerate": rate,
            "is_bluetooth": is_bluetooth_device(name),
            "usable": idx is not None and rate is not None,
        })
    return results


def match_device_name(query: str, kind: str, refresh: bool = False) -> Optional[str]:
    """Match a spoken/user device query (e.g. 'OPPO Enco Buds2', 'laptop microphone',
    'laptop speakers', 'Windows default') to an available audio device name.

    Returns DEFAULT_VALUE ('') for default requests, the exact matched device name
    when found, or None when no matching device is available.
    """
    raw = (query or "").strip()
    low = raw.lower()
    if not raw or any(k in low for k in ("system default", "windows default", "default audio", "default device")):
        return DEFAULT_VALUE

    available = list_devices(kind, refresh=refresh)
    if not available:
        return None

    # Handle natural phrases for built-in laptop mic / speakers
    if any(k in low for k in ("laptop", "built-in", "built in", "internal", "integrated", "pc speaker", "computer")):
        non_bt = [
            d for d in available
            if not is_bluetooth_device(d)
            and not any(ex in d.lower() for ex in EXCLUDED_INPUT_KEYWORDS)
        ]
        if non_bt:
            for pref in ("array", "realtek", "speaker", "microphone", "internal", "integrated"):
                for d in non_bt:
                    if pref in d.lower():
                        return d
            return non_bt[0]

    # Exact case-insensitive match
    for d in available:
        if d.lower() == low:
            return d

    # Folded exact or substring match (handles 'OPPO Enco Buds 2' vs 'OPPO Enco Buds2')
    q_fold = _fold(raw)
    for d in available:
        d_fold = _fold(d)
        if q_fold and (d_fold == q_fold or q_fold in d_fold or d_fold in q_fold):
            return d

    # Generic headphone / earbuds / bluetooth phrase
    if any(k in low for k in ("earphone", "earbud", "headphone", "headset", "bud", "bluetooth")):
        for d in available:
            dl = d.lower()
            if is_bluetooth_device(d) or any(w in dl for w in ("bud", "enco", "oppo", "headphone", "headset", "bluetooth")):
                return d

    # Token overlap fallback
    q_tokens = {tok for tok in low.replace("-", " ").split() if len(tok) >= 2}
    if q_tokens:
        best_dev = None
        best_hits = 0
        for d in available:
            dl = d.lower()
            hits = sum(1 for tok in q_tokens if tok in dl)
            if hits > best_hits:
                best_hits = hits
                best_dev = d
        if best_dev and best_hits >= max(1, len(q_tokens) // 2):
            return best_dev

    return None


def resolve_with_rate(name: str, kind: str) -> tuple[Optional[int], Optional[int]]:
    """Resolve a saved device name into (device_index, supported_sample_rate).

    Walks preferred host APIs in order, checks exact name -> prefix -> folded substring
    (handles MME 31-char truncation AND Bluetooth names like 'OPPO Enco Buds2'), and
    negotiates a working hardware sample rate (target rate first, then 48k/44.1k/etc.).
    """
    wanted = (name or "").strip()
    target_rate = _RATES.get(kind, 16000)
    if not wanted or wanted == DEFAULT_LABEL:
        rate = find_supported_rate(None, kind, target_rate)
        return None, rate or target_rate

    try:
        import platform
        import sounddevice as sd

        devices = list(sd.query_devices())
        try:
            apis = [a.get("name", "") for a in sd.query_hostapis()]
        except Exception:
            apis = []

        want_in = (kind == "input")
        chan_key = "max_input_channels" if want_in else "max_output_channels"

        def _candidates(api_filter):
            for idx, dev in enumerate(devices):
                if dev.get(chan_key, 0) <= 0:
                    continue
                if api_filter is not None:
                    h_id = dev.get("hostapi", -1)
                    api = apis[h_id].lower() if 0 <= h_id < len(apis) else ""
                    if api_filter not in api:
                        continue
                yield idx, (dev.get("name") or "").strip()

        list_devices(kind)
        chosen = _chosen_api.get(kind)
        pref_platform = _PREFERRED_APIS.get(platform.system(), {})
        pref_raw = pref_platform.get(kind, ()) if isinstance(pref_platform, dict) else pref_platform
        preferred: tuple = tuple(pref_raw) if isinstance(pref_raw, (tuple, list)) else ()
        orders = ([chosen] if chosen is not None else []) \
            + [a for a in preferred if a != chosen] \
            + [None]

        wanted_fold = _fold(wanted)

        for api_filter in orders:
            partial: Optional[tuple[int, int]] = None
            folded_hit: Optional[tuple[int, int]] = None
            for idx, dev_name in _candidates(api_filter):
                if dev_name == wanted:
                    rate = find_supported_rate(idx, kind, target_rate)
                    if rate is not None:
                        return idx, rate
                    continue
                if partial is None and (
                    dev_name.startswith(wanted[:24]) or wanted.startswith(dev_name[:24])
                ):
                    rate = find_supported_rate(idx, kind, target_rate)
                    if rate is not None:
                        partial = (idx, rate)
                        continue
                if folded_hit is None and wanted_fold:
                    dev_fold = _fold(dev_name)
                    if wanted_fold in dev_fold or dev_fold in wanted_fold:
                        rate = find_supported_rate(idx, kind, target_rate)
                        if rate is not None:
                            folded_hit = (idx, rate)
            if partial is not None:
                return partial
            if folded_hit is not None:
                return folded_hit

        logger.warning(
            "[Audio] Device '%s' (%s) could not be opened on any host API or fallback sample rate",
            wanted,
            kind,
        )
        return None, None
    except Exception as e:
        logger.warning("[Audio] resolve_with_rate(%s, %s) failed: %s", wanted, kind, e)
        return None, None


def resolve(name: str, kind: str):
    """Turn a saved device name into a sounddevice index, or None for system default."""
    wanted = (name or "").strip()
    if not wanted or wanted == DEFAULT_LABEL:
        return None
    idx, _rate = resolve_with_rate(wanted, kind)
    return idx


# ─────────────────────────────────────────────────────────────────────────────
#  The one question automation cannot answer: is it hearing YOU?
# ─────────────────────────────────────────────────────────────────────────────
#
# Everything above decides whether a device can be OPENED and whether it moves
# audio, and it does that with silence so it can run at startup without anyone
# noticing. That is not the same question as "is my microphone picking up my
# voice", and it never will be: a muted mic, a level slider at zero, a headset
# boom pointing at the ceiling and a privacy shutter all pass every automated
# probe in this file and deliver nothing but a flat line.
#
# Only a person can answer that, by speaking. These two functions are what a
# TEST button in the settings panel calls — measure what actually arrives, and
# play something the user can confirm they heard.
#
# This began life as `ses_testi.py`, a loose script in the project root that
# nobody would ever find. It is here because the moment somebody needs it is
# the moment they have opened AUDIO DEVICES wondering why JARVIS cannot hear
# them.

# Below this peak, nothing usable arrived; below the second, speech is present
# but so quiet that recognition will struggle. Both are int16 sample values,
# taken from the levels a normal speaking voice produces at arm's length.
_SILENT_PEAK = 100
_QUIET_PEAK = 1500


def measure_input(name: str, seconds: float = 3.0, on_level=None) -> dict:
    """Record from one microphone and report what actually arrived.

    -> {"ok", "peak", "level", "verdict"} — `level` is 0.0-1.0 for a meter,
    `verdict` a short English sentence for the panel. Never raises: a device
    that cannot be opened is a result, not an exception, because the user is
    standing in front of the panel waiting for an answer either way.

    `on_level(0.0-1.0)` is called as the audio arrives, roughly twenty times a
    second, so the caller can move a meter WHILE the user is speaking.

    THE METER IS NOT DECORATION.
        The first version recorded for three seconds and then printed a
        verdict. Reported as "I saw no sign of anything, no level, no
        movement" — which is exactly right: three seconds of a static label
        reading "Speak now" is indistinguishable from a test that has hung.
        A person needs to see the thing respond to their voice to believe it
        is listening, and that is the entire point of the button.
    """
    idx = resolve(name, "input")
    peak_seen = [0]
    try:
        import numpy as np
        import sounddevice as sd

        rate = _RATES.get("input", 16000)

        def _cb(indata, _frames, _t, _status):
            block = int(np.abs(np.frombuffer(indata, dtype="int16")).max())                 if len(indata) else 0
            if block > peak_seen[0]:
                peak_seen[0] = block
            if on_level is not None:
                try:
                    on_level(min(1.0, block / 12000.0))
                except Exception:
                    pass

        stream = sd.RawInputStream(samplerate=rate, channels=1, dtype="int16",
                                   blocksize=int(rate / 20), device=idx,
                                   callback=_cb)
        with stream:
            time.sleep(max(0.5, float(seconds)))
        peak = peak_seen[0]
    except Exception as e:
        return {"ok": False, "peak": 0, "level": 0.0,
                "verdict": f"This microphone could not be opened: {e}"}

    if peak < _SILENT_PEAK:
        verdict = ("Nothing arrived. The microphone is muted, its level is at "
                   "zero, or this is not the device you are speaking into.")
    elif peak < _QUIET_PEAK:
        verdict = ("Very quiet. Speech recognition will struggle — raise the "
                   "input level in the system sound settings.")
    else:
        verdict = "Working. Your voice is coming through clearly."

    return {"ok": peak >= _QUIET_PEAK, "peak": peak,
            "level": min(1.0, peak / 12000.0), "verdict": verdict}


_bt_detection_cache: dict[str, tuple[bool, float]] = {}
_bt_detection_lock = threading.Lock()
_BT_CACHE_TTL = 15.0  # seconds


EXCLUDED_INPUT_KEYWORDS = (
    "stereo mix",
    "loopback",
    "virtual",
    "vb-cable",
    "obs",
    "steam streaming",
)


def _detect_bluetooth_via_endpoint_metadata(name: str) -> bool | None:
    """Check endpoint bus / enumerator metadata via Windows pycaw using exact ID or name match."""
    if _platform_is() != "Windows" or not name:
        return None
    try:
        from pycaw.pycaw import AudioUtilities
        wanted = _fold(name)
        wanted_raw = name.strip()
        if not wanted and not wanted_raw:
            return None

        for dev in AudioUtilities.GetAllDevices():
            try:
                dev_id = str(getattr(dev, "id", "") or "").strip()
                friendly = getattr(dev, "FriendlyName", "") or ""
                friendly_folded = _fold(friendly)
            except Exception:
                continue

            # Exact endpoint-ID matching or exact friendly-name matching (no prefix matching)
            matches_id = bool(dev_id and (dev_id == wanted_raw or dev_id.upper() == wanted_raw.upper()))
            matches_name = bool(friendly_folded and friendly_folded == wanted)

            if matches_id or matches_name:
                props = getattr(dev, "properties", {})
                for _k, v in props.items():
                    if isinstance(v, str):
                        v_u = v.upper()
                        if any(b in v_u for b in ("BTHENUM", "BTHHFENUM", "BTH\\", "BTHLE\\", "MICROSOFT_BLUETOOTH", "BTHA2DPSRC")):
                            return True
                        if "HDAUDIO\\" in v_u or v_u == "HDAUDIO" or "USB\\" in v_u or v_u == "USB":
                            return False

                dev_id_u = dev_id.upper()
                if any(b in dev_id_u for b in ("BTHENUM", "BTHHFENUM", "BTH\\", "BTHLE\\")):
                    return True

                fn_low = friendly.lower()
                if "hands-free" in fn_low or "bluetooth" in fn_low:
                    return True
                return False
    except Exception as e:
        logger.debug("CoreAudio Bluetooth metadata check error: %s", e)
    return None


def is_bluetooth_device(name: str) -> bool:
    """
    Check if device name or endpoint refers to a Bluetooth device.
    Uses Windows endpoint metadata (pycaw/MMDevice enumerators and properties)
    with a short cache, falling back to name heuristics.
    """
    wanted = (name or "").strip()
    if not wanted or wanted == DEFAULT_LABEL:
        return False

    with _bt_detection_lock:
        now = time.monotonic()
        if wanted in _bt_detection_cache:
            res, ts = _bt_detection_cache[wanted]
            if (now - ts) < _BT_CACHE_TTL:
                return res

    # 1. Primary: Endpoint metadata from Windows CoreAudio
    meta_result = None
    try:
        meta_result = _detect_bluetooth_via_endpoint_metadata(wanted)
    except Exception:
        meta_result = None

    if meta_result is not None:
        with _bt_detection_lock:
            _bt_detection_cache[wanted] = (meta_result, time.monotonic())
        return meta_result

    # 2. Fallback: Keyword-based heuristics (safe against false positives)
    low = wanted.lower()
    # Explicit USB wired / USB dongle / 3.5mm wired without Bluetooth branding -> not Bluetooth
    if ("usb" in low or "wired" in low) and not ("bluetooth" in low or "bth" in low):
        res = False
    else:
        # High-confidence Bluetooth indicators (excluding generic 'wireless' or 'headset' which match USB peripherals)
        bt_keywords = (
            "bluetooth", "bth", "hands-free", "airpods",
            "freebuds", "galaxy buds", "pixel buds"
        )
        res = any(k in low for k in bt_keywords)

    with _bt_detection_lock:
        _bt_detection_cache[wanted] = (res, time.monotonic())
    return res


def is_output_bluetooth(output_name: str) -> bool:
    """Check if the specified output (or system default output if empty) is Bluetooth."""
    wanted = (output_name or "").strip()
    if wanted and wanted != DEFAULT_LABEL:
        return is_bluetooth_device(wanted)

    if _platform_is() == "Windows":
        try:
            from pycaw.pycaw import AudioUtilities
            spk = AudioUtilities.GetSpeakers()
            if spk is not None:
                fname = getattr(spk, "FriendlyName", "")
                if fname:
                    return is_bluetooth_device(fname)
        except Exception:
            pass
    return False


def get_current_default_endpoint(kind: str = "output") -> tuple[str | None, str | None]:
    """Return (endpoint_id, friendly_name) for current default Windows audio device."""
    if _platform_is() != "Windows":
        return (None, None)
    try:
        import comtypes
        comtypes.CoInitialize()
        try:
            from pycaw.pycaw import AudioUtilities
            if kind == "output":
                dev = AudioUtilities.GetSpeakers()
            else:
                dev = AudioUtilities.GetMicrophone()
            if dev is not None:
                ep_id = getattr(dev, "id", None)
                ep_fn = getattr(dev, "FriendlyName", None)
                if ep_fn:
                    return (ep_id, ep_fn)
                # Fall through to sounddevice display name while preserving ep_id
                try:
                    import sounddevice as sd
                    info = sd.query_devices(kind=kind)
                    if info and info.get("name"):
                        return (ep_id, _display_name(str(info["name"]).strip(), list(sd.query_devices())))
                except Exception:
                    pass
                return (ep_id, None)
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
    except Exception:
        pass
    # Fallback to sounddevice default device name when pycaw GetMicrophone/GetSpeakers returns None
    try:
        import sounddevice as sd
        info = sd.query_devices(kind=kind)
        if info and info.get("name"):
            return (None, _display_name(str(info["name"]).strip(), list(sd.query_devices())))
    except Exception:
        pass
    return (None, None)


def get_connected_endpoints_snapshot() -> set[str]:
    """Return set of ACTIVE (connected) endpoint IDs and friendly names.

    Filters out NotPresent / Unplugged / Disabled remembered Windows Bluetooth
    endpoints so connect/disconnect hotplug transitions are detected accurately.
    """
    snapshot: set[str] = set()
    if _platform_is() != "Windows":
        return snapshot
    try:
        import comtypes
        comtypes.CoInitialize()
        try:
            from pycaw.pycaw import AudioUtilities
            for dev in AudioUtilities.GetAllDevices():
                state = getattr(dev, "state", None)
                if state is not None and "active" not in str(state).lower():
                    continue
                d_id = getattr(dev, "id", None)
                if d_id:
                    snapshot.add(str(d_id))
                fn = getattr(dev, "FriendlyName", None)
                if fn:
                    snapshot.add(str(fn))
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
    except Exception:
        pass
    return snapshot


def set_default_endpoint(device_name_or_id: str, kind: str = "output") -> bool:
    """Set the Windows default audio endpoint (Console, Multimedia, Communications).

    Accepts an endpoint ID (e.g. '{0.0.0.00000000}.{...}') or a friendly device name.
    Degrades safely on non-Windows platforms.
    """
    if _platform_is() != "Windows":
        return False
    target = (device_name_or_id or "").strip()
    if not target or target.lower() == DEFAULT_LABEL.lower():
        return False

    try:
        import ctypes
        import comtypes
        import comtypes.client
        from comtypes import GUID, COMMETHOD, IUnknown
        from pycaw.pycaw import AudioUtilities

        class IPolicyConfig(IUnknown):
            _iid_ = GUID('{f8679f50-850a-41cf-9c72-430f290290c8}')
            _methods_ = [
                COMMETHOD([], ctypes.HRESULT, 'GetMixFormat',
                          (['in'], ctypes.c_wchar_p, 'pwszDeviceId'),
                          (['out'], ctypes.c_void_p, 'ppFormat')),
                COMMETHOD([], ctypes.HRESULT, 'GetDeviceFormat'),
                COMMETHOD([], ctypes.HRESULT, 'ResetDeviceFormat'),
                COMMETHOD([], ctypes.HRESULT, 'SetDeviceFormat'),
                COMMETHOD([], ctypes.HRESULT, 'GetProcessingPeriod'),
                COMMETHOD([], ctypes.HRESULT, 'SetProcessingPeriod'),
                COMMETHOD([], ctypes.HRESULT, 'GetShareMode'),
                COMMETHOD([], ctypes.HRESULT, 'SetShareMode'),
                COMMETHOD([], ctypes.HRESULT, 'GetPropertyValue'),
                COMMETHOD([], ctypes.HRESULT, 'SetPropertyValue'),
                COMMETHOD([], ctypes.HRESULT, 'SetDefaultEndpoint',
                          (['in'], ctypes.c_wchar_p, 'pwszDeviceId'),
                          (['in'], ctypes.c_uint, 'role')),
                COMMETHOD([], ctypes.HRESULT, 'SetEndpointVisibility'),
            ]

        comtypes.CoInitialize()
        try:
            target_id = None
            if target.startswith("{") and "}" in target:
                target_id = target
            else:
                target_low = _fold(target)
                for dev in AudioUtilities.GetAllDevices():
                    dev_fn = _fold(getattr(dev, "FriendlyName", "") or "")
                    if target_low in dev_fn or dev_fn in target_low:
                        target_id = getattr(dev, "id", None)
                        break

            if not target_id:
                return False

            CLSID_PolicyConfigClient = GUID('{870af99c-171d-4f9e-af0d-e63df40c2bc9}')
            policy = comtypes.client.CreateObject(CLSID_PolicyConfigClient, interface=IPolicyConfig)
            # 0: eConsole, 1: eMultimedia, 2: eCommunications
            for role in (0, 1, 2):
                policy.SetDefaultEndpoint(target_id, role)
            invalidate_cache()
            return True
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
    except Exception as e:
        logger.debug("Failed to set default endpoint via IPolicyConfig: %s", e)
        return False


def _is_wasapi_endpoint(name: str) -> bool:
    """Check if an input device name or endpoint corresponds to WASAPI."""
    if not name:
        return False
    low = name.lower()
    if "wasapi" in low:
        return True
    try:
        import sounddevice as sd
        apis = [a.get("name", "").lower() for a in sd.query_hostapis()]
        target = _fold(name)
        for d in sd.query_devices():
            if d.get("max_input_channels", 0) > 0:
                d_name = d.get("name") or ""
                if d_name == name or _fold(d_name) == target:
                    h_idx = d.get("hostapi", -1)
                    if 0 <= h_idx < len(apis) and "wasapi" in apis[h_idx]:
                        return True
    except Exception:
        pass
    return False


def select_best_input(
    requested_input: str,
    output_name: str,
    input_mode: str = "auto",
    allow_bluetooth_mic: bool = False,
) -> dict:
    """
    Select the optimal input device to avoid Bluetooth HFP call mode conflicts.
    Excludes loopback and virtual inputs, and prefers WASAPI capture endpoints.
    """
    wanted = (requested_input or "").strip()
    is_explicit = bool(wanted and wanted != DEFAULT_LABEL)
    out_is_bt = is_output_bluetooth(output_name)

    # 1. If output is NOT Bluetooth, no A2DP/HFP profile conflict exists
    if not out_is_bt:
        return {
            "device": wanted if is_explicit else None,
            "reason": f"Output is not Bluetooth; using {'requested device ' + wanted if is_explicit else 'system default input'}",
            "is_bluetooth": is_bluetooth_device(wanted) if is_explicit else False,
            "quality_drop_warning": False,
        }

    # 2. Output IS Bluetooth:
    # If user explicitly picked a specific non-Bluetooth device, respect it!
    if is_explicit and not is_bluetooth_device(wanted):
        return {
            "device": wanted,
            "reason": f"Explicit non-Bluetooth user selection '{wanted}' respected",
            "is_bluetooth": False,
            "quality_drop_warning": False,
        }

    # If user explicitly opted in to allow Bluetooth mic:
    if allow_bluetooth_mic:
        dev_name = wanted if is_explicit else None
        return {
            "device": dev_name,
            "reason": f"Bluetooth mic {'(' + wanted + ')' if is_explicit else '(system default)'} allowed by user setting",
            "is_bluetooth": True,
            "quality_drop_warning": True,
        }

    # If input mode is manual and user picked a specific device (even BT):
    if input_mode == "manual" and is_explicit:
        is_bt = is_bluetooth_device(wanted)
        return {
            "device": wanted,
            "reason": f"Manual mode: user selection '{wanted}' used (Bluetooth={is_bt})",
            "is_bluetooth": is_bt,
            "quality_drop_warning": is_bt,
        }

    # In 'auto' mode: avoid Bluetooth microphone!
    available_inputs = list_devices("input")

    def _is_virtual_or_loopback(dev_name: str) -> bool:
        low = dev_name.lower()
        return any(k in low for k in EXCLUDED_INPUT_KEYWORDS)

    # Exclude loopback / virtual endpoints (Stereo Mix, Loopback, Virtual, VB-Cable, OBS, Steam Streaming)
    candidates = [
        dev for dev in available_inputs
        if not is_bluetooth_device(dev) and not _is_virtual_or_loopback(dev)
    ]

    # If no non-virtual, non-BT mic exists, fall back to any non-BT input
    if not candidates:
        candidates = [dev for dev in available_inputs if not is_bluetooth_device(dev)]

    if candidates:
        def _rank(dev_name: str) -> tuple[int, int, str]:
            low = dev_name.lower()
            # Prefer WASAPI capture endpoints (0 = WASAPI, 1 = other)
            wasapi_rank = 0 if _is_wasapi_endpoint(dev_name) else 1

            # Built-in laptop mic array first, then built-in, then USB, then other
            if "array" in low:
                hw_rank = 0
            elif any(k in low for k in ("built-in", "internal", "integrated", "realtek", "conexant", "intel")):
                hw_rank = 1
            elif "usb" in low:
                hw_rank = 2
            else:
                hw_rank = 3

            return (wasapi_rank, hw_rank, dev_name)

        candidates.sort(key=_rank)
        chosen = candidates[0]
        return {
            "device": chosen,
            "reason": f"Auto-selected non-Bluetooth input '{chosen}' to preserve Bluetooth A2DP stereo playback",
            "is_bluetooth": False,
            "quality_drop_warning": False,
        }

    # No non-Bluetooth mic found anywhere
    fallback_dev = wanted if is_explicit else None
    return {
        "device": fallback_dev,
        "reason": f"No non-Bluetooth mic found; falling back to {'headset mic ' + wanted if is_explicit else 'system default'}",
        "is_bluetooth": True,
        "quality_drop_warning": True,
    }


def get_device_status(name: str, kind: str) -> dict:
    """
    Get detailed device status including connection state and Bluetooth profile info.
    Returns:
        {
            "name": str,
            "connected": bool,
            "is_bluetooth": bool,
            "profile": str,  # 'A2DP Stereo', 'HFP Hands-Free', 'Built-in Audio', etc.
            "warning": str | None,
        }
    """
    wanted = (name or "").strip()
    if not wanted or wanted == DEFAULT_LABEL:
        return {
            "name": DEFAULT_LABEL,
            "connected": True,
            "is_bluetooth": False,
            "profile": "System Default",
            "warning": None,
        }

    is_bt = is_bluetooth_device(wanted)
    idx = resolve(wanted, kind)
    connected = (idx is not None)

    warning = None
    profile = "Built-in / USB"
    if is_bt:
        if kind == "input":
            profile = "Bluetooth HFP (Hands-Free Mono)"
            warning = (
                "Bluetooth microphone selected: Windows switches earphones to Hands-Free (HFP) mode. "
                "This reduces playback sound quality and may muffle background apps (like Spotify). "
                "Recommended: Use Laptop Microphone for high-fidelity A2DP stereo playback."
            )
        else:
            profile = "Bluetooth A2DP (Stereo Playback)"
    else:
        if kind == "input":
            profile = "Built-in Microphone Array"
        else:
            profile = "Built-in Speakers"

    return {
        "name": wanted,
        "connected": connected,
        "is_bluetooth": is_bt,
        "profile": profile,
        "warning": warning,
    }


def find_builtin_microphone(configured_name: Optional[str] = None) -> Optional[str]:
    """
    Find the laptop's built-in microphone (e.g. ASUS Realtek Microphone Array).
    If configured_name is provided and available, returns it.
    Otherwise queries available inputs, filtering out virtual and Bluetooth endpoints,
    and returns the highest-ranked built-in microphone.
    """
    if configured_name:
        matched = match_device_name(configured_name, "input", refresh=False)
        if matched and not is_bluetooth_device(matched):
            return matched

    sel = select_best_input(requested_input="", output_name="bluetooth", input_mode="auto", allow_bluetooth_mic=False)
    dev = sel.get("device")
    if dev and not sel.get("is_bluetooth", False):
        return dev

    # Direct search across available inputs
    available = list_devices("input")
    candidates = [
        d for d in available
        if not is_bluetooth_device(d) and not any(k in d.lower() for k in EXCLUDED_INPUT_KEYWORDS)
    ]
    if candidates:
        for pref in ("array", "realtek", "conexant", "intel", "internal", "integrated", "microphone"):
            for d in candidates:
                if pref in d.lower():
                    return d
        return candidates[0]
    return None


def find_bluetooth_microphone(preferred: Optional[str] = None) -> Optional[str]:
    """
    Find the connected Bluetooth headset microphone (e.g. 'Headset (OPPO Enco Buds2)').
    If preferred is given and valid/connected, returns it.
    Otherwise searches input devices for any active Bluetooth endpoint.
    """
    available = list_devices("input")
    if preferred:
        matched = match_device_name(preferred, "input", refresh=False)
        if matched and is_bluetooth_device(matched):
            return matched

    # Look for devices with bluetooth indicators or keywords
    bt_inputs = [d for d in available if is_bluetooth_device(d)]
    if bt_inputs:
        for pref in ("oppo", "buds", "headset", "hands-free", "bluetooth"):
            for d in bt_inputs:
                if pref in d.lower():
                    return d
        return bt_inputs[0]
    return None


def find_bluetooth_output(preferred: Optional[str] = None) -> Optional[str]:
    """
    Find the connected Bluetooth stereo headphone endpoint (e.g. 'Headphones (OPPO Enco Buds2)').
    """
    available = list_devices("output")
    if preferred:
        matched = match_device_name(preferred, "output", refresh=False)
        if matched and is_bluetooth_device(matched):
            return matched

    bt_outputs = [d for d in available if is_bluetooth_device(d)]
    if bt_outputs:
        for pref in ("oppo", "buds", "headphones", "stereo", "bluetooth"):
            for d in bt_outputs:
                if pref in d.lower():
                    return d
        return bt_outputs[0]
    return None


def detect_a2dp_status(output_name: str, input_stream_open: bool = False) -> dict:
    """
    Check if the Bluetooth output device is currently in high-quality A2DP stereo mode.
    On Windows:
    - If a Bluetooth capture stream is open, Windows locks the Bluetooth connection in HFP call mode.
    - If no Bluetooth capture stream is open and the device is connected, Windows restores A2DP stereo.
    """
    is_bt = is_bluetooth_device(output_name)
    if not is_bt:
        return {
            "is_bluetooth": False,
            "a2dp_active": True,
            "profile": "Built-in / Wired Audio",
            "message": "Output is not Bluetooth; standard audio playback active.",
        }

    if input_stream_open:
        return {
            "is_bluetooth": True,
            "a2dp_active": False,
            "profile": "Bluetooth HFP (Mono Call Mode)",
            "message": "Bluetooth microphone is active; playback quality reduced to HFP call mode.",
        }

    idx = resolve(output_name, "output")
    if idx is None:
        return {
            "is_bluetooth": True,
            "a2dp_active": False,
            "profile": "Disconnected",
            "message": "Bluetooth device disconnected.",
        }

    try:
        import sounddevice as sd
        info = sd.query_devices(idx)
        channels = int(info.get("max_output_channels", 2))
        rate = float(info.get("default_samplerate", 44100.0))
        is_stereo = (channels >= 2 and rate >= 44100.0)
        return {
            "is_bluetooth": True,
            "a2dp_active": is_stereo,
            "profile": "Bluetooth A2DP (Stereo Playback)" if is_stereo else "Bluetooth Hands-Free (HFP)",
            "sample_rate": rate,
            "channels": channels,
            "message": "A2DP stereo music playback active." if is_stereo else "HFP call mode active.",
        }
    except Exception:
        return {
            "is_bluetooth": True,
            "a2dp_active": not input_stream_open,
            "profile": "Bluetooth A2DP (Stereo Playback)" if not input_stream_open else "Bluetooth HFP",
            "message": "Stereo music playback restored." if not input_stream_open else "HFP call mode.",
        }


def play_tone(name: str, seconds: float = 0.6) -> dict:
    """Play a short test sound through the chosen output device.

    Uses RawOutputStream to ensure the audio is actually routed to the specified endpoint,
    not the system default. Checks that audio was consumed in real time, rejecting silent sinks.
    """
    idx = resolve(name, "output")
    if name and name != DEFAULT_LABEL and idx is None:
        return {
            "ok": False,
            "verdict": f"Output device '{name}' is disconnected or unavailable. Please connect it or choose another output.",
        }

    try:
        import numpy as np
        import sounddevice as sd

        rate = _RATES.get("output", 24000)
        t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
        # Distinct pleasant two-tone chime (550 Hz -> 880 Hz)
        half = len(t) // 2
        tone1 = np.sin(2 * np.pi * 550 * t[:half]) * 8000
        tone2 = np.sin(2 * np.pi * 880 * t[half:]) * 8000
        tone = np.concatenate([tone1, tone2]).astype(np.int16)

        st = sd.RawOutputStream(
            samplerate=rate,
            channels=1,
            dtype="int16",
            device=idx,
            blocksize=1024,
        )
        st.start()
        t0 = time.monotonic()
        st.write(tone.tobytes())
        elapsed = time.monotonic() - t0
        st.stop()
        st.close()

        if elapsed < seconds * 0.3:
            return {
                "ok": False,
                "verdict": "Output device swallowed audio without playing (silent sink).",
            }

        dev_label = name if (name and name != DEFAULT_LABEL) else "System Default"
        return {
            "ok": True,
            "verdict": f"Chime played through '{dev_label}' ({elapsed:.2f}s). Did you hear it?",
        }
    except Exception as e:
        return {"ok": False, "verdict": f"This output could not be played: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
#  Output volume, so the panel can set it where the device is chosen
# ─────────────────────────────────────────────────────────────────────────────
#
# Choosing "Realtek(R) Audio" in this panel and then finding it far too quiet
# means going to the system sound settings, finding the same endpoint again and
# moving a slider there — reported as exactly that annoyance. The volume
# belongs beside the device it applies to.
#
# These delegate to actions/computer_settings, which already owns the
# per-platform volume plumbing and had a real bug in it (see
# _win_endpoint_volume there). One implementation, not two.

def _endpoint_for(name: str):
    """The Windows volume interface for ONE named output, or None.

    WHY THIS IS NOT JUST THE DEFAULT DEVICE
        The first version set the system's default endpoint, so moving the
        slider in this panel changed the volume of whichever output Windows
        happened to consider default — not the one chosen two lines above it.

        That is wrong in a way that matters to how people actually use this:
        somebody puts the assistant on their speakers and keeps everything else
        in their headphones, and a slider labelled with one device silently
        moving the other is worse than having no slider.

        It also explains a complaint that looked separate. The selected output
        here was sitting at 10% while the default endpoint was at 100% — so the
        assistant was almost inaudible, and the slider "fixed" it by turning up
        something the user could not hear either.

    Matching is by the name Windows itself reports, which is the same string
    sounddevice lists, folded so case and punctuation cannot separate them.
    """
    if _platform_is() != "Windows" or not name:
        return None
    try:
        from pycaw.pycaw import AudioUtilities
        wanted = _fold(name)
        if not wanted:
            return None

        # Collect first, decide after. An earlier version returned the first
        # candidate that matched on a PREFIX, which meant a loosely-similar
        # name could win over the exact one simply by coming first in the
        # enumeration — and the slider then moved a device the user had not
        # chosen. Exact wins, always; the prefix pass exists only because MME
        # truncates long names to 31 characters.
        exact, partial = None, None
        for dev in AudioUtilities.GetAllDevices():
            # ONLY ACTIVE ENDPOINTS, AND THIS IS NOT A TIDINESS FILTER.
            #
            # Windows keeps a record of hardware that is no longer here, and it
            # keeps the SAME FRIENDLY NAME. Measured on this machine, two
            # devices answered to "Hoparlör (Realtek(R) Audio)": one
            # NotPresent sitting at 100%, and the real one, Active, at 10%.
            # Matching by name alone returned whichever came first — so the
            # panel read 100% for a speaker that was actually at 10%, and
            # setting it moved a device that does not exist.
            try:
                if str(getattr(dev, "state", "")) != "AudioDeviceState.Active":
                    continue
            except Exception:
                continue
            try:
                # Reading this property activates a COM interface and raises
                # for devices that are unplugged or disabled — normal, and not
                # worth a line in the log for each one.
                endpoint = dev.EndpointVolume
            except Exception:
                continue
            if endpoint is None:
                continue
            try:
                friendly = _fold(getattr(dev, "FriendlyName", "") or "")
            except Exception:
                continue
            if not friendly:
                continue
            if friendly == wanted:
                exact = endpoint
                break
            if partial is None and (friendly.startswith(wanted[:24])
                                    or wanted.startswith(friendly[:24])):
                partial = endpoint
        return exact or partial
    except Exception as e:
        print(f"[Audio] per-device volume unavailable: {e}")
    return None


def _platform_is() -> str:
    import platform
    return platform.system()


def _fold(text: str) -> str:
    """Same reduction core/appidentity uses for application names — imported
    lazily so this module keeps working if that one is ever absent."""
    try:
        from core.appidentity import fold
        return fold(text)
    except Exception:
        return "".join(c for c in str(text or "").lower() if c.isalnum())


def get_output_volume(name: str = "") -> Optional[int]:
    """Volume 0-100 of ONE output, or of the system default when unnamed.

    None where the platform will not report it — macOS and Linux have no
    per-device volume through this plumbing, so a name there falls back to the
    master level rather than silently answering about the wrong device.
    """
    endpoint = _endpoint_for(name)
    if endpoint is not None:
        try:
            return max(0, min(100, round(
                endpoint.GetMasterVolumeLevelScalar() * 100)))
        except Exception as e:
            print(f"[Audio] could not read '{name}' volume: {e}")
    try:
        from actions.computer_settings import volume_get
        return volume_get()
    except Exception as e:
        print(f"[Audio] could not read the output volume: {e}")
        return None


def set_output_volume(percent: int, name: str = "") -> bool:
    """Set ONE output's volume 0-100. -> did it take.

    Falls back to the system master only when the named device cannot be
    reached, and says so, because silently moving a different device's volume
    is the bug this function exists to fix.
    """
    percent = max(0, min(100, int(percent)))
    endpoint = _endpoint_for(name)
    if endpoint is not None:
        try:
            endpoint.SetMasterVolumeLevelScalar(percent / 100.0, None)
            return True
        except Exception as e:
            print(f"[Audio] could not set '{name}' volume: {e}")
    if name:
        print(f"[Audio] '{name}' has no separate volume control here — "
              f"setting the system level instead.")
    try:
        from actions.computer_settings import volume_set, volume_get
        volume_set(percent)
        now = volume_get()
        return now is None or abs(now - percent) <= 3
    except Exception as e:
        print(f"[Audio] could not set the output volume: {e}")
        return False
