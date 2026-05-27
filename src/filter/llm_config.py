"""Pluggable LLM configuration for the chaos relevance filter.

Supports multiple LLM backends. Set via environment variable or .env file.
Falls back to keyword filter if no LLM is configured.

Environment variables:
  LLM_PROVIDER=claude_code|ollama|anthropic|openai|google|none
  LLM_MODEL=qwen2.5-coder:14b  (or claude-sonnet-4-6, gpt-4o, gemini-2.5-pro, etc.)

  # Per-phase model routing (optional — falls back to LLM_PROVIDER/LLM_MODEL):
  LLM_FILTER_PROVIDER / LLM_FILTER_MODEL   — used by FILTER phase
  LLM_MAP_PROVIDER / LLM_MAP_MODEL         — used by MAP phase
  LLM_ANALYZE_PROVIDER / LLM_ANALYZE_MODEL — used by ANALYZE phase

  # Provider-specific keys (only need the one matching LLM_PROVIDER):
  OLLAMA_BASE_URL=http://localhost:11434  (default)
  ANTHROPIC_API_KEY=sk-ant-...
  OPENAI_API_KEY=sk-...
  GOOGLE_API_KEY=...
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class LLMProvider(Enum):
    NONE = "none"            # Keyword filter only
    CLAUDE_CODE = "claude_code"  # Claude Code CLI (uses existing subscription)
    OLLAMA = "ollama"        # Local Ollama (default when available)
    ANTHROPIC = "anthropic"  # Claude API (requires ANTHROPIC_API_KEY)
    OPENAI = "openai"        # OpenAI API
    GOOGLE = "google"        # Google Gemini API


@dataclass(frozen=True)
class LLMBackendConfig:
    provider: LLMProvider
    model: str
    api_key: str | None = None
    base_url: str | None = None


def _claude_code_available() -> bool:
    """Check if claude CLI is installed and responsive."""
    try:
        import shutil
        return shutil.which("claude") is not None
    except Exception:
        return False


_PHASE_ENV_PREFIXES: dict[str, str] = {
    "filter": "LLM_FILTER",
    "map": "LLM_MAP",
    "analyze": "LLM_ANALYZE",
}


def _resolve_provider_and_model(
    phase: str,
) -> tuple[str, str | None]:
    """Return (provider, model) for the given phase.

    Checks per-phase env vars first (e.g. LLM_FILTER_PROVIDER),
    then falls back to the global LLM_PROVIDER / LLM_MODEL.
    The model may be None when neither per-phase nor global is set
    (each provider branch supplies its own default in that case).
    """
    prefix = _PHASE_ENV_PREFIXES.get(phase)

    phase_provider = ""
    phase_model: str | None = None
    if prefix:
        phase_provider = os.environ.get(f"{prefix}_PROVIDER", "").lower()
        phase_model = os.environ.get(f"{prefix}_MODEL") or None

    provider = phase_provider or os.environ.get("LLM_PROVIDER", "").lower()
    model = phase_model or os.environ.get("LLM_MODEL") or None
    return provider, model


def detect_llm_backend(phase: str = "default") -> LLMBackendConfig:
    """Auto-detect the best available LLM backend from environment.

    Args:
        phase: Pipeline phase requesting an LLM.
               "filter", "map", "analyze" check per-phase env vars first
               (e.g. LLM_FILTER_PROVIDER / LLM_FILTER_MODEL).
               "default" uses global config only.

    Priority: explicit > claude_code > Anthropic key > OpenAI key > Google key > Ollama > none
    """
    explicit, phase_model = _resolve_provider_and_model(phase)

    if explicit == "claude_code" or (not explicit and _claude_code_available()):
        model = phase_model or "claude-sonnet-4-6"
        logger.info("LLM backend [%s]: Claude Code CLI (%s)", phase, model)
        return LLMBackendConfig(
            provider=LLMProvider.CLAUDE_CODE, model=model,
        )

    if explicit == "anthropic" or (not explicit and os.environ.get("ANTHROPIC_API_KEY")):
        model = phase_model or "claude-sonnet-4-6"
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            logger.info("LLM backend [%s]: Anthropic (%s)", phase, model)
            return LLMBackendConfig(
                provider=LLMProvider.ANTHROPIC, model=model, api_key=key,
            )

    if explicit == "openai" or (not explicit and os.environ.get("OPENAI_API_KEY")):
        model = phase_model or "gpt-4o"
        key = os.environ.get("OPENAI_API_KEY")
        if key and key != "not-needed":
            logger.info("LLM backend [%s]: OpenAI (%s)", phase, model)
            return LLMBackendConfig(
                provider=LLMProvider.OPENAI, model=model, api_key=key,
            )

    if explicit == "google" or (not explicit and os.environ.get("GOOGLE_API_KEY")):
        model = phase_model or "gemini-2.5-pro"
        key = os.environ.get("GOOGLE_API_KEY")
        if key:
            logger.info("LLM backend [%s]: Google (%s)", phase, model)
            return LLMBackendConfig(
                provider=LLMProvider.GOOGLE, model=model, api_key=key,
            )

    if explicit == "ollama" or not explicit:
        # Check if Ollama is running
        try:
            import urllib.request
            urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
            model = phase_model or "qwen2.5-coder:14b"
            base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
            logger.info("LLM backend [%s]: Ollama (%s)", phase, model)
            return LLMBackendConfig(
                provider=LLMProvider.OLLAMA, model=model, base_url=base_url,
            )
        except Exception:
            pass

    if explicit == "none":
        logger.info("LLM backend [%s]: none (keyword filter only)", phase)
    else:
        logger.info("No LLM backend available — using keyword filter")

    return LLMBackendConfig(provider=LLMProvider.NONE, model="")
