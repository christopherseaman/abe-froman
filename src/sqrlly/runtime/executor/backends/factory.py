from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from sqrlly.runtime.result import PromptBackend
from sqrlly.runtime.secrets import resolve_secret

if TYPE_CHECKING:
    from sqrlly.schema.models import Preset

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
        from sqrlly.runtime.executor.backends.acp import ACPBackend

        return ACPBackend(
            program=kwargs.get("program", "npx"),
            args=kwargs.get("args", ("@zed-industries/claude-code-acp",)),
        )

    if executor_type == "anthropic":
        from sqrlly.runtime.executor.backends.anthropic import (
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
        from sqrlly.runtime.executor.backends.openai import OpenAIBackend

        api_key = kwargs.get("api_key") or _resolve_deepseek_key()
        if not api_key:
            raise ValueError(
                "DeepSeek backend requested but no API key found "
                "(set DEEPSEEK_API_KEY in the environment; see .env.example)."
            )
        return OpenAIBackend(api_key=api_key, base_url=DEEPSEEK_BASE_URL)

    if executor_type == "openai":
        from sqrlly.runtime.executor.backends.openai import OpenAIBackend

        api_key = kwargs.get("api_key") or resolve_secret("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OpenAI backend requested but no API key found "
                "(set OPENAI_API_KEY in the environment; see .env.example)."
            )
        # ``OPENAI_API_KEY`` is reserved for *real* OpenAI here.
        # For OpenAI-compatible third parties (OpenRouter, Ollama,
        # LM Studio, LiteLLM, Azure OpenAI, vLLM, ...), use
        # ``--executor custom`` with ``CUSTOM_API_KEY`` +
        # ``CUSTOM_API_BASE_URL``. Power-user override:
        # ``OPENAI_BASE_URL`` is honored when set, but ``custom``
        # is the canonical path for non-OpenAI endpoints.
        base_url = kwargs.get("base_url") or resolve_secret("OPENAI_BASE_URL")
        return OpenAIBackend(api_key=api_key, base_url=base_url)

    if executor_type == "custom":
        from sqrlly.runtime.executor.backends.openai import OpenAIBackend

        api_key = kwargs.get("api_key") or resolve_secret("CUSTOM_API_KEY")
        base_url = (
            kwargs.get("base_url") or resolve_secret("CUSTOM_API_BASE_URL")
        )
        if not api_key:
            raise ValueError(
                "Custom OpenAI-compatible backend requested but no API key "
                "found (set CUSTOM_API_KEY in the environment; "
                "see .env.example)."
            )
        if not base_url:
            raise ValueError(
                "Custom OpenAI-compatible backend requested but no base "
                "URL configured (set CUSTOM_API_BASE_URL in the "
                "environment, e.g. https://openrouter.ai/api/v1)."
            )
        return OpenAIBackend(api_key=api_key, base_url=base_url)

    raise ValueError(
        f"Unknown executor type: {executor_type!r}. "
        f"Supported: acp, anthropic, custom, deepseek, openai"
    )


def create_backend_from_preset(preset: "Preset") -> PromptBackend:
    """Instantiate a PromptBackend matching the preset's transport+provider.

    The preset's ``model`` is consulted per-call via ``send_prompt(model=...)``;
    backends themselves are stateless w.r.t. model selection, so two presets
    differing only in model share an instance shape (different presets =
    different backend instances at the registry level for lifecycle clarity).
    """
    if preset.transport == "acp":
        from sqrlly.runtime.executor.backends.acp import ACPBackend
        return ACPBackend(
            program="npx",
            args=("@zed-industries/claude-code-acp",),
        )

    # transport == "api"
    if preset.provider == "anthropic":
        from sqrlly.runtime.executor.backends.anthropic import AnthropicBackend
        api_key = _resolve_anthropic_key()
        if not api_key:
            raise ValueError(
                "Preset (transport=api, provider=anthropic) requires "
                "ANTHROPIC_API_KEY in the environment or .env"
            )
        return AnthropicBackend(api_key=api_key)

    if preset.provider == "openai":
        from sqrlly.runtime.executor.backends.openai import OpenAIBackend
        api_key = resolve_secret("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "Preset (transport=api, provider=openai) requires "
                "OPENAI_API_KEY in the environment or .env"
            )
        base_url = resolve_secret("OPENAI_BASE_URL")
        return OpenAIBackend(api_key=api_key, base_url=base_url)

    if preset.provider == "deepseek":
        from sqrlly.runtime.executor.backends.openai import OpenAIBackend
        api_key = _resolve_deepseek_key()
        if not api_key:
            raise ValueError(
                "Preset (transport=api, provider=deepseek) requires "
                "DEEPSEEK_API_KEY in the environment or .env"
            )
        return OpenAIBackend(api_key=api_key, base_url=DEEPSEEK_BASE_URL)

    if preset.provider == "custom":
        from sqrlly.runtime.executor.backends.openai import OpenAIBackend
        api_key = resolve_secret("CUSTOM_API_KEY")
        if not api_key:
            raise ValueError(
                "Preset (transport=api, provider=custom) requires "
                "CUSTOM_API_KEY in the environment or .env"
            )
        # preset.base_url is the canonical place; CUSTOM_API_BASE_URL
        # env var stays as a fallback for the no-YAML-presets path.
        base_url = preset.base_url or resolve_secret("CUSTOM_API_BASE_URL")
        if not base_url:
            raise ValueError(
                "Preset (transport=api, provider=custom) requires "
                "preset.base_url OR CUSTOM_API_BASE_URL in the environment"
            )
        return OpenAIBackend(api_key=api_key, base_url=base_url)

    raise ValueError(
        f"Unsupported preset shape: transport={preset.transport!r}, "
        f"provider={preset.provider!r}"
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
