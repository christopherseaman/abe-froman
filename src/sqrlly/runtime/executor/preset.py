"""Named-preset resolution + auto-detect synthesis.

A workflow declares ``settings.presets:`` as a dict of named execution
bundles; nodes reference one via ``params.preset:`` or inherit the
preset marked ``default: true``. The ``--preset`` CLI flag overrides
the default at run time.

This module provides:

- ``resolve_preset_name(node, settings)`` — returns the preset name to
  use for a node (``params.preset:`` if set, else the default).
- ``auto_detect_default_preset()`` — synthesizes a Preset from
  environment keys when ``settings.presets`` is empty. Mirrors the
  legacy ``auto_detect_executor`` chain: Anthropic key → DeepSeek key
  → ACP via npx. Raises ``RuntimeError`` on miss.
- ``build_preset_registry(settings, cli_override=None)`` — master
  entry point. Returns a fully-resolved ``dict[str, Preset]`` ready
  for backend instantiation. Handles empty-presets (auto-detect) and
  CLI-override-flips-default cases.
"""
from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from sqrlly.runtime.secrets import resolve_secret
from sqrlly.schema.models import LlmPreset, Preset, Settings

if TYPE_CHECKING:
    from sqrlly.schema.models import Node
    from sqrlly.schema.params import PromptParams

_AUTO_PRESET_NAME = "_auto"


def resolve_preset_name(node: "Node", settings: Settings) -> str:
    """Return the preset name a node should use.

    Order: ``params.preset:`` on the node's execute block > the preset
    flagged ``default: true`` in ``settings.presets``. Raises
    ``ValueError`` if neither is resolvable.
    """
    # node.execute.params is a dict[str, Any] (Stage 5b shape); the
    # preset reference is the raw value at the "preset" key. We don't
    # coerce to PromptParams here — that's done at dispatch time. This
    # helper deliberately works with the raw dict so it can be called
    # at compile-time before coercion runs.
    if node.execute is not None and isinstance(node.execute.params, dict):
        node_preset = node.execute.params.get("preset")
        if node_preset is not None:
            if node_preset not in settings.presets:
                raise ValueError(
                    f"Node {node.id!r}: params.preset={node_preset!r} "
                    f"not found in settings.presets "
                    f"(declared: {sorted(settings.presets)!r})"
                )
            return node_preset

    # Fall through to the default LLM preset. The Settings validator
    # guarantees exactly one when any LlmPreset exists. ``getattr`` —
    # CommandPresets have no ``default`` attribute.
    for name, preset in settings.presets.items():
        if getattr(preset, "default", False):
            return name

    raise ValueError(
        f"Node {node.id!r}: no preset reference and no default preset "
        f"in settings.presets. Either declare a default: true preset "
        f"or set params.preset: on each node."
    )


def auto_detect_default_preset() -> LlmPreset:
    """Synthesize a default LlmPreset from environment keys.

    Resolution order (first match wins):

      1. ``ANTHROPIC_API_KEY`` → ``LlmPreset(transport=api,
         provider=anthropic, model=sonnet, default=True)``
      2. ``DEEPSEEK_API_KEY`` → ``LlmPreset(transport=api,
         provider=deepseek, model=deepseek-v4-flash, default=True)``
      3. ``npx`` on PATH → ``LlmPreset(transport=acp, provider=anthropic,
         model=sonnet, default=True)`` (assumes
         ``@zed-industries/claude-code-acp`` is installed)
      4. Nothing → ``RuntimeError`` naming all three remediation
         paths.

    Called as a fallback when ``settings.presets`` is empty and
    neither YAML nor CLI specified an explicit preset.
    """
    if resolve_secret("ANTHROPIC_API_KEY"):
        return LlmPreset(
            transport="api", provider="anthropic", model="sonnet", default=True,
        )
    if resolve_secret("DEEPSEEK_API_KEY"):
        return LlmPreset(
            transport="api", provider="deepseek",
            model="deepseek-v4-flash", default=True,
        )
    if shutil.which("npx"):
        return LlmPreset(
            transport="acp", provider="anthropic", model="sonnet", default=True,
        )
    raise RuntimeError(
        "No preset declared in settings.presets and no executor "
        "auto-detectable from environment. Set ANTHROPIC_API_KEY "
        "(recommended), set DEEPSEEK_API_KEY, install npx + "
        "@zed-industries/claude-code-acp, or declare an explicit "
        "settings.presets block."
    )


def build_preset_registry(
    settings: Settings, cli_override: str | None = None,
) -> dict[str, Preset]:
    """Return the runtime preset registry for a workflow.

    Three input cases:

      - ``settings.presets`` non-empty + ``cli_override`` None → return
        ``settings.presets`` as-is.
      - ``settings.presets`` non-empty + ``cli_override`` set → flip
        ``default`` to the named preset, clear it on the prior default.
        Raises ``ValueError`` if the named preset doesn't exist.
      - ``settings.presets`` empty → auto-detect a single default
        preset under the name ``_auto``. ``cli_override`` is ignored
        in this branch (no other presets to switch to); raise instead
        of silent-no-op so the misconfiguration surfaces.

    Returns a NEW dict — never mutates the input ``settings.presets``.
    """
    if not settings.presets:
        if cli_override is not None:
            raise ValueError(
                f"--preset {cli_override!r} given but settings.presets "
                f"is empty. Declare presets in YAML or drop the flag."
            )
        return {_AUTO_PRESET_NAME: auto_detect_default_preset()}

    if cli_override is None:
        # New dict, same Preset instances. Pydantic models aren't mutated
        # downstream, so a shallow dict copy is sufficient to give callers
        # an independent registry without paying for per-element model_copy.
        return dict(settings.presets)

    if cli_override not in settings.presets:
        raise ValueError(
            f"--preset {cli_override!r} not found in settings.presets "
            f"(declared: {sorted(settings.presets)!r})"
        )
    if not isinstance(settings.presets[cli_override], LlmPreset):
        raise ValueError(
            f"--preset {cli_override!r} names a command preset; the "
            f"flag selects the default LLM preset and only applies to "
            f"kind=llm presets."
        )

    # Flip the `default` flag so cli_override wins. Only LlmPresets carry
    # `default` — CommandPresets pass through untouched. model_copy keeps
    # the caller's Settings instance unmutated.
    def _flip(name: str, preset: Preset) -> Preset:
        if isinstance(preset, LlmPreset):
            return preset.model_copy(update={"default": name == cli_override})
        return preset

    return {name: _flip(name, p) for name, p in settings.presets.items()}
