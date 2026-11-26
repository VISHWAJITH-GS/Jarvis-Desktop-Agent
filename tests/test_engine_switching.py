"""
tests/test_engine_switching.py — Unit test suite for Seamless Engine Switching (Part A).
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from memory import config_manager
from actions import jarvis_control
from providers.engine_pipeline import PipelineEngine
from providers import llm_openai
from core import prompt_builder


def test_engine_config_get_and_set():
    """Verify get_active_engine and set_active_engine work with provider modes."""
    orig_mode = config_manager.get_provider_mode()
    try:
        # Default is Gemini
        config_manager.set_active_engine(config_manager.ENGINE_GEMINI_LIVE)
        assert config_manager.get_active_engine() == config_manager.ENGINE_GEMINI_LIVE
        assert config_manager.get_provider_mode() == config_manager.PROVIDER_MODE_GEMINI
        assert "Google" in config_manager.get_audio_destination_label()

        # Switch to Local
        config_manager.set_active_engine(config_manager.ENGINE_LOCAL)
        assert config_manager.get_active_engine() == config_manager.ENGINE_LOCAL
        assert config_manager.get_provider_mode() == config_manager.PROVIDER_MODE_PIPELINE
        assert "Local" in config_manager.get_audio_destination_label()
    finally:
        config_manager.save_provider_mode(orig_mode)


def test_ensure_local_pipeline_configured():
    """Verify ensure_local_pipeline_configured guarantees local components."""
    orig_pipe = config_manager.get_pipeline()
    try:
        # Mock pipeline with non-local / missing components
        config_manager.save_providers(pipeline={"llm": "openai_compat@openai", "stt": "cloud_stt", "tts": "elevenlabs"})
        config_manager.ensure_local_pipeline_configured()
        pipe = config_manager.get_pipeline()
        assert pipe.get("stt") == "whisper"
        assert "ollama" in pipe.get("llm")
        assert pipe.get("tts") == "piper"
    finally:
        config_manager.save_providers(pipeline=orig_pipe)


def test_voice_switch_requires_confirmation():
    """Voice switching must require UI confirmation (core.confirm pattern)."""
    player = MagicMock()
    orig_mode = config_manager.get_provider_mode()
    try:
        config_manager.set_active_engine(config_manager.ENGINE_GEMINI_LIVE)

        # Calling switch_engine without confirmed flag should request confirmation
        res = jarvis_control.jarvis_control(
            parameters={"action": "switch_to_local"},
            player=player,
        )
        assert "CONFIRMATION_REQUIRED" in res or "Switch to Local" in str(res)
        player.show_confirm.assert_called_once()

        # Engine must NOT have switched yet
        assert config_manager.get_active_engine() == config_manager.ENGINE_GEMINI_LIVE

        # Now calling with confirmed=True executes switch
        res_confirmed = jarvis_control.jarvis_control(
            parameters={"action": "switch_to_local", "confirmed": True},
            player=player,
        )
        assert "Switched to Local" in res_confirmed
        assert config_manager.get_active_engine() == config_manager.ENGINE_LOCAL
        player.request_engine_switch.assert_called_with(config_manager.ENGINE_LOCAL)
    finally:
        config_manager.save_provider_mode(orig_mode)


def test_pipeline_engine_load_history():
    """PipelineEngine.load_history should load recent session turns."""
    engine = PipelineEngine(llm=MagicMock(), stt=MagicMock(), tts=MagicMock())
    turns = [
        "User: How is the weather today?",
        "JARVIS: It is sunny and 24 degrees Celsius, Sir.",
        "User: Remind me to call John at 5pm.",
        "JARVIS: I have noted that reminder, Sir.",
    ]
    engine.load_history(turns)
    assert len(engine._history) == 4
    assert engine._history[0] == {"role": "user", "content": "How is the weather today?"}
    assert engine._history[1] == {"role": "assistant", "content": "It is sunny and 24 degrees Celsius, Sir."}
    assert engine._history[2] == {"role": "user", "content": "Remind me to call John at 5pm."}
    assert engine._history[3] == {"role": "assistant", "content": "I have noted that reminder, Sir."}


def test_model_tool_budget_4b_vs_8b():
    """Verify tool budgeting is tailored for 4B vs 8B models on local hardware."""
    # 4B model
    prov_4b = llm_openai.OpenAICompatLLM(
        {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:4b"}
    )
    assert prov_4b.caps["max_tools"] == 10
    assert prov_4b.caps["max_tool_chars"] == 8000

    # 8B model
    prov_8b = llm_openai.OpenAICompatLLM(
        {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:8b"}
    )
    assert prov_8b.caps["max_tools"] == 14
    assert prov_8b.caps["max_tool_chars"] == 12000

    # Explicit user override
    prov_custom = llm_openai.OpenAICompatLLM(
        {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:4b", "max_tools": 6, "max_tool_chars": 5000}
    )
    assert prov_custom.caps["max_tools"] == 6
    assert prov_custom.caps["max_tool_chars"] == 5000


def test_describe_limits_barge_in():
    """Verify _describe_limits honors can_barge_in capability."""
    # Gemini Live: can_barge_in=True (default)
    limits_live = prompt_builder._describe_limits(has_vision=True, has_mic=True, can_barge_in=True)
    assert "barge-in is not supported" not in limits_live

    # Local Engine: can_barge_in=False
    limits_local = prompt_builder._describe_limits(has_vision=False, has_mic=True, can_barge_in=False)
    assert "barge-in is not supported" in limits_local
    assert "You have no sight at all" in limits_local


def test_configure_local_ollama_model():
    """Verify configure_local_ollama_model configures model and budgets appropriately."""
    config_manager.configure_local_ollama_model("qwen3.5:4b")
    cfg_4b = config_manager.get_provider_config("openai_compat@ollama")
    assert cfg_4b["model"] == "qwen3.5:4b"
    assert cfg_4b["max_tools"] == "10"
    assert cfg_4b["max_tool_chars"] == "8000"

    config_manager.configure_local_ollama_model("qwen3:8b")
    cfg_8b = config_manager.get_provider_config("openai_compat@ollama")
    assert cfg_8b["model"] == "qwen3:8b"
    assert cfg_8b["max_tools"] == "14"
    assert cfg_8b["max_tool_chars"] == "12000"


def test_pipeline_engine_warmup():
    """Verify PipelineEngine.warmup preloads the model before listening."""
    import asyncio
    mock_llm = MagicMock()
    mock_llm.model = "qwen3:8b"
    mock_llm.preload_async = AsyncMock(return_value=(True, "Loaded"))
    log_lines = []

    engine = PipelineEngine(llm=mock_llm, stt=MagicMock(spec=[]), tts=MagicMock(spec=[]), log=log_lines.append)
    asyncio.run(engine.warmup())

    mock_llm.preload_async.assert_called_once()
    assert any("Loading local model 'qwen3:8b'" in line for line in log_lines)
    assert any("is loaded in VRAM" in line for line in log_lines)


def test_voice_switch_with_specific_model():
    """Voice command specifying model should configure that model."""
    orig_mode = config_manager.get_provider_mode()
    try:
        player = MagicMock()
        res = jarvis_control.jarvis_control(
            parameters={"action": "switch_engine", "engine": "local", "model": "qwen3.5:4b", "confirmed": True},
            player=player,
        )
        assert "Switched to Local (qwen3.5:4b)" in res
        cfg = config_manager.get_provider_config("openai_compat@ollama")
        assert cfg["model"] == "qwen3.5:4b"
    finally:
        config_manager.save_provider_mode(orig_mode)
