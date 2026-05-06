from __future__ import annotations

import shutil

from abe_froman.runtime.result import PromptBackend
from abe_froman.runtime.secrets import resolve_secret

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


def _resolve_deepseek_key() -> str | None:
    """Thin wrapper for backward compatibility / readability — the
    real resolution chain lives in ``runtime/secrets.py``."""
    return resolve_secret("DEEPSEEK_API_KEY")


def _resolve_anthropic_key() -> str | None:
    """Thin wrapper for backward compatibility / readability — the
    real resolution chain lives in ``runtime/secrets.py``."""
    return resolve_secret("ANTHROPIC_API_KEY")


def create_prompt_backend(executor_type: str, **kwargs: object) -> PromptBackend:
    """Create a PromptBackend instance from a type identifier.

    Supported types:
    - "acp": ACP via claude-code-acp adapter
    - "anthropic": Direct Anthropic Messages API
    - "deepseek": OpenAI-compatible backend pointed at DeepSeek
    - "openai": OpenAI-compatible backend (caller supplies key/base_url)
    """
    if executor_type == "acp":
        from abe_froman.runtime.executor.backends.acp import ACPBackend

        return ACPBackend(
            program=kwargs.get("program", "npx"),
            args=kwargs.get("args", ("@zed-industries/claude-code-acp",)),
        )

    if executor_type == "anthropic":
        from abe_froman.runtime.executor.backends.anthropic import (
            AnthropicBackend,
        )

        api_key = kwargs.get("api_key") or _resolve_anthropic_key()
        if not api_key:
            raise ValueError(
                "Anthropic backend requested but no API key found "
                "(set ANTHROPIC_API_KEY in the environment; see .env.example)."
            )
        return AnthropicBackend(api_key=api_key)

    if executor_type == "deepseek":
        from abe_froman.runtime.executor.backends.openai import OpenAIBackend

        api_key = kwargs.get("api_key") or _resolve_deepseek_key()
        if not api_key:
            raise ValueError(
                "DeepSeek backend requested but no API key found "
                "(set DEEPSEEK_API_KEY in the environment; see .env.example)."
            )
        return OpenAIBackend(api_key=api_key, base_url=DEEPSEEK_BASE_URL)

    if executor_type == "openai":
        from abe_froman.runtime.executor.backends.openai import OpenAIBackend

        api_key = kwargs.get("api_key") or resolve_secret("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OpenAI backend requested but no API key found "
                "(set OPENAI_API_KEY in the environment; see .env.example)."
            )
        # ``OPENAI_BASE_URL`` lets OpenAI-compatible providers work
        # with the same backend without a code change. Examples:
        #   OpenRouter:  https://openrouter.ai/api/v1
        #   Ollama:      http://localhost:11434/v1
        #   LM Studio:   http://localhost:1234/v1
        #   LiteLLM:     http://localhost:4000
        # Set OPENAI_API_KEY to the provider's key and OPENAI_BASE_URL
        # to its endpoint.
        base_url = kwargs.get("base_url") or resolve_secret("OPENAI_BASE_URL")
        return OpenAIBackend(api_key=api_key, base_url=base_url)

    raise ValueError(
        f"Unknown executor type: {executor_type!r}. "
        f"Supported: acp, anthropic, deepseek, openai"
    )


def auto_detect_executor() -> str:
    """Pick the first available real backend; raise on miss.

    Resolution order (first match wins):
      1. ``ANTHROPIC_API_KEY`` env var → ``"anthropic"``.
      2. ``DEEPSEEK_API_KEY`` env var → ``"deepseek"``.
      3. ``npx`` on PATH → ``"acp"`` (assumes
         ``@zed-industries/claude-code-acp`` is installed).
      4. Nothing → ``RuntimeError`` naming all three remediation
         paths. There is no silent fake-output fallback.

    Only called from the CLI as a *fallback* when neither ``--executor``
    nor ``settings.executor`` was set.
    """
    if _resolve_anthropic_key():
        return "anthropic"
    if _resolve_deepseek_key():
        return "deepseek"
    if shutil.which("npx"):
        return "acp"
    raise RuntimeError(
        "No prompt backend available. Set ANTHROPIC_API_KEY (recommended), "
        "set DEEPSEEK_API_KEY, or install npx + "
        "@zed-industries/claude-code-acp. Pass --executor explicitly "
        "to override auto-detect."
    )
