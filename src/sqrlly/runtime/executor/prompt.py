from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from jinja2 import Template

from sqrlly.runtime.executor.preset import resolve_preset_name
from sqrlly.runtime.result import ExecutionResult, OverloadError, PromptBackend
from sqrlly.schema.models import LlmPreset, Node, Settings


def resolve_model(node: Node, settings: Settings) -> str | None:
    """Pick the declared LLM model for a node — used by foreman for
    per-model semaphore selection.

    Returns ``None`` (foreman → "no per-model semaphore") whenever no
    LLM model applies:
      - ``settings.presets`` is empty;
      - the node resolves to a ``CommandPreset`` (a script node — no
        model);
      - the node has no ``params.preset`` and there is no default LLM
        preset (a script node in a command-preset-only workflow —
        ``resolve_preset_name`` raises, caught here).
    """
    if not settings.presets:
        return None
    try:
        preset = settings.presets[resolve_preset_name(node, settings)]
    except ValueError:
        return None
    return preset.model if isinstance(preset, LlmPreset) else None


# Tier list for OverloadError auto-downgrade. Fixed — the chain mirrors
# Anthropic's model tiers, not a per-workflow knob.
MODEL_DOWNGRADE_CHAIN = ["opus", "sonnet", "haiku"]


def downgrade_model(current: str, chain: list[str]) -> str | None:
    try:
        idx = chain.index(current)
    except ValueError:
        return None
    if idx + 1 < len(chain):
        return chain[idx + 1]
    return None


def retry_delay(attempt: int, backoff: list[float]) -> float:
    """Seconds to sleep before retry ``attempt`` (1-indexed).

    Clamps to the last value past the list length; 0.0 when ``backoff``
    is empty. The single clamp shared by gate retries (compile layer —
    the allowed import direction is compile → runtime) and backend
    retries here.
    """
    if not backoff:
        return 0.0
    idx = min(attempt - 1, len(backoff) - 1)
    return backoff[idx]


def render_template(template: str, context: dict[str, Any]) -> str:
    return Template(template, keep_trailing_newline=True).render(**context)


def prepend_eval_preamble(rendered: str, context: dict[str, Any]) -> str:
    """Stage 5c: auto-prepend the eval preamble for inline-route goto
    targets that opted in via ``include_eval: true``.

    The synthetic ``_route_<id>`` writes the preamble string into state
    when the matched case sets ``include_eval=True``; ``build_context``
    surfaces it as ``_route_eval_preamble``. Concatenation happens
    AFTER Jinja render so author-template content is preserved verbatim
    — preamble appears as a system-style block before the body. No-op
    when the key is absent or empty.

    Pure helper — extracted so the auto-prepend behavior is unit-
    testable without instrumenting the PromptBackend boundary.
    """
    eval_preamble = context.get("_route_eval_preamble")
    if not eval_preamble:
        return rendered
    return f"{eval_preamble}\n\n{rendered}"


def apply_preamble(
    template: str, *, settings: Settings, base_workdir: str,
) -> str:
    """Prepend ``settings.preamble_file`` if configured.

    Returns the modified template (or the original if no preamble is
    configured). Raises ``FileNotFoundError`` with the resolved path if
    the configured preamble file is missing — caller translates to
    ``ExecutionResult`` once. ``base_workdir`` is the DispatchExecutor's
    BASE workdir: the preamble lives with the config, never in a
    per-node worktree. ``settings`` is scope-aware — pass the merged
    settings so a subgraph's preamble_file is honored for its nodes.
    """
    if not settings.preamble_file:
        return template
    preamble_path = Path(base_workdir) / settings.preamble_file
    return preamble_path.read_text() + "\n\n" + template


async def execute_with_downgrade(
    backend: PromptBackend,
    rendered: str,
    model: str,
    workdir: str,
    timeout: float | None = None,
    *,
    settings: Settings,
) -> ExecutionResult:
    """Send a pre-rendered prompt with overload→downgrade fallback.

    ``settings`` is scope-aware — provides ``backend_max_retries`` /
    ``retry_backoff`` for this scope (subgraph wrappers pass the
    merged settings).

    Bounded transient-retry layer wrapping the overload-downgrade loop:
    a non-OverloadError backend exception (e.g. the CLI backend's
    `claude exited 1`) re-enters the downgrade loop from the ORIGINAL
    model up to `backend_max_retries` times. OverloadError stays inside
    the inner loop (model downgrade) and never consumes a backend
    retry — overload exhaustion returns from inside the inner loop, so
    it never reaches the outer `except Exception`. attempt 0 = first try.
    """
    attempt = 0
    while True:
        current_model = model
        try:
            while True:
                try:
                    result = await backend.send_prompt(
                        rendered, current_model, workdir, timeout=timeout,
                    )
                    break
                except OverloadError:
                    next_model = downgrade_model(
                        current_model, MODEL_DOWNGRADE_CHAIN
                    )
                    if next_model is None:
                        return ExecutionResult(
                            success=False,
                            error=(
                                f"API overloaded, exhausted model chain "
                                f"(last: {current_model})"
                            ),
                        )
                    current_model = next_model
            break
        except Exception as e:
            if attempt >= settings.backend_max_retries:
                return ExecutionResult(
                    success=False, error=f"Backend error: {e}"
                )
            attempt += 1
            delay = retry_delay(attempt, settings.retry_backoff)
            if delay > 0:
                await asyncio.sleep(delay)

    return ExecutionResult(
        success=True,
        output=result.output,
        structured_output=result.structured_output,
    )
