"""Tests for per-phase LLM model routing in llm_config."""

import os
from unittest.mock import patch

import pytest

from src.filter.llm_config import LLMBackendConfig, LLMProvider, detect_llm_backend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_llm_env() -> dict[str, str]:
    """Return a copy of os.environ with all LLM-related vars removed."""
    keys_to_strip = {
        "LLM_PROVIDER", "LLM_MODEL",
        "LLM_FILTER_PROVIDER", "LLM_FILTER_MODEL",
        "LLM_MAP_PROVIDER", "LLM_MAP_MODEL",
        "LLM_ANALYZE_PROVIDER", "LLM_ANALYZE_MODEL",
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
        "OLLAMA_BASE_URL",
    }
    return {k: v for k, v in os.environ.items() if k not in keys_to_strip}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDetectBackendDefault:
    """phase='default' should use global LLM_PROVIDER / LLM_MODEL only."""

    def test_detect_backend_default_uses_global_model(self) -> None:
        env = {
            **_clean_llm_env(),
            "LLM_PROVIDER": "anthropic",
            "LLM_MODEL": "claude-sonnet-4-6",
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = detect_llm_backend(phase="default")

        assert cfg.provider == LLMProvider.ANTHROPIC
        assert cfg.model == "claude-sonnet-4-6"
        assert cfg.api_key == "sk-ant-test"

    def test_detect_backend_default_ignores_phase_vars(self) -> None:
        """Per-phase vars should have no effect when phase='default'."""
        env = {
            **_clean_llm_env(),
            "LLM_PROVIDER": "anthropic",
            "LLM_MODEL": "claude-sonnet-4-6",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "LLM_FILTER_MODEL": "should-be-ignored",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = detect_llm_backend(phase="default")

        assert cfg.model == "claude-sonnet-4-6"


class TestFilterPhase:

    def test_detect_backend_filter_phase_uses_filter_model(self) -> None:
        env = {
            **_clean_llm_env(),
            "LLM_PROVIDER": "anthropic",
            "LLM_MODEL": "claude-sonnet-4-6",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "LLM_FILTER_MODEL": "claude-haiku-4-5",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = detect_llm_backend(phase="filter")

        assert cfg.provider == LLMProvider.ANTHROPIC
        assert cfg.model == "claude-haiku-4-5"

    def test_filter_phase_provider_overrides_global(self) -> None:
        env = {
            **_clean_llm_env(),
            "LLM_PROVIDER": "anthropic",
            "LLM_MODEL": "claude-sonnet-4-6",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "OPENAI_API_KEY": "sk-openai-test",
            "LLM_FILTER_PROVIDER": "openai",
            "LLM_FILTER_MODEL": "gpt-4o-mini",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = detect_llm_backend(phase="filter")

        assert cfg.provider == LLMProvider.OPENAI
        assert cfg.model == "gpt-4o-mini"


class TestMapPhase:

    def test_detect_backend_map_phase_uses_map_model(self) -> None:
        env = {
            **_clean_llm_env(),
            "LLM_PROVIDER": "anthropic",
            "LLM_MODEL": "claude-opus-4-6",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "LLM_MAP_MODEL": "claude-sonnet-4-6",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = detect_llm_backend(phase="map")

        assert cfg.provider == LLMProvider.ANTHROPIC
        assert cfg.model == "claude-sonnet-4-6"


class TestAnalyzePhase:

    def test_detect_backend_analyze_phase_uses_analyze_model(self) -> None:
        env = {
            **_clean_llm_env(),
            "LLM_PROVIDER": "anthropic",
            "LLM_MODEL": "claude-sonnet-4-6",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "LLM_ANALYZE_MODEL": "claude-opus-4-6",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = detect_llm_backend(phase="analyze")

        assert cfg.provider == LLMProvider.ANTHROPIC
        assert cfg.model == "claude-opus-4-6"


class TestFallback:

    def test_fallback_to_global_when_phase_config_missing(self) -> None:
        """When no per-phase vars are set, global config is used."""
        env = {
            **_clean_llm_env(),
            "LLM_PROVIDER": "anthropic",
            "LLM_MODEL": "claude-sonnet-4-6",
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }
        with patch.dict(os.environ, env, clear=True):
            for phase in ("filter", "map", "analyze"):
                cfg = detect_llm_backend(phase=phase)
                assert cfg.provider == LLMProvider.ANTHROPIC
                assert cfg.model == "claude-sonnet-4-6"

    def test_no_config_returns_none_provider(self) -> None:
        """No env vars at all should yield NONE provider."""
        with patch.dict(os.environ, _clean_llm_env(), clear=True), \
             patch("src.filter.llm_config._claude_code_available", return_value=False), \
             patch("urllib.request.urlopen", side_effect=Exception("no ollama")):
            cfg = detect_llm_backend(phase="filter")

        assert cfg.provider == LLMProvider.NONE
        assert cfg.model == ""


class TestPhaseProviderOverridesGlobal:

    def test_phase_provider_overrides_global_provider(self) -> None:
        """Per-phase provider should override the global provider."""
        env = {
            **_clean_llm_env(),
            "LLM_PROVIDER": "anthropic",
            "LLM_MODEL": "claude-sonnet-4-6",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "GOOGLE_API_KEY": "google-test-key",
            "LLM_ANALYZE_PROVIDER": "google",
            "LLM_ANALYZE_MODEL": "gemini-2.5-pro",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = detect_llm_backend(phase="analyze")

        assert cfg.provider == LLMProvider.GOOGLE
        assert cfg.model == "gemini-2.5-pro"
        assert cfg.api_key == "google-test-key"

    def test_phase_provider_only_no_phase_model(self) -> None:
        """Per-phase provider set but no per-phase model — use global model."""
        env = {
            **_clean_llm_env(),
            "LLM_PROVIDER": "anthropic",
            "LLM_MODEL": "claude-sonnet-4-6",
            "OPENAI_API_KEY": "sk-openai-test",
            "LLM_MAP_PROVIDER": "openai",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = detect_llm_backend(phase="map")

        assert cfg.provider == LLMProvider.OPENAI
        # Global LLM_MODEL is used since LLM_MAP_MODEL is not set
        assert cfg.model == "claude-sonnet-4-6"


class TestConfigImmutability:
    """LLMBackendConfig is frozen — verify it cannot be mutated."""

    def test_config_is_frozen(self) -> None:
        cfg = LLMBackendConfig(
            provider=LLMProvider.ANTHROPIC,
            model="claude-sonnet-4-6",
            api_key="test",
        )
        with pytest.raises(AttributeError):
            cfg.model = "something-else"  # type: ignore[misc]
