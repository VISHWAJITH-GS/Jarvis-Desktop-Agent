"""
Speech recognition — on this machine, or through a transcription API.

TWO BACKENDS, ONE FILE, FOR THE SAME REASON AS THE LANGUAGE MODEL
    `faster-whisper` runs Whisper locally on the CPU. `/v1/audio/transcriptions`
    is the same job over HTTP, and it is what OpenAI, Groq and several local
    servers all speak. Somebody who wants recognition to stay on their machine
    and somebody who wants it fast and hosted are asking for the same thing in
    two places, so it is one provider with a switch.

CPU IS THE DEFAULT AND THAT IS A PROMISE, NOT A FALLBACK
    The project states that no GPU is required. A provider that quietly needs
    one breaks that for everybody who read it, so `device` defaults to "cpu" and
    CUDA is something a person turns on. `compute_type` follows the device
    rather than being left on "auto": auto picks the best thing the *library*
    supports, which on a CPU without AVX512 can be a type that then fails at
    load time with a message about instruction sets.

ACCURACY TUNED FOR ACCENTED ENGLISH AND TAMIL
    Configured with beam search (beam_size=5), temperature fallback, custom
    vocabulary priming from config_manager hotwords, silence/noise padding,
    and automatic suppression of known Whisper silence hallucinations.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from importlib.util import find_spec
from typing import Optional

from . import audio
from .base import DEFAULT_CAPS, Field
from memory import config_manager

# Ordered by recommendation/accuracy.
# Medium is the default for maximum accuracy on accented English/Tamil.
MODELS = ("medium", "small", "distil-large-v3", "large-v3", "large-v3-turbo", "base", "tiny")
DEFAULT_MODEL = "medium"


def _threads() -> int:
    """How many CPU threads recognition may use.

    Not all of them. This runs while the assistant is also playing audio,
    drawing an avatar at ~50 mouth shapes a second and holding a socket open.
    ctranslate2's default is to take every core, which starves exactly those and
    produces stuttering playback during recognition.
    """
    try:
        n = os.cpu_count() or 4
    except Exception:
        n = 4
    return max(1, min(8, n // 2))


class WhisperSTT:
    def __init__(self, cfg: dict):
        self.backend = (cfg.get("backend") or "local").strip().lower()
        self.model_name = (cfg.get("model") or DEFAULT_MODEL).strip()
        self.device = (cfg.get("device") or "cpu").strip().lower()

        raw_lang = (cfg.get("language") or "en").strip().lower()
        if raw_lang in ("ta", "auto"):
            self.language = raw_lang
        else:
            self.language = "en"

        self.base_url = (cfg.get("base_url") or "").strip()
        self.api_key = (cfg.get("api_key") or "").strip()
        self.download_root = (cfg.get("download_root") or "").strip() or None
        self._model = None
        self._load_lock = asyncio.Lock()
        self.detected_language = self.language if self.language in ("en", "ta") else "en"
        self.last_confidence = 1.0
        self.last_is_low_confidence = False

        # Tuned decoding parameters for accented speech
        self.beam_size = int(cfg.get("beam_size") or 5)
        self.temperature = float(cfg.get("temperature") or 0.0)
        self.vad_filter = True
        self.min_silence_duration_ms = int(cfg.get("min_silence_duration_ms") or 500)
        self.speech_pad_ms = int(cfg.get("speech_pad_ms") or 300)
        self.no_speech_threshold = float(cfg.get("no_speech_threshold") or 0.60)

        self.caps = dict(DEFAULT_CAPS)

    # ── local ────────────────────────────────────────────────────────────────
    def _compute_type(self) -> str:
        # int8 on CPU is several times faster than float32 and the accuracy cost
        # on speech this short is not audible. float16 is a GPU format.
        return "float16" if self.device.startswith("cuda") else "int8"

    def _load_blocking(self):
        os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
        from faster_whisper import WhisperModel

        t0 = time.monotonic()
        model = WhisperModel(
            self.model_name,
            device=self.device,
            compute_type=self._compute_type(),
            cpu_threads=_threads(),
            download_root=self.download_root,
        )
        return model, time.monotonic() - t0

    async def _ensure_model(self, log=None):
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is not None:
                return self._model
            if find_spec("faster_whisper") is None:
                raise RuntimeError(
                    "faster-whisper is not installed — install it from the "
                    "providers panel, or with: pip install faster-whisper"
                )
            if log:
                log(f"SYS: loading the {self.model_name} speech model (the first time also downloads it)")
            self._model, took = await asyncio.to_thread(self._load_blocking)
            if log:
                log(
                    f"SYS: speech model ready in {took:.1f}s "
                    f"({self.device}, {self._compute_type()}, "
                    f"{_threads()} threads)"
                )
            return self._model

    def unload_model(self) -> None:
        """Release faster-whisper model from memory."""
        self._model = None
        import gc

        gc.collect()

    def _transcribe_blocking(self, samples, language_override: str = "") -> tuple[str, float, str, bool]:
        prompt = config_manager.get_stt_initial_prompt()

        lang = None
        if language_override:
            lang = language_override
        elif self.language in ("en", "ta"):
            lang = self.language
        # else lang is None -> auto-detection

        segments, info = self._model.transcribe(
            samples,
            language=lang,
            task="transcribe",
            beam_size=self.beam_size,
            best_of=self.beam_size,
            temperature=self.temperature,
            vad_filter=self.vad_filter,
            vad_parameters=dict(
                min_silence_duration_ms=self.min_silence_duration_ms,
                speech_pad_ms=self.speech_pad_ms,
            ),
            no_speech_threshold=self.no_speech_threshold,
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
        )

        segments_list = list(segments)
        text = " ".join(s.text.strip() for s in segments_list).strip()

        # Update language if auto-detecting
        if lang is None and info is not None:
            self._accept_language(info.language, info.language_probability, text)

        # Compute confidence metrics
        if segments_list:
            avg_logprob = sum(s.avg_logprob for s in segments_list) / len(segments_list)
            max_no_speech = max((s.no_speech_prob for s in segments_list), default=0.0)
            confidence = math.exp(max(avg_logprob, -10.0))
            is_low_confidence = (avg_logprob < -0.80) or (max_no_speech > 0.45) or (confidence < 0.60)
        else:
            confidence = 0.0
            is_low_confidence = False

        # Suppress silence/noise hallucinations
        if text and audio.is_whisper_hallucination(text):
            return "", 0.0, self.detected_language, False

        self.last_confidence = confidence
        self.last_is_low_confidence = is_low_confidence
        return text, confidence, self.detected_language, is_low_confidence

    _LANG_MIN_CONFIDENCE = 0.65
    _LANG_MIN_CHARS = 12

    def _accept_language(self, code: str, confidence: float, text: str) -> None:
        """Update the language the voice follows — only on real evidence."""
        code = code.strip().lower()
        if not code:
            return
        if not self.detected_language:
            self.detected_language = code
            return
        if code == self.detected_language:
            return
        if confidence >= self._LANG_MIN_CONFIDENCE and len(text) >= self._LANG_MIN_CHARS:
            print(f"[STT] language {self.detected_language} -> {code} ({confidence:.0%}, {len(text)} chars)")
            self.detected_language = code
            return
        print(
            f"[STT] ignoring language guess {code} ({confidence:.0%}, "
            f"{len(text)} chars) - keeping {self.detected_language}"
        )

    # ── hosted ───────────────────────────────────────────────────────────────
    def _api_blocking(self, pcm: bytes, sample_rate: int) -> str:
        import httpx
        from .llm_openai import _normalise, _is_loopback, _terse

        base = _normalise(self.base_url)
        wav = audio.to_wav(pcm, rate=sample_rate)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = {"model": self.model_name, "response_format": "json", "language": "en"}
        with httpx.Client(timeout=60.0, trust_env=not _is_loopback(base)) as cl:
            r = cl.post(
                f"{base}/audio/transcriptions",
                headers=headers,
                data=data,
                files={"file": ("speech.wav", wav, "audio/wav")},
            )
            if r.status_code >= 400:
                raise RuntimeError(f"{r.status_code} {_terse(r.text)}")
            payload = r.json()
        self._accept_language(str(payload.get("language") or self.language or ""), 1.0, "x" * 99)
        return (payload.get("text") or "").strip()

    # ── hybrid cloud fallback ────────────────────────────────────────────────
    async def _hybrid_gemini_transcribe(self, pcm: bytes, sample_rate: int) -> Optional[str]:
        """Multimodal one-shot REST fallback via core.gemini when local STT is uncertain."""
        try:
            from core import gemini

            wav_bytes = audio.to_wav(pcm, rate=sample_rate)
            prompt = (
                "You are an accurate Speech-To-Text transcription engine for Indian English and Tamil-accented English. "
                "Listen carefully to the audio and transcribe EXACTLY what the user spoke. "
                "Output ONLY the transcribed words with proper capitalization and punctuation. "
                "Do not add any preamble, explanation, or quotes. If there is only silence, noise, or music, output empty string."
            )
            wav_part = {"mime_type": "audio/wav", "data": wav_bytes}
            result = await asyncio.to_thread(gemini.text, [prompt, wav_part], tier=gemini.FAST)
            if result and isinstance(result, str):
                cleaned = result.strip().strip("\"'")
                if cleaned and not audio.is_whisper_hallucination(cleaned):
                    return cleaned
        except Exception:
            pass
        return None

    # ── the contract ─────────────────────────────────────────────────────────
    async def transcribe(self, pcm: bytes, sample_rate: int = audio.MIC_RATE, language: str = "", log=None) -> str:
        if not pcm:
            return ""

        # Preprocess: resample to 16kHz, remove DC offset, gentle normalize, pad edges, min 0.3s duration
        samples = audio.preprocess_for_whisper(
            pcm,
            sample_rate=sample_rate,
            min_duration_s=0.30,
            pad_start_ms=150,
            pad_end_ms=250,
            normalize=True,
            remove_dc=True,
        )
        if samples.size == 0:
            return ""

        if language:
            if language in ("en", "ta"):
                self.language = language
            elif language in ("auto", ""):
                self.language = "auto"

        if self.backend == "api":
            return await asyncio.to_thread(self._api_blocking, pcm, sample_rate)

        # Optional hybrid mode: try Gemini first if explicitly enabled in settings
        if config_manager.is_stt_hybrid_mode():
            cloud_text = await self._hybrid_gemini_transcribe(pcm, sample_rate)
            if cloud_text:
                if log:
                    log(f"STT (Gemini Hybrid): \"{cloud_text}\"")
                return cloud_text

        await self._ensure_model(log)
        text, conf, lang, is_low = await asyncio.to_thread(self._transcribe_blocking, samples)

        # Fallback to hybrid Gemini if local confidence is low
        if is_low and text and not config_manager.is_stt_hybrid_mode():
            cloud_text = await self._hybrid_gemini_transcribe(pcm, sample_rate)
            if cloud_text:
                if log:
                    log(f"STT: local confidence low ({conf:.0%}), upgraded via Gemini: \"{cloud_text}\"")
                return cloud_text

        # Activity visibility logging
        if text and log:
            flag = " [LOW CONFIDENCE]" if is_low else ""
            log(f"STT: \"{text}\" ({lang}, {conf:.0%}{flag})")

        return text

    async def warmup(self, log=None) -> None:
        """Load the model and run one throwaway recognition before user speaks."""
        if self.backend != "local":
            return
        try:
            await self._ensure_model(log)
            t0 = time.monotonic()
            silence = audio.to_float32(b"\x00" * 3200)  # 0.1s at 16 kHz
            await asyncio.to_thread(self._transcribe_blocking, silence)
            if log:
                log(f"SYS: speech recognition warm ({time.monotonic() - t0:.1f}s, paid once)")
        except Exception as e:
            if log:
                log(f"SYS: speech model not ready — {type(e).__name__}: {str(e)[:100]}")

    def probe(self) -> tuple[bool, str]:
        if self.backend == "api":
            if not self.base_url:
                return False, "No address set."
            return True, "Will be checked on first use."
        if find_spec("faster_whisper") is None:
            return False, "faster-whisper is not installed."
        return True, (f"{self.model_name} on {self.device} ({self._compute_type()}, {_threads()} threads)")


def prepare(values: dict, report=None) -> None:
    """Download model as soon as chosen in settings, with live progress reporting."""
    backend = str((values or {}).get("backend") or "local").strip().lower()
    if backend != "local":
        return
    model_name = str((values or {}).get("model") or DEFAULT_MODEL).strip()
    if not model_name:
        return
    try:
        os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
        import faster_whisper

        if report:
            report(None, f"Checking / downloading Whisper {model_name} model...")
        t0 = time.monotonic()
        faster_whisper.download_model(model_name)
        if report:
            report(True, f"Whisper {model_name} downloaded and ready in {time.monotonic() - t0:.1f}s.")
    except Exception as e:
        if report:
            report(False, f"Download failed: {type(e).__name__}: {str(e)[:120]}")


def _make(cfg: dict) -> WhisperSTT:
    return WhisperSTT(cfg)


PROVIDER = {
    "id": "whisper",
    "kind": "stt",
    "label": "Whisper",
    "hint": "On this machine with faster-whisper, or through a transcription API",
    "requires": (),
    "install": "faster-whisper",
    "catalog": (
        {
            "id": "local",
            "label": "Whisper on this computer",
            "note": "free · private · runs on the CPU",
            "local": True,
            "values": {"backend": "local", "base_url": "", "api_key": "", "model": "medium"},
        },
        {
            "id": "groq",
            "label": "Groq Whisper",
            "note": "cloud · very fast · free tier",
            "local": False,
            "values": {
                "backend": "api",
                "model": "whisper-large-v3-turbo",
                "base_url": "https://api.groq.com/openai/v1",
            },
        },
        {
            "id": "openai",
            "label": "OpenAI Whisper",
            "note": "cloud · needs a key",
            "local": False,
            "values": {"backend": "api", "model": "whisper-1", "base_url": "https://api.openai.com/v1"},
        },
    ),
    "needs": (
        Field(
            "backend",
            "Where it runs",
            kind="choice",
            choices=("local", "api"),
            default="local",
            hint="local = on this machine, nothing leaves it",
        ),
        Field(
            "model",
            "Model",
            kind="choice",
            choices=MODELS,
            default=DEFAULT_MODEL,
            hint="medium is high accuracy on CPU (~3-4s); small is faster (~1.5s); large models need GPU",
        ),
        Field(
            "device",
            "Processor",
            kind="choice",
            choices=("cpu", "cuda"),
            default="cpu",
            required=False,
            advanced=True,
            hint="CPU is the default and needs no graphics card",
        ),
        Field(
            "language",
            "Language",
            kind="choice",
            choices=("en", "auto", "ta"),
            default="en",
            required=False,
            advanced=True,
            hint="en (forced by default for best English recognition); ta for Tamil; auto for dynamic detection",
        ),
        Field(
            "base_url",
            "API address",
            default="",
            required=False,
            hint="Only for the api backend, e.g. https://api.groq.com/openai/v1",
        ),
        Field("api_key", "API key", kind="secret", default="", required=False),
        Field(
            "download_root",
            "Model folder",
            kind="path",
            default="",
            required=False,
            advanced=True,
            hint="Leave empty for the usual cache in your home folder",
        ),
    ),
    "caps": {},
    "factory": _make,
}


def available() -> bool:
    return find_spec("httpx") is not None
