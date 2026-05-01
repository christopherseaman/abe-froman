from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Template

from abe_froman.runtime.result import ExecutionResult, OverloadError, PromptBackend
from abe_froman.schema.models import Node, PromptExecution, Settings


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


def resolve_model(node: Node, settings: Settings) -> str:
    return node.model or settings.default_model


class PromptExecutor:
    """Renders prompt templates, resolves models, delegates to a PromptBackend."""

    def __init__(self, backend: PromptBackend, settings: Settings, workdir: str = "."):
        self._backend = backend
        self._settings = settings
        self._workdir = workdir

    def apply_preamble(self, template: str) -> str | ExecutionResult:
        """Prepend ``settings.preamble_file`` if configured.

        Returns the modified template, or an ExecutionResult on error.
        Preamble lives with the config (base workdir), not in any per-node
        worktree.
        """
        if not self._settings.preamble_file:
            return template
        preamble_path = Path(self._workdir) / self._settings.preamble_file
        try:
            preamble = preamble_path.read_text()
        except FileNotFoundError:
            return ExecutionResult(
                success=False,
                error=f"Preamble file not found: {preamble_path}",
            )
        return preamble + "\n\n" + template

    async def execute_rendered(
        self,
        rendered: str,
        model: str,
        workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        """Send a pre-rendered prompt with overload→downgrade fallback.

        Single source of truth for the model-downgrade chain so callers
        (the legacy execute() path, the Stage-5b dispatch, and any future
        entry points) share one loop.
        """
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
                        current_model, self._settings.model_downgrade_chain
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

    async def execute(
        self, node: Node, context: dict[str, Any], workdir: str | None = None
    ) -> ExecutionResult:
        if not isinstance(node.execution, PromptExecution):
            return ExecutionResult(
                success=False,
                error=f"PromptExecutor requires PromptExecution, got {type(node.execution).__name__}",
            )

        effective_workdir = workdir or self._workdir
        prompt_path = Path(effective_workdir) / node.execution.prompt_file
        try:
            template = prompt_path.read_text()
        except FileNotFoundError:
            return ExecutionResult(
                success=False,
                error=f"Prompt file not found: {prompt_path}",
            )

        applied = self.apply_preamble(template)
        if isinstance(applied, ExecutionResult):
            return applied
        template = applied

        rendered = render_template(template, context)
        return await self.execute_rendered(
            rendered,
            resolve_model(node, self._settings),
            effective_workdir,
            timeout=node.effective_timeout(self._settings),
        )

    async def close(self) -> None:
        await self._backend.close()
