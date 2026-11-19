"""
Local wake-word detection for JARVIS ("Jarvis" alone).

Design goals:
  • ZERO cost when the feature is inactive — openwakeword (and the optional
    local keyword spotter for wake words without a dedicated ONNX model, such
    as standalone "Jarvis") are imported ONLY inside start()/install/worker helpers, never
    at module load.
  • ZERO latency on the audio path — the microphone callback only ever does a
    cheap, non-blocking queue push (feed()); inference runs on background
    threads, so the real-time audio thread and Gemini stream are never slowed.
  • Fully local & offline — audio fed here never leaves the machine; while
    sleeping, nothing is streamed to the cloud until the wake word ("Jarvis")
    or the global Force Listening shortcut (Ctrl + Alt + K) fires.
  • Hybrid detection:
      1. Fast ONNX path via openwakeword (e.g. "hey_jarvis" + any custom ONNX
         models found for configured wake words) evaluated at threshold 0.200.
      2. Low-power RMS-VAD-gated local keyword spotter (using cached offline
         faster-whisper if installed) for wake words without a pretrained ONNX
         model (such as standalone "Jarvis"). During room silence the spotter
         sleeps with ~0% CPU.
"""
from __future__ import annotations

import inspect
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

# Active wake words and sensitivity threshold ("Jarvis" alone, threshold 0.200 / 200).
DEFAULT_WAKE_WORDS: tuple[str, ...] = ("Jarvis",)
DEFAULT_THRESHOLD: float = 0.200

# Pretrained openwakeword model kept as a module constant for compatibility.
WAKE_MODEL = "hey_jarvis"
# Mic frames arrive at 16 kHz int16.
SAMPLE_RATE = 16000

# Mapping from normalized wake word phrases to pretrained openwakeword models.
_BUILTIN_OWW_MODELS: dict[str, str] = {
    "jarvis": "hey_jarvis",
    "hey jarvis": "hey_jarvis",
    "hey_jarvis": "hey_jarvis",
    "alexa": "alexa",
    "hey mycroft": "hey_mycroft",
    "hey_mycroft": "hey_mycroft",
    "hey rhasspy": "hey_rhasspy",
    "hey_rhasspy": "hey_rhasspy",
    "timer": "timer",
    "weather": "weather",
}

# Post-detection cooldown (seconds) to avoid double-triggering on trailing frames
# at sensitive thresholds (e.g. 0.100).
_DETECT_COOLDOWN_S = 1.5

# Cheap RMS energy VAD parameters for the secondary keyword spotter (int16 PCM).
_VAD_SPEECH_RMS = 260.0        # RMS above this counts as speech energy
_VAD_MIN_SPEECH_S = 0.18       # minimum voiced duration to evaluate
_VAD_SILENCE_END_S = 0.35      # trailing silence that ends a short utterance
_VAD_MAX_UTTERANCE_S = 1.8     # flush and check after this much continuous speech


def normalize_wake_words(wake_words: Optional[Iterable[str]]) -> list[str]:
    """Return a clean, deduplicated list of active wake words, excluding 'hello'."""
    if not wake_words:
        return list(DEFAULT_WAKE_WORDS)
    if isinstance(wake_words, str):
        raw_items = [w.strip() for w in wake_words.split(",")]
    else:
        raw_items = [str(w).strip() for w in wake_words]

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if not item or item.lower() == "hello":
            continue
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out or list(DEFAULT_WAKE_WORDS)


