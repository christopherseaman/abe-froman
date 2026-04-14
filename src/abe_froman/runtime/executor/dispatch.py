from __future__ import annotations

from typing import Any

from abe_froman.runtime.executor.base import PhaseResult
from abe_froman.runtime.executor.command import CommandExecutor
from abe_froman.runtime.executor.prompt import PromptExecutor
from abe_froman.runtime.executor.prompt_backend import PromptBackend
from abe_froman.schema.models import CommandExecution, GateOnlyExecution, Phase, PromptExecution, Settings


class DispatchExecutor:
    """Routes execution to the appropriate executor based on phase type.

    - CommandExecution → CommandExecutor (subprocess)
    - GateOnlyExecution → no-op (gate evaluation happens in builder)
    - PromptExecution → PromptExecutor with pluggable PromptBackend
    """

    def __init__(
        self,
        workdir: str = ".",
        prompt_backend: PromptBackend | None = None,
        settings: Settings | None = None,
    ):
        self._command_executor = CommandExecutor(workdir=workdir)
        self._workdir = workdir
        self._settings = settings or Settings()

        if prompt_backend is not None:
            self._prompt_executor: PromptExecutor | None = PromptExecutor(
                backend=prompt_backend,
                settings=self._settings,
                workdir=workdir,
            )
        else:
            self._prompt_executor = None

    async def execute(self, phase: Phase, context: dict[str, Any]) -> PhaseResult:
        execution = phase.execution

        if isinstance(execution, CommandExecution):
            return await self._command_executor.execute(phase, context)

        if isinstance(execution, GateOnlyExecution):
            return PhaseResult(success=True, output=f"[gate-only] {phase.id}")

        if isinstance(execution, PromptExecution):
            if self._prompt_executor is not None:
                return await self._prompt_executor.execute(phase, context)
            return PhaseResult(
                success=True,
                output=f"[prompt-stub] {phase.id}: {execution.prompt_file}",
            )

        if execution is None:
            return PhaseResult(
                success=False,
                error=f"Phase '{phase.id}' has no execution configuration",
            )

        return PhaseResult(
            success=False,
            error=f"Unknown execution type: {type(execution).__name__}",
        )

    async def close(self) -> None:
        """Clean up backend resources."""
        if self._prompt_executor is not None:
            await self._prompt_executor.close()
