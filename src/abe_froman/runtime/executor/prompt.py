from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Template

from abe_froman.runtime.result import ExecutionResult, OverloadError, PromptBackend
from abe_froman.schema.models import Phase, PromptExecution, Settings


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


def resolve_model(phase: Phase, settings: Settings) -> str:
    return phase.model or settings.default_model


class PromptExecutor:
    """Renders prompt templates, resolves models, delegates to a PromptBackend."""

    def __init__(self, backend: PromptBackend, settings: Settings, workdir: str = "."):
        self._backend = backend
        self._settings = settings
        self._workdir = workdir

    async def execute(
        self, phase: Phase, context: dict[str, Any], workdir: str | None = None
    ) -> ExecutionResult:
        if not isinstance(phase.execution, PromptExecution):
            return ExecutionResult(
                success=False,
                error=f"PromptExecutor requires PromptExecution, got {type(phase.execution).__name__}",
            )

        effective_workdir = workdir or self._workdir
        prompt_path = Path(effective_workdir) / phase.execution.prompt_file
        try:
            template = prompt_path.read_text()
        except FileNotFoundError:
            return ExecutionResult(
                success=False,
                error=f"Prompt file not found: {prompt_path}",
            )

        if self._settings.preamble_file:
            # Preamble lives with the config, not in a per-phase worktree —
            # always resolve from the base workdir.
            preamble_path = Path(self._workdir) / self._settings.preamble_file
            try:
                preamble = preamble_path.read_text()
                template = preamble + "\n\n" + template
            except FileNotFoundError:
                return ExecutionResult(
                    success=False,
                    error=f"Preamble file not found: {preamble_path}",
                )

        rendered = render_template(template, context)
        current_model = resolve_model(phase, self._settings)
        timeout = phase.effective_timeout(self._settings)

        try:
            while True:
                try:
                    result = await self._backend.send_prompt(
                        rendered, current_model, effective_workdir,
                        timeout=timeout,
                    )
                    break
                except OverloadError:
                    next_model = downgrade_model(
                        current_model, self._settings.model_downgrade_chain
                    )
                    if next_model is None:
                        return ExecutionResult(
                            success=False,
                            error=f"API overloaded, exhausted model chain (last: {current_model})",
                        )
                    current_model = next_model
        except Exception as e:
            return ExecutionResult(success=False, error=f"Backend error: {e}")

        return ExecutionResult(
            success=True,
            output=result.output,
            structured_output=result.structured_output,
            tokens_used=result.tokens_used,
        )

    async def close(self) -> None:
        await self._backend.close()
