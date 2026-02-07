from __future__ import annotations

from typing import Any

from abe_froman.executor.base import PhaseResult
from abe_froman.schema.models import Phase


class MockExecutor:
    def __init__(
        self,
        results: dict[str, PhaseResult] | None = None,
    ):
        self._results = results or {}
        self.execution_order: list[str] = []
        self.received_contexts: dict[str, dict[str, Any]] = {}

    async def execute(self, phase: Phase, context: dict[str, Any]) -> PhaseResult:
        self.execution_order.append(phase.id)
        self.received_contexts[phase.id] = context

        if phase.id in self._results:
            return self._results[phase.id]

        return PhaseResult(
            success=True,
            output=f"[mock] {phase.id} completed",
        )
