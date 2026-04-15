from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from abe_froman.runtime.result import ExecutionResult
from abe_froman.schema.models import Phase


@runtime_checkable
class PhaseExecutor(Protocol):
    async def execute(self, phase: Phase, context: dict[str, Any]) -> ExecutionResult: ...
