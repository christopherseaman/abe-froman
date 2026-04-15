from __future__ import annotations

from typing import Any

from abe_froman.runtime.result import ExecutionResult
from abe_froman.schema.models import Phase


class MockExecutor:
    def __init__(
        self,
        results: dict[str, ExecutionResult] | None = None,
    ):
        self._results = results or {}
        self.execution_order: list[str] = []
        self.received_contexts: dict[str, dict[str, Any]] = {}

    async def execute(self, phase: Phase, context: dict[str, Any]) -> ExecutionResult:
        self.execution_order.append(phase.id)
        self.received_contexts[phase.id] = context

        if phase.id in self._results:
            return self._results[phase.id]

        return ExecutionResult(
            success=True,
            output=f"[mock] {phase.id} completed",
        )
