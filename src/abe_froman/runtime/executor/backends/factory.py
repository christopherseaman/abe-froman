from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from abe_froman.runtime.result import PromptBackend

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


def _resolve_key_from_auth_json(provider: str) -> str | None:
    """Read ``~/.pi/agent/auth.json`` and extract ``{provider: {key: ...}}``.

    Returns ``None`` if the file is missing, malformed, or has no key
    for the requested provider. Shared by the per-provider resolvers
    so they don't duplicate the file-IO + JSON-parse + shape-check.
    """
    auth_path = Path.home() / ".pi" / "agent" / "auth.json"
    if not auth_path.exists():
        return None
    try:
        data = json.loads(auth_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    section = data.get(provider) if isinstance(data, dict) else None
    if isinstance(section, dict):
        key = section.get("key")
        if isinstance(key, str) and key:
            return key
    return None


def _resolve_deepseek_key() -> str | None:
    """Resolve a DeepSeek API key, env-first then on-disk auth.json.

    Priority:
      1. ``DEEPSEEK_API_KEY`` env var.
      2. ``~/.pi/agent/auth.json`` carrying ``{"deepseek": {"key": "..."}}``.

    Returns ``None`` if neither source has a key.
    """
    env_key = os.getenv("DEEPSEEK_API_KEY")
    if env_key:
        return env_key
    return _resolve_key_from_auth_json("deepseek")


def _resolve_anthropic_key() -> str | None:
    """Resolve an Anthropic API key, env-first then on-disk auth.json.

    Priority:
      1. ``ANTHROPIC_API_KEY`` env var (standard Anthropic SDK contract).
      2. ``~/.pi/agent/auth.json`` carrying ``{"anthropic": {"key": "..."}}``.

    Returns ``None`` if neither source has a key.
    """
    env_key = os.getenv("ANTHROPIC_API_KEY")
    if env_key:
        return env_key
    return _resolve_key_from_auth_json("anthropic")


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
                "(set ANTHROPIC_API_KEY or place key in "
                "~/.pi/agent/auth.json)."
            )
        return AnthropicBackend(api_key=api_key)

    if executor_type == "deepseek":
        from abe_froman.runtime.executor.backends.openai import OpenAIBackend

        api_key = kwargs.get("api_key") or _resolve_deepseek_key()
        if not api_key:
            raise ValueError(
                "DeepSeek backend requested but no API key found "
                "(set DEEPSEEK_API_KEY or place key in ~/.pi/agent/auth.json)."
            )
        return OpenAIBackend(api_key=api_key, base_url=DEEPSEEK_BASE_URL)

    if executor_type == "openai":
        from abe_froman.runtime.executor.backends.openai import OpenAIBackend

        api_key = kwargs.get("api_key") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OpenAI backend requested but no API key found "
                "(set OPENAI_API_KEY or pass api_key=...)."
            )
        return OpenAIBackend(api_key=api_key, base_url=kwargs.get("base_url"))

    raise ValueError(
        f"Unknown executor type: {executor_type!r}. "
        f"Supported: acp, anthropic, deepseek, openai"
    )


def auto_detect_executor() -> str:
    """Pick the first available real backend; raise on miss.

    Resolution order (first match wins):
      1. Anthropic key (env ``ANTHROPIC_API_KEY`` or
         ``~/.pi/agent/auth.json``) → ``"anthropic"``.
      2. DeepSeek key (env ``DEEPSEEK_API_KEY`` or
         ``~/.pi/agent/auth.json``) → ``"deepseek"``.
      3. ``npx`` on PATH → ``"acp"`` (assumes
         ``@zed-industries/claude-code-acp`` is installed).
      4. Nothing → ``RuntimeError`` naming all three remediation
         paths. There is no longer a silent fake-output fallback.

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
