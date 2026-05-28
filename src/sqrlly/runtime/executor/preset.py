"""Named-preset resolution.

A workflow declares ``settings.presets:`` as a dict of named execution
bundles; nodes reference one via ``params.preset:`` or inherit the
preset marked ``default: true``. The ``--preset`` CLI flag overrides
the default at run time.

This module provides:

- ``resolve_preset_name(node, settings)`` — returns the preset name to
  use for a node (``params.preset:`` if set, else the default).
- ``build_preset_registry(settings, cli_override=None)`` — master
  entry point. Returns a fully-resolved ``dict[str, Preset]`` ready
  for backend instantiation. Handles the CLI-override-flips-default
  case. Empty ``settings.presets`` is an error — presets must be
  declared explicitly; sqrlly does not synthesize defaults from the
  environment.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqrlly.schema.models import LlmPreset, Preset, Settings

if TYPE_CHECKING:
    from sqrlly.schema.models import Node


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
    if node.execute is not None:
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


def build_preset_registry(
    settings: Settings, cli_override: str | None = None,
) -> dict[str, Preset]:
    """Return the runtime preset registry for a workflow.

    sqrlly does not synthesize defaults from the local environment.
    An empty ``settings.presets`` returns an empty registry — valid
    for script-only workflows; LLM-dispatching nodes will fail at the
    call site with a clear "no prompt backend wired" message. A
    missing CLI (e.g., ``npx`` for an ACP preset) surfaces at the
    call site as a backend error with actionable remediation, not as
    a pre-flight check; this keeps sqrlly out of the business of
    probing the local toolchain and lets earlier workflow steps
    install dependencies that later steps need.

    Input shapes:

      - ``settings.presets`` empty + no ``cli_override`` → empty dict.
      - ``settings.presets`` empty + ``cli_override`` set → raises
        (the named override doesn't exist).
      - ``settings.presets`` non-empty + ``cli_override`` None →
        return ``settings.presets`` as-is (new dict; input not mutated).
      - ``settings.presets`` non-empty + ``cli_override`` set → flip
        ``default`` to the named ``LlmPreset``, clear it on the prior
        default. Raises ``ValueError`` if the named preset doesn't
        exist or names a ``CommandPreset``.
    """
    if not settings.presets:
        if cli_override is not None:
            raise ValueError(
                f"--preset {cli_override!r} given but settings.presets "
                f"is empty. Declare presets in YAML or drop the flag."
            )
        # Empty registry is valid for script-only workflows. LLM
        # dispatch will fail at the call site with a clear message
        # (DispatchExecutor's "no prompt backend wired" error) if a
        # prompt node tries to run without a declared preset.
        return {}

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
