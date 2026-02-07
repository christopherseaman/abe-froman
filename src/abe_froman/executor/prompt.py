from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from abe_froman.executor.base import PhaseResult
from abe_froman.executor.prompt_backend import PromptBackend
from abe_froman.schema.models import Phase, PromptExecution, Settings


def render_template(template: str, context: dict[str, Any]) -> str:
    """Replace {{variable}} placeholders with values from context.

    Leaves unresolved placeholders intact.
    """

    def replacer(match: re.Match) -> str:
        key = match.group(1).strip()
        if key in context:
            return str(context[key])
        return match.group(0)

    return re.sub(r"\{\{(\s*\w+\s*)\}\}", replacer, template)


def resolve_model(phase: Phase, settings: Settings) -> str:
    """Phase model > settings default_model."""
    return phase.model or settings.default_model


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

        rendered = render_template(template, context)
        model = resolve_model(phase, self._settings)

        try:
            result = await self._backend.send_prompt(rendered, model, self._workdir)
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
        )

    async def close(self) -> None:
        await self._backend.close()