def match_wake_word(transcript: str, wake_words: Iterable[str]) -> Optional[str]:
    """
    Return the matched configured wake word if present as a standalone word or
    phrase in `transcript` (case-insensitive, punctuation-tolerant), else None.
    """
    if not transcript:
        return None
    cleaned = re.sub(r"[^a-z0-9\s]", " ", transcript.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None

    for word in normalize_wake_words(wake_words):
        target = re.sub(r"[^a-z0-9\s]", " ", word.lower())
        target = re.sub(r"\s+", " ", target).strip()
        if not target:
            continue
        if re.search(rf"(?:^|\s){re.escape(target)}(?:\s|$)", cleaned):
            return word
    return None


def _find_custom_model_file(word: str) -> Optional[str]:
    """Look for a custom ONNX/TFLite wake model file for `word` on disk."""
    slug = re.sub(r"[^a-z0-9]+", "_", word.lower()).strip("_")
    if not slug:
        return None

    search_dirs: list[Path] = []
    try:
        base_dir = Path(__file__).resolve().parent.parent
        search_dirs.append(base_dir / "models")
    except Exception:
        pass
    try:
        import openwakeword
        search_dirs.append(Path(openwakeword.__file__).resolve().parent / "resources" / "models")
    except Exception:
        pass

    for d in search_dirs:
        if not d.is_dir():
            continue
        for ext in ("onnx", "tflite"):
            matches = sorted(d.glob(f"{slug}*.{ext}"))
            if matches:
                return str(matches[0])
    return None


def _resolve_wake_models(wake_words: Iterable[str]) -> tuple[list[str], list[str], dict[str, str]]:
    """
    Split configured wake words into:
      - oww_models: model names/paths to pass to openwakeword.model.Model
      - spotter_words: wake words to watch via the local VAD keyword spotter
      - model_to_word: mapping from model key substring -> display wake word
    """
    words = normalize_wake_words(wake_words)
    oww_models: list[str] = []
    spotter_words: list[str] = []
    model_to_word: dict[str, str] = {}

    for word in words:
        key = word.lower().strip()
        if key in _BUILTIN_OWW_MODELS:
            model_name = _BUILTIN_OWW_MODELS[key]
            if model_name not in oww_models:
                oww_models.append(model_name)
            model_to_word[model_name] = word
            # Also include standalone "Jarvis" in the local keyword spotter so
            # saying "Jarvis" without "Hey" still wakes reliably if cached STT exists.
            if word not in spotter_words:
                spotter_words.append(word)
        else:
            custom_path = _find_custom_model_file(word)
            if custom_path:
                oww_models.append(custom_path)
                model_to_word[Path(custom_path).stem.lower()] = word
            if word not in spotter_words:
                spotter_words.append(word)

    return oww_models, spotter_words, model_to_word


def is_installed() -> bool:
    """True if the openwakeword package is importable (no model check)."""
    try:
        import importlib.util
        return importlib.util.find_spec("openwakeword") is not None
    except Exception:
        return False


def is_ready() -> bool:
    """True if openwakeword is installed AND its model files are present on disk.

    This is a cheap, DETERMINISTIC file-existence check. It deliberately does NOT
    construct a Model to probe readiness — doing that is slow and, worse, can clash
    with the detector's own Model when it's already running, which intermittently
    returned False and made the UI flicker to 'not downloaded'. Never raises.
    """
    if not is_installed():
        return False
    try:
        import openwakeword
        models_dir = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
        if not models_dir.is_dir():
            return False
        has_wake = (any(models_dir.glob(f"{WAKE_MODEL}*.onnx"))
                    or any(models_dir.glob(f"{WAKE_MODEL}*.tflite")))
        has_mel = (any(models_dir.glob("melspectrogram*.onnx"))
                   or any(models_dir.glob("melspectrogram*.tflite")))
        has_emb = (any(models_dir.glob("embedding_model*.onnx"))
                   or any(models_dir.glob("embedding_model*.tflite")))
        return bool(has_wake and has_mel and has_emb)
    except Exception:
        return False


def install_and_download(logger: Callable[[str], None] = print,
                         notify: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """
    One-click setup for the UI button: pip-install openwakeword if missing, then
    download the wake model. Returns (ok, message). Never raises — every failure
    is reported through the returned message and the logger.
    """
    _tell = notify or (lambda _msg: None)
    try:
        if not is_installed():
            logger("Wake word: installing openwakeword (one-time)…")
            _tell("Wake word: installing openwakeword (one-time)…")
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "openwakeword"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
                return False, f"pip install failed: {tail[0][:160]}"
        # Download the pretrained melspectrogram/embedding + wake models.
        logger("Wake word: downloading models…")
        _tell("Wake word: downloading models…")
        try:
            import openwakeword.utils as _u
            try:
                _u.download_models([WAKE_MODEL])
            except TypeError:
                _u.download_models()   # older signature downloads the default set
        except Exception as e:
            return False, f"model download failed: {e}"

        if not is_ready():
            return False, "installed, but the wake model could not be loaded."
        logger("Wake word: ready.")
        return True, "Wake word installed and ready."
    except Exception as e:
        return False, f"setup error: {e}"


class WakeWordDetector:
    """
    Runs local wake-word detection in dedicated background threads.
    Supports active wake word ("Jarvis" alone) at configurable
    sensitivity (default threshold: 0.200).

    The mic thread calls `feed()` with raw int16 frames; detections invoke
    `on_detect()` (or `on_detect(word)` if the callback accepts 1 argument).
    """

    def __init__(
        self,
        on_detect: Callable[..., None],
        threshold: float = DEFAULT_THRESHOLD,
        wake_words: Optional[Iterable[str]] = None,
        logger: Callable[[str], None] = print,
        notify: Callable[[str], None] | None = None,
        enable_keyword_spotter: bool = True,
    ):
        self._on_detect = on_detect
        try:
            t = float(threshold)
            if t > 1.0 and t <= 1000.0:
                t = t / 1000.0
            self._threshold = max(0.0, min(1.0, t))
        except (TypeError, ValueError):
            self._threshold = DEFAULT_THRESHOLD
        self._wake_words: list[str] = normalize_wake_words(wake_words)
        self._logger = logger
        self._notify = notify or (lambda _msg: None)
        self._enable_keyword_spotter = bool(enable_keyword_spotter)

        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._spotter_queue: queue.Queue = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        self._spotter_thread: threading.Thread | None = None
        self._running = False
        self._model = None
        self._whisper_model = None
        self._whisper_failed = False
        self._ready = False
        self._oww_models: list[str] = []
        self._spotter_words: list[str] = []
        self._model_to_word: dict[str, str] = {}
        self._detect_lock = threading.Lock()
        self._last_detect_ts = 0.0
        self._cooldown_s = _DETECT_COOLDOWN_S
        self.last_detected_word: Optional[str] = None
        self._callback_accepts_word = self._check_callback_arity(on_detect)
        self._paused: bool = False

    @staticmethod
    def _check_callback_arity(cb: Callable[..., None]) -> bool:
        try:
            sig = inspect.signature(cb)
            params = [
                p for p in sig.parameters.values()
                if p.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            ]
            has_varargs = any(
                p.kind == inspect.Parameter.VAR_POSITIONAL
                for p in sig.parameters.values()
            )
            return has_varargs or len(params) >= 1
        except Exception:
            return False

    @property
    def threshold(self) -> float:
        return self._threshold

    @threshold.setter
    def threshold(self, value: float) -> None:
        self._threshold = max(0.0, min(1.0, float(value)))

    @property
    def wake_words(self) -> list[str]:
        return list(self._wake_words)

    def set_wake_words(self, wake_words: Iterable[str]) -> None:
        self._wake_words = normalize_wake_words(wake_words)
        self._oww_models, self._spotter_words, self._model_to_word = _resolve_wake_models(self._wake_words)

    def start(self) -> bool:
        """Load the wake model(s) and spawn background inference threads.
        Safe to call again — a no-op if already running. Never raises."""
        if self._running:
            return True

        self._oww_models, self._spotter_words, self._model_to_word = _resolve_wake_models(self._wake_words)
        oww_loaded = False

        if self._oww_models:
            try:
                from openwakeword.model import Model
                self._model = Model(
                    wakeword_models=self._oww_models,
                    inference_framework="onnx",
                )
                oww_loaded = True
            except Exception as e:
                self._logger(f"Wake word: could not load openwakeword model(s) — {e}")
                self._model = None

        spotter_possible = self._enable_keyword_spotter and bool(self._spotter_words) and self._can_import_whisper()

        if not oww_loaded and not spotter_possible:
            self._notify("Wake word unavailable — press Ctrl+Alt+K or use WAKE NOW.")
            return False

        self._running = True
        self._ready = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="WakeWordThread")
        self._thread.start()

        if spotter_possible:
            self._spotter_thread = threading.Thread(
                target=self._spotter_loop,
                daemon=True,
                name="WakeWordSpotterThread",
            )
            self._spotter_thread.start()

        words_str = ", ".join(f"'{w}'" for w in self._wake_words)
        self._logger(
            f"Wake word: listening for {words_str} "
            f"(threshold={self._threshold:.3f}, oww={oww_loaded}, spotter={spotter_possible})."
        )
        return True

    @staticmethod
    def _can_import_whisper() -> bool:
        try:
            import importlib.util
            return importlib.util.find_spec("faster_whisper") is not None
        except Exception:
            return False

    def stop(self) -> None:
        self._running = False
        self._paused = False
        for q in (self._queue, self._spotter_queue):
            try:
                q.put_nowait(None)
            except Exception:
                pass
        self._model = None
        self._ready = False

    def pause(self) -> None:
        """Pause wake-word processing without unloading models from memory."""
        self._paused = True
        self._drain()

    def resume(self) -> None:
        """Resume wake-word processing after pause."""
        self._drain()
        model = getattr(self, "_model", None)
        if model is not None and hasattr(model, "reset"):
            try:
                model.reset()
            except Exception:
                pass
        self._paused = False

    def reset_buffers(self) -> None:
        """Drain queued audio and reset openwakeword internal buffers."""
        self._drain()
        model = getattr(self, "_model", None)
        if model is not None and hasattr(model, "reset"):
            try:
                model.reset()
            except Exception:
                pass

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def ready(self) -> bool:
        return self._ready

    def feed(self, frame_int16) -> None:
        """Called from the mic callback (real-time thread). Must stay cheap and
        never block — the frame is copied and dropped if the queue is backed up."""
        if not self._running or self._paused:
            return
        try:
            data = frame_int16[:, 0].copy() if getattr(frame_int16, "ndim", 1) > 1 else frame_int16.copy()
            self._queue.put_nowait(data)
        except queue.Full:
            pass
        except Exception:
            pass

    def _map_score_to_word(self, scores: dict) -> tuple[str, float]:
        """Return (display_wake_word, highest_score) from an openwakeword scores dict."""
        best_word = self._wake_words[0] if self._wake_words else "Jarvis"
        best_score = 0.0
        for k, v in scores.items():
            val = float(v)
            if val > best_score:
                best_score = val
                kl = str(k).lower()
                matched_label = None
                for model_key, display_word in self._model_to_word.items():
                    if model_key in kl or display_word.lower() in kl:
                        matched_label = display_word
                        break
                best_word = matched_label or str(k)
        return best_word, best_score

    def _trigger_detection(self, word: str, score: float) -> None:
        with self._detect_lock:
            if not self._running:
                return
            now = time.monotonic()
            if (now - self._last_detect_ts) < self._cooldown_s:
                return
            self._last_detect_ts = now
            self.last_detected_word = word

        # Reset openwakeword buffers and drain queued frames to prevent double triggers
        if self._model is not None and hasattr(self._model, "reset"):
            try:
                self._model.reset()
            except Exception:
                pass
        self._drain()

        self._logger(f"Wake word: detected '{word}' (score={score:.3f}, threshold={self._threshold:.3f}).")
        try:
            if self._callback_accepts_word:
                self._on_detect(word)
            else:
                self._on_detect()
        except Exception as e:
            self._logger(f"Wake word: on_detect error — {e}")

    def _loop(self) -> None:
        import numpy as np

        speech_chunks: list = []
        speech_samples = 0
        silence_samples = 0
        min_speech_samples = int(SAMPLE_RATE * _VAD_MIN_SPEECH_S)
        end_silence_samples = int(SAMPLE_RATE * _VAD_SILENCE_END_S)
        max_utterance_samples = int(SAMPLE_RATE * _VAD_MAX_UTTERANCE_S)
        # Sensitivity scaling: lower threshold -> slightly lower VAD energy floor.
        vad_floor = max(140.0, _VAD_SPEECH_RMS * (0.75 + 0.5 * self._threshold))

        while self._running:
            try:
                frame = self._queue.get()
                if frame is None or not self._running:
                    break

                arr = np.asarray(frame, dtype=np.int16)

                # 1. Fast ONNX path (openwakeword)
                if self._model is not None:
                    scores = self._model.predict(arr)
                    if isinstance(scores, dict) and scores:
                        word, score = self._map_score_to_word(scores)
                        if score >= self._threshold:
                            speech_chunks.clear()
                            speech_samples = 0
                            silence_samples = 0
                            self._trigger_detection(word, score)
                            continue

                # 2. Low-power RMS-VAD accumulator for local keyword spotting ("Jarvis")
                if self._spotter_thread is not None and not self._whisper_failed:
                    f32 = arr.astype(np.float32)
                    rms = float(np.sqrt(np.mean(f32 * f32))) if f32.size else 0.0
                    if rms >= vad_floor:
                        speech_chunks.append(arr)
                        speech_samples += arr.size
                        silence_samples = 0
                        if speech_samples >= max_utterance_samples:
                            clip = np.concatenate(speech_chunks)
                            speech_chunks.clear()
                            speech_samples = 0
                            silence_samples = 0
                            self._enqueue_spotter_clip(clip)
                    elif speech_samples > 0:
                        speech_chunks.append(arr)
                        silence_samples += arr.size
                        if silence_samples >= end_silence_samples:
                            if speech_samples >= min_speech_samples:
                                clip = np.concatenate(speech_chunks)
                                self._enqueue_spotter_clip(clip)
                            speech_chunks.clear()
                            speech_samples = 0
                            silence_samples = 0
            except Exception as e:
                self._logger(f"Wake word: inference error — {e}")

    def _enqueue_spotter_clip(self, clip_int16) -> None:
        try:
            self._spotter_queue.put_nowait(clip_int16)
        except queue.Full:
            # Drop stale clip and keep the freshest utterance
            try:
                self._spotter_queue.get_nowait()
                self._spotter_queue.put_nowait(clip_int16)
            except Exception:
                pass

    def _load_local_whisper(self):
        """Lazily load a locally cached faster-whisper model (never downloads)."""
        if self._whisper_model is not None:
            return self._whisper_model
        if self._whisper_failed:
            return None
        try:
            from faster_whisper import WhisperModel
            for model_name in ("tiny", "base", "tiny.en", "base.en"):
                try:
                    self._whisper_model = WhisperModel(
                        model_name,
                        device="cpu",
                        compute_type="int8",
                        local_files_only=True,
                    )
                    self._logger(f"Wake word: offline keyword spotter ready (whisper-{model_name}).")
                    return self._whisper_model
                except Exception:
                    continue
            self._whisper_failed = True
            self._logger("Wake word: no cached offline Whisper model found; using ONNX models only.")
        except Exception as e:
            self._whisper_failed = True
            self._logger(f"Wake word: keyword spotter unavailable — {e}")
        return None

    def _spotter_loop(self) -> None:
        import numpy as np

        while self._running:
            try:
                clip = self._spotter_queue.get()
                if clip is None or not self._running:
                    break
                model = self._load_local_whisper()
                if model is None:
                    break

                audio_f32 = np.asarray(clip, dtype=np.float32) / 32768.0
                prompt_hint = ", ".join(self._spotter_words)
                segments, _ = model.transcribe(
                    audio_f32,
                    language="en",
                    beam_size=1,
                    best_of=1,
                    condition_on_previous_text=False,
                    vad_filter=False,
                    initial_prompt=prompt_hint,
                )
                seg_list = list(segments)
                transcript = " ".join(s.text for s in seg_list).strip()
                if not transcript:
                    continue

                # Confidence estimate from segment no_speech_prob (1.0 - min no_speech_prob)
                confidences = [
                    max(0.0, 1.0 - float(getattr(s, "no_speech_prob", 0.0)))
                    for s in seg_list
                ]
                confidence = max(confidences) if confidences else 1.0

                matched = match_wake_word(transcript, self._spotter_words)
                if matched and confidence >= self._threshold:
                    self._trigger_detection(matched, confidence)
            except Exception as e:
                self._logger(f"Wake word: keyword spotter error — {e}")

    def _drain(self) -> None:
        for q in (getattr(self, "_queue", None), getattr(self, "_spotter_queue", None)):
            if q is None:
                continue
            try:
                while True:
                    q.get_nowait()
            except Exception:
                pass
