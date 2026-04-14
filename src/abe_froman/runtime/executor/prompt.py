from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from abe_froman.runtime.executor.base import PhaseResult
from abe_froman.runtime.executor.prompt_backend import OverloadError, PromptBackend
from abe_froman.runtime.templates import (
    MODEL_DOWNGRADE_CHAIN,
    downgrade_model,
    render_template,
    resolve_model,
)
from abe_froman.schema.models import Phase, PromptExecution, Settings

__all__ = [
    "MODEL_DOWNGRADE_CHAIN",
    "PromptExecutor",
    "downgrade_model",
    "render_template",
    "resolve_model",
]


class PromptExecutor:
    """Executes PromptExecution phases by rendering templates and
    delegating to a PromptBackend.

    Shared responsibilities (not duplicated in backends):
    - Read prompt_file from disk
    - Render {{variable}} templates from context
    - Resolve effective model
    - Attempt JSON parsing for structured_output if output_schema is set
    - Convert PromptBackendResult -> PhaseResult
    """

    def __init__(self, backend: PromptBackend, settings: Settings, workdir: str = "."):
        self._backend = backend
        self._settings = settings
        self._workdir = workdir

    async def execute(self, phase: Phase, context: dict[str, Any]) -> PhaseResult:
        if not isinstance(phase.execution, PromptExecution):
            return PhaseResult(
                success=False,
                error=f"PromptExecutor requires PromptExecution, got {type(phase.execution).__name__}",
            )

        prompt_path = Path(self._workdir) / phase.execution.prompt_file
        try:
            template = prompt_path.read_text()
        except FileNotFoundError:
            return PhaseResult(
                success=False,
                error=f"Prompt file not found: {prompt_path}",
            )

        if self._settings.preamble_file:
            preamble_path = Path(self._workdir) / self._settings.preamble_file
            try:
                preamble = preamble_path.read_text()
                template = preamble + "\n\n" + template
            except FileNotFoundError:
                return PhaseResult(
                    success=False,
                    error=f"Preamble file not found: {preamble_path}",
                )

        rendered = render_template(template, context)
        model = resolve_model(phase, self._settings)

        current_model = model
        try:
            while True:
                try:
                    result = await self._backend.send_prompt(
                        rendered, current_model, self._workdir
                    )
                    break
                except OverloadError:
                    next_model = downgrade_model(current_model)
                    if next_model is None:
                        return PhaseResult(
                            success=False,
                            error=f"API overloaded, exhausted model chain (last: {current_model})",
                        )
                    current_model = next_model
        except Exception as e:
            return PhaseResult(success=False, error=f"Backend error: {e}")

        structured = result.structured_output
        if structured is None and phase.output_schema is not None:
            try:
                structured = json.loads(result.output)
            except (json.JSONDecodeError, TypeError):
                pass

        return PhaseResult(
            success=True,
            output=result.output,
            structured_output=structured,
            tokens_used=result.tokens_used,
        )

    async def close(self) -> None:
        await self._backend.close()
