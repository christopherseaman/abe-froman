from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from sqrlly.runtime.executor.backends.acp import ACPBackend
from sqrlly.runtime.executor.backends.cli import CLIBackend
from sqrlly.runtime.result import PromptBackend

if TYPE_CHECKING:
    from sqrlly.schema.models import LlmPreset


def _build_acp(_preset: "LlmPreset") -> PromptBackend:
    return ACPBackend(
        program="npx",
        args=("@zed-industries/claude-code-acp",),
    )


def _build_cli(_preset: "LlmPreset") -> PromptBackend:
    return CLIBackend(argv_prefix=("claude", "-p"))


# (transport, provider) → builder. Restoring or adding a transport means
# a new row + builder; the schema literal in ``LlmPreset.transport`` must
# also list it so YAML validation accepts the value.
_BACKEND_BUILDERS: dict[tuple[str, str], Callable[["LlmPreset"], PromptBackend]] = {
    ("acp", "anthropic"): _build_acp,
    ("cli", "anthropic"): _build_cli,
}


def create_backend_from_preset(preset: "LlmPreset") -> PromptBackend:
    """Instantiate a PromptBackend matching the preset's transport+provider.

    The preset's ``model`` is consulted per-call via ``send_prompt(model=...)``;
    backends are stateless w.r.t. model selection. Two presets differing only
    in model share an instance shape, but each preset gets its own backend
    instance at the registry level for lifecycle clarity (separate ``close()``
    paths, separate semaphore accounting if added later).
    """
    return _BACKEND_BUILDERS[(preset.transport, preset.provider)](preset)
