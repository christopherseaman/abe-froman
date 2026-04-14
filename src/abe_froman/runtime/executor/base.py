from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from abe_froman.runtime.result import ExecutionResult
from abe_froman.schema.models import Phase

# PhaseResult is the historical name for the unified ExecutionResult.
# Kept as an alias so existing call sites continue to work during
# the refactor. Deleted in Step 14.
PhaseResult = ExecutionResult


@runtime_checkable
class PhaseExecutor(Protocol):
    async def execute(self, phase: Phase, context: dict[str, Any]) -> ExecutionResult: ...
