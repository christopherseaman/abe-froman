from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Template

from sqrlly.runtime.result import ExecutionResult, OverloadError, PromptBackend
from sqrlly.schema.models import Node, Settings


def resolve_model(node: Node, settings: Settings) -> str:
    """Pick the model for a node: node.model overrides settings.default_model.

    Used by foreman for per-model-semaphore selection. PromptParams.model
    is a runtime-only override (handled in dispatch._dispatch_prompt) and
    is not visible here — foreman reserves the slot for the *declared*
    model, not the runtime override.
    """
    return node.model or settings.default_model


def downgrade_model(current: str, chain: list[str]) -> str | None:
    try:
        idx = chain.index(current)
    except ValueError:
        return None
    if idx + 1 < len(chain):
        return chain[idx + 1]
    return None


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
        except Exception as e:
            return ExecutionResult(success=False, error=f"Backend error: {e}")

        return ExecutionResult(
            success=True,
            output=result.output,
            structured_output=result.structured_output,
        )

    async def close(self) -> None:
        await self._backend.close()
