from __future__ import annotations

from typing import Any

from abe_froman.runtime.result import ExecutionResult
from abe_froman.runtime.executor.command import CommandExecutor
from abe_froman.runtime.executor.prompt import PromptExecutor
from abe_froman.runtime.result import PromptBackend
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

    async def execute(
        self, phase: Phase, context: dict[str, Any], workdir: str | None = None
    ) -> ExecutionResult:
        execution = phase.execution

        if isinstance(execution, CommandExecution):
            return await self._command_executor.execute(phase, context, workdir=workdir)

        if isinstance(execution, GateOnlyExecution):
            return ExecutionResult(success=True, output=f"[gate-only] {phase.id}")

        if isinstance(execution, PromptExecution):
            if self._prompt_executor is not None:
                return await self._prompt_executor.execute(phase, context, workdir=workdir)
            return ExecutionResult(
                success=True,
                output=f"[prompt-stub] {phase.id}: {execution.prompt_file}",
            )

        if execution is None:
            return ExecutionResult(
                success=False,
                error=f"Phase '{phase.id}' has no execution configuration",
            )

        return ExecutionResult(
            success=False,
            error=f"Unknown execution type: {type(execution).__name__}",
        )

    def get_backend(self) -> PromptBackend | None:
        """Return the PromptBackend, if one is configured.

        Used by the orchestrator to dispatch .md LLM gates through the
        same backend the phase executor uses.
        """
        if self._prompt_executor is None:
            return None
        return self._prompt_executor._backend

    async def close(self) -> None:
        """Clean up backend resources."""
        if self._prompt_executor is not None:
            await self._prompt_executor.close()
