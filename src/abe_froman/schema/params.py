"""Per-mode params dataclasses for Stage 5b's `execute: { url, params }` shape.

Each handler mode (prompt, subgraph, script, exec) accepts a mode-
specific `params:` shape. Defining them as Pydantic models means typos
(`arg:` vs `args:`, `model_name:` vs `model:`) fail at compile time
rather than silently dropping into a generic dict.

The resolver `params_for_url` picks the right model based on the
resolved URL's extension/scheme. Schema validation on `Execute.params`
(in models.py) coerces the raw dict into the matching model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict


class _StrictParams(BaseModel):
    """Reject extra keys — typos surface as ValidationError, not silent drop."""
    model_config = ConfigDict(extra="forbid")


class PromptParams(_StrictParams):
    """Params for prompt mode (`*.md`, `*.txt`, `*.prompt`)."""
    model: str | None = None
    agent: str | None = None
    timeout: float | None = None


class SubgraphParams(_StrictParams):
    """Params for subgraph mode (`*.yaml`, `*.yml`)."""
    inputs: dict[str, str] = {}
    outputs: dict[str, str] = {}


class SubprocessParams(_StrictParams):
    """Params for any subprocess mode — script (interpreter-dispatched)
    or direct-exec (binary path / unrecognized extension). Both paths
    use ``args`` + ``env`` and run through the same shared
    ``DispatchExecutor._run_subprocess``.
    """
    args: list[str] = []
    env: dict[str, str] = {}


_PROMPT_EXTS = {".md", ".txt", ".prompt"}
_SUBGRAPH_EXTS = {".yaml", ".yml"}
SCRIPT_EXTS = {".py", ".js", ".mjs", ".ts", ".sh"}


# Mode-name → params dataclass. Used when ``execute.mode:`` is set,
# overriding extension-driven routing in `params_for_url`.
_MODE_TO_PARAMS: dict[str, type[_StrictParams]] = {
    "prompt": PromptParams,
    "subgraph": SubgraphParams,
    "exec": SubprocessParams,
    "python": SubprocessParams,
    "node": SubprocessParams,
    "tsx": SubprocessParams,
    "bash": SubprocessParams,
}


def params_for_url(
    resolved_url: str, mode: str | None = None,
) -> type[_StrictParams]:
    """Pick the params dataclass that matches the resolved URL's mode.

    When ``mode`` is set (from ``execute.mode:``), it overrides the
    URL-extension lookup. Otherwise, extension lookup is case-insensitive;
    unknown extensions and bare binary paths fall through to
    ``SubprocessParams`` (script + exec collapsed in Stage 5b cleanup).
    """
    if mode is not None:
        return _MODE_TO_PARAMS[mode]
    parts = urlsplit(resolved_url)
    ext = Path(parts.path).suffix.lower()
    if ext in _PROMPT_EXTS:
        return PromptParams
    if ext in _SUBGRAPH_EXTS:
        return SubgraphParams
    return SubprocessParams


def coerce_params(
    resolved_url: str, raw: dict[str, Any], mode: str | None = None,
) -> _StrictParams:
    """Coerce a raw params dict into the matching mode's model.

    Raises pydantic.ValidationError on mode-mismatched keys.
    """
    return params_for_url(resolved_url, mode=mode)(**raw)
