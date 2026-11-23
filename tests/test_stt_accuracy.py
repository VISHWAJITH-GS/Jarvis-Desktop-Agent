"""
Unit tests for STT improvements (Part B):
- Audio preprocessing (resampling, DC offset, normalization, padding, min duration)
- Whisper silence hallucination detection
- Custom vocabulary / hotwords management & voice commands
- Language policy and confidence thresholds
"""

import numpy as np
import pytest

from actions.jarvis_control import jarvis_control
from memory import config_manager
from providers import audio
from providers.stt_whisper import WhisperSTT, DEFAULT_MODEL, MODELS


def test_hallucination_detection():
    # Hallucinations produced on silence/noise
    assert audio.is_whisper_hallucination("Thank you for watching.")
    assert audio.is_whisper_hallucination("Thanks for watching")
    assert audio.is_whisper_hallucination("Thank you very much.")
    assert audio.is_whisper_hallucination("Please like and subscribe!")
    assert audio.is_whisper_hallucination("Subtitles by Amara.org")
    assert audio.is_whisper_hallucination("Transcribed by BBC")
    assert audio.is_whisper_hallucination("[Music]")
    assert audio.is_whisper_hallucination("(Applause)")
    assert audio.is_whisper_hallucination("...")
    assert audio.is_whisper_hallucination("you you you you you")
    assert audio.is_whisper_hallucination("thank you thank you thank you")

    # Legitimate user utterances must NEVER be flagged as hallucinations
    assert not audio.is_whisper_hallucination("What is the weather today?")
    assert not audio.is_whisper_hallucination("Jarvis open VS Code and check the algorithm")
    assert not audio.is_whisper_hallucination("Tell me about placement interview preparation")
    assert not audio.is_whisper_hallucination("Yes please")
    assert not audio.is_whisper_hallucination("Thank you Jarvis")


def test_audio_preprocessing_duration_cutoff():
    # Under 0.3s should return an empty float32 array
    short_pcm = b"\x00\x00" * int(16000 * 0.20)  # 0.20s
    res = audio.preprocess_for_whisper(short_pcm, sample_rate=16000, min_duration_s=0.30)
    assert res.size == 0


def test_audio_preprocessing_padding_and_normalization():
    # 1.0s of 440Hz sine wave at 16kHz
    sr = 16000
    dur = 1.0
    t = np.linspace(0, dur, int(sr * dur), endpoint=False, dtype=np.float32)
    sine = 0.05 * np.sin(2 * np.pi * 440 * t)
    pcm = audio.to_pcm16(sine)

    # Preprocess with 150ms start padding and 250ms end padding
    processed = audio.preprocess_for_whisper(
        pcm,
        sample_rate=sr,
        min_duration_s=0.30,
        pad_start_ms=150,
        pad_end_ms=250,
        normalize=True,
        remove_dc=True,
    )

    expected_len = int(sr * dur) + int(sr * 0.15) + int(sr * 0.25)
    assert len(processed) == expected_len
    # Normalized peak should be scaled up from 0.05 towards 0.88
    peak = float(np.max(np.abs(processed)))
    assert 0.80 <= peak <= 0.95
    # Start and end pad samples should be 0.0
    assert processed[0] == 0.0
    assert processed[-1] == 0.0


def test_stt_hotwords_config():
    initial_words = config_manager.get_stt_hotwords()
    assert "Jarvis" in initial_words
    assert "VS Code" in initial_words
    assert "GitHub" in initial_words

    test_word = "PyTestCustomVocabularyWord99"
    try:
        # Add word
        added = config_manager.add_stt_hotword(test_word)
        assert added is True
        assert test_word in config_manager.get_stt_hotwords()

        # Prompt formatting
        prompt = config_manager.get_stt_initial_prompt()
        assert test_word in prompt

        # Adding duplicate returns False
        assert config_manager.add_stt_hotword(test_word) is False
    finally:
        # Cleanup
        config_manager.remove_stt_hotword(test_word)
        assert test_word not in config_manager.get_stt_hotwords()


def test_jarvis_control_vocabulary_actions():
    test_term = "SuperTensorFlowX"
    try:
        # Add via jarvis_control voice action
        res_add = jarvis_control({"action": "add_hotword", "word": test_term})
        assert f"Added '{test_term}'" in res_add
        assert test_term in config_manager.get_stt_hotwords()

        # List hotwords
        res_list = jarvis_control({"action": "list_hotwords"})
        assert test_term in res_list

        # Remove via jarvis_control
        res_rem = jarvis_control({"action": "remove_hotword", "word": test_term})
        assert f"Removed '{test_term}'" in res_rem
        assert test_term not in config_manager.get_stt_hotwords()
    finally:
        config_manager.remove_stt_hotword(test_term)


def test_whisper_stt_configuration():
    # Default initialization
    stt = WhisperSTT({})
    assert stt.model_name == "medium"
    assert stt.language == "en"
    assert stt.device == "cpu"
    assert stt._compute_type() == "int8"
    assert stt.beam_size == 5
    assert stt.vad_filter is True
    assert stt.min_silence_duration_ms == 500
    assert stt.speech_pad_ms == 300
    assert stt.no_speech_threshold == 0.60

    # Tamil language configuration
    stt_ta = WhisperSTT({"language": "ta"})
    assert stt_ta.language == "ta"

    # Auto language configuration
    stt_auto = WhisperSTT({"language": "auto"})
    assert stt_auto.language == "auto"


def test_language_switch_policy():
    stt = WhisperSTT({})
    stt.detected_language = "en"

    # Weak confidence or short text should NOT switch language
    stt._accept_language("ta", 0.50, "hello world!")
    assert stt.detected_language == "en"

    stt._accept_language("ta", 0.85, "hi")  # short chars (< 12)
    assert stt.detected_language == "en"

    # High confidence and sufficient text DOES switch
    stt._accept_language("ta", 0.85, "vanakkam nanba eppadi irukkeenga")
    assert stt.detected_language == "ta"
