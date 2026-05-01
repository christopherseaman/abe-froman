from __future__ import annotations

import json
import os
import shutil
import warnings
from pathlib import Path

from abe_froman.runtime.result import PromptBackend

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


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
    auth_path = Path.home() / ".pi" / "agent" / "auth.json"
    if not auth_path.exists():
        return None
    try:
        data = json.loads(auth_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    deepseek = data.get("deepseek") if isinstance(data, dict) else None
    if isinstance(deepseek, dict):
        key = deepseek.get("key")
        if isinstance(key, str) and key:
            return key
    return None


def create_prompt_backend(executor_type: str, **kwargs: object) -> PromptBackend:
    """Create a PromptBackend instance from a type identifier.

    Supported types:
    - "stub": placeholder backend (default, no external dependencies)
    - "acp": ACP via claude-code-acp adapter
    - "deepseek": OpenAI-compatible backend pointed at DeepSeek
    - "openai": OpenAI-compatible backend (caller supplies key/base_url)
    """
    if executor_type == "stub":
        from abe_froman.runtime.executor.backends.stub import StubBackend

        return StubBackend()

    if executor_type == "acp":
        from abe_froman.runtime.executor.backends.acp import ACPBackend

        return ACPBackend(
            program=kwargs.get("program", "npx"),
            args=kwargs.get("args", ("@zed-industries/claude-code-acp",)),
        )

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
        f"Supported: stub, acp, deepseek, openai"
    )


def auto_detect_executor() -> str:
    """Pick the first available real backend; warn + return ``"stub"``
    on miss.

    Resolution order (first match wins):
      1. ``ANTHROPIC_API_KEY`` env → ``"anthropic"`` (placeholder; the
         backend itself is not yet wired and ``create_prompt_backend``
         will raise ``ValueError`` on use. Listed first to document
         intent for the eventual native backend.)
      2. DeepSeek key on disk or env → ``"deepseek"``.
      3. ``npx`` on PATH → ``"acp"``.
      4. Nothing → ``"stub"`` with a UserWarning so the operator knows
         prompt nodes will produce fake output.

    Only called from the CLI as a *fallback* when neither ``--executor``
    nor ``settings.executor`` was set. Explicit ``--executor stub`` or
    ``executor: stub`` in YAML never triggers this function — and so
    never emits the warning.
    """
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if _resolve_deepseek_key():
        return "deepseek"
    if shutil.which("npx"):
        return "acp"
    warnings.warn(
        "No real backend detected (no ANTHROPIC_API_KEY, no "
        "DEEPSEEK_API_KEY, no npx on PATH); falling back to stub. "
        "Prompt nodes will produce fake output. Set DEEPSEEK_API_KEY "
        "or install npx + @zed-industries/claude-code-acp.",
        stacklevel=2,
    )
    return "stub"
