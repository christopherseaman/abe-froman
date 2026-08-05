from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from sqrlly.runtime.executor.backends.cli import CLIBackend
from sqrlly.runtime.result import PromptBackend

if TYPE_CHECKING:
    from sqrlly.schema.models import LlmPreset


def _build_acp(preset: "LlmPreset") -> PromptBackend:
    # Lazy import — the `acp` Python package is the `[acp]` optional
    # extra. Keeping this import inside the builder means
    # `pip install sqrlly` (cli-only) loads the factory without
    # needing the `acp` package present, and (since backends are built
    # lazily on first dispatch) a declared-but-unused acp preset never
    # reaches here. A genuinely missing dependency surfaces at the
    # dispatching node as an actionable error, not a bare ImportError.
    try:
        from sqrlly.runtime.executor.backends.acp import ACPBackend
    except ImportError as e:
        raise RuntimeError(
            "transport: acp requires the optional 'acp' dependency, which "
            "is not installed. Install it with 'pip install sqrlly[acp]' "
            "(or 'uv tool install sqrlly --with agent-client-protocol'), or "
            "switch the preset to transport: cli."
        ) from e

    return ACPBackend(
        program="npx",
        args=("@zed-industries/claude-code-acp",),
        permission_mode=preset.permission_mode,
        allowed_tools=preset.allowed_tools,
        disallowed_tools=preset.disallowed_tools,
        env=preset.env,
    )


def _build_claude_cli(preset: "LlmPreset") -> PromptBackend:
    return CLIBackend(
        argv_prefix=("claude", "-p"),
        permission_mode=preset.permission_mode,
        allowed_tools=preset.allowed_tools,
        disallowed_tools=preset.disallowed_tools,
        cli_args=preset.cli_args,
        env=preset.env,
    )


def _build_codex_cli(preset: "LlmPreset") -> PromptBackend:
    return CLIBackend(
        argv_prefix=("codex", "exec", "--skip-git-repo-check"),
        prompt_arg="-",
        cli_args=preset.cli_args,
        env=preset.env,
    )


# (transport, provider) → builder. Restoring or adding a transport means
# a new row + builder; the schema literal in ``LlmPreset.transport`` must
# also list it so YAML validation accepts the value.
_BACKEND_BUILDERS: dict[tuple[str, str], Callable[["LlmPreset"], PromptBackend]] = {
    ("acp", "anthropic"): _build_acp,
    ("cli", "anthropic"): _build_claude_cli,
    ("cli", "openai"): _build_codex_cli,
}


def create_backend_from_preset(
    preset: "LlmPreset", *, safe_mode: bool = False,
) -> PromptBackend:
    """Instantiate a PromptBackend matching the preset's transport+provider.

    The preset's ``model`` is consulted per-call via ``send_prompt(model=...)``;
    backends are stateless w.r.t. model selection. Two presets differing only
    in model share an instance shape, but each preset gets its own backend
    instance at the registry level for lifecycle clarity (separate ``close()``
    paths, separate semaphore accounting if added later).

    ``safe_mode`` appends ``--safe-mode`` to a cli backend's argv, so the
    invocation ignores the operator's Claude customizations (output styles,
    CLAUDE.md, skills, MCP, hooks) — keeping workflow generation reproducible
    and free of e.g. an "explanatory" output style leaking commentary into
    node output. It is a ``claude`` CLI flag with no acp equivalent, so it
    only applies to ``transport: cli, provider: anthropic`` presets.
    """
    if (
        safe_mode
        and preset.transport == "cli"
        and preset.provider == "anthropic"
    ):
        existing = preset.cli_args or []
        if "--safe-mode" not in existing:
            preset = preset.model_copy(
                update={"cli_args": [*existing, "--safe-mode"]},
            )
    return _BACKEND_BUILDERS[(preset.transport, preset.provider)](preset)
