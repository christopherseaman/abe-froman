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


def downgrade_model(current: str, chain: list[str]) -> str | None:
    try:
        idx = chain.index(current)
    except ValueError:
        return None
    if idx + 1 < len(chain):
        return chain[idx + 1]
    return None


def _backend_retry_delay(attempt: int, backoff: list[float]) -> float:
    """Seconds to sleep before backend-retry ``attempt`` (1-indexed).

    Clamps to the last value past the list length; 0.0 when ``backoff``
    is empty. Mirrors ``compile/nodes._get_retry_delay`` so gate and
    backend retries share the same author intuition — duplicated rather
    than imported because ``runtime/`` must not import ``compile/`` (layer
    rule), and the function is a two-line clamp.
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


class PromptExecutor:
    """Renders prompt templates, resolves models, delegates to a PromptBackend.

    Used by DispatchExecutor's `_dispatch_prompt`: callers fetch the
    prompt body, apply preamble, render Jinja, then call
    `execute_rendered` for the overload-downgrade loop.
    """

    def __init__(self, backend: PromptBackend, settings: Settings, workdir: str = "."):
        self._backend = backend
        self._settings = settings
        self._workdir = workdir

    def apply_preamble(
        self, template: str, *, settings: Settings | None = None,
    ) -> str:
        """Prepend ``settings.preamble_file`` if configured.

        Returns the modified template (or the original if no preamble
        is configured). Raises ``FileNotFoundError`` with the resolved
        path if the configured preamble file is missing — caller
        translates to ``ExecutionResult`` once. Preamble lives with the
        config (base workdir), not in any per-node worktree.

        ``settings`` (Phase 3 / scope-aware): when provided, used in
        place of ``self._settings`` so a subgraph's preamble_file is
        honored for nodes inside that subgraph.
        """
        s = settings or self._settings
        if not s.preamble_file:
            return template
        preamble_path = Path(self._workdir) / s.preamble_file
        return preamble_path.read_text() + "\n\n" + template

    async def execute_rendered(
        self,
        rendered: str,
        model: str,
        workdir: str,
        timeout: float | None = None,
        *,
        settings: Settings | None = None,
    ) -> ExecutionResult:
        """Send a pre-rendered prompt with overload→downgrade fallback.

        ``settings`` (Phase 3 / scope-aware): provides the
        ``model_downgrade_chain`` for this scope. Subgraph wrappers pass
        the merged settings so a subgraph-specific chain is honored.
        """
        s = settings or self._settings
        # Bounded transient-retry layer wrapping the overload-downgrade loop.
        # A non-OverloadError backend exception (e.g. the CLI backend's
        # `claude exited 1`) re-enters the downgrade loop from the ORIGINAL
        # model up to `backend_max_retries` times. OverloadError stays inside
        # the inner loop (model downgrade) and never consumes a backend
        # retry — overload exhaustion returns from inside the inner loop, so
        # it never reaches `except Exception` here. attempt 0 = first try.
        attempt = 0
        while True:
            current_model = model
            try:
                while True:
                    try:
                        result = await self._backend.send_prompt(
                            rendered, current_model, workdir, timeout=timeout,
                        )
                        break
                    except OverloadError:
                        next_model = downgrade_model(
                            current_model, s.model_downgrade_chain
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
                if attempt >= s.backend_max_retries:
                    return ExecutionResult(
                        success=False, error=f"Backend error: {e}"
                    )
                attempt += 1
                delay = _backend_retry_delay(attempt, s.retry_backoff)
                if delay > 0:
                    await asyncio.sleep(delay)

        return ExecutionResult(
            success=True,
            output=result.output,
            structured_output=result.structured_output,
        )

    async def close(self) -> None:
        await self._backend.close()
