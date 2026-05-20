from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from sqrlly.runtime.executor.backends.acp import ACPBackend
from sqrlly.runtime.executor.backends.anthropic import AnthropicBackend
from sqrlly.runtime.executor.backends.openai import OpenAIBackend
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


def _build_acp(_preset: "Preset") -> PromptBackend:
    return ACPBackend(
        program="npx",
        args=("@zed-industries/claude-code-acp",),
    )


def _build_anthropic_api(_preset: "Preset") -> PromptBackend:
    api_key = _resolve_anthropic_key()
    if not api_key:
        raise ValueError(
            "Preset (transport=api, provider=anthropic) requires "
            "ANTHROPIC_API_KEY in the environment or .env"
        )
    return AnthropicBackend(api_key=api_key)


def _build_openai_api(_preset: "Preset") -> PromptBackend:
    api_key = resolve_secret("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "Preset (transport=api, provider=openai) requires "
            "OPENAI_API_KEY in the environment or .env"
        )
    base_url = resolve_secret("OPENAI_BASE_URL")
    return OpenAIBackend(api_key=api_key, base_url=base_url)


def _build_deepseek_api(_preset: "Preset") -> PromptBackend:
    api_key = _resolve_deepseek_key()
    if not api_key:
        raise ValueError(
            "Preset (transport=api, provider=deepseek) requires "
            "DEEPSEEK_API_KEY in the environment or .env"
        )
    return OpenAIBackend(api_key=api_key, base_url=DEEPSEEK_BASE_URL)


def _build_custom_api(preset: "Preset") -> PromptBackend:
    api_key = resolve_secret("CUSTOM_API_KEY")
    if not api_key:
        raise ValueError(
            "Preset (transport=api, provider=custom) requires "
            "CUSTOM_API_KEY in the environment or .env"
        )
    # ``preset.api_base_url`` is the canonical place; ``CUSTOM_API_BASE_URL``
    # env var stays as a fallback for the auto-detect path that
    # synthesizes presets without an authored endpoint.
    api_base_url = preset.api_base_url or resolve_secret("CUSTOM_API_BASE_URL")
    if not api_base_url:
        raise ValueError(
            "Preset (transport=api, provider=custom) requires "
            "preset.api_base_url OR CUSTOM_API_BASE_URL in the environment"
        )
    return OpenAIBackend(api_key=api_key, base_url=api_base_url)


# (transport, provider) → builder. Exhaustive — the schema validator
# (``Preset._validate_combinations`` + ``Literal`` field types) ensures
# every parseable Preset matches one row. New transports/providers add
# one row + one builder.
_BACKEND_BUILDERS: dict[tuple[str, str], Callable[["Preset"], PromptBackend]] = {
    ("acp", "anthropic"): _build_acp,
    ("api", "anthropic"): _build_anthropic_api,
    ("api", "openai"):    _build_openai_api,
    ("api", "deepseek"):  _build_deepseek_api,
    ("api", "custom"):    _build_custom_api,
}


def create_backend_from_preset(preset: "Preset") -> PromptBackend:
    """Instantiate a PromptBackend matching the preset's transport+provider.

    The preset's ``model`` is consulted per-call via ``send_prompt(model=...)``;
    backends are stateless w.r.t. model selection. Two presets differing only
    in model share an instance shape, but each preset gets its own backend
    instance at the registry level for lifecycle clarity (separate ``close()``
    paths, separate semaphore accounting if added later).
    """
    return _BACKEND_BUILDERS[(preset.transport, preset.provider)](preset)
