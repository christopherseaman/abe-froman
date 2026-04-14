from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from abe_froman.schema.models import Phase


@dataclass
class PhaseResult:
    success: bool
    output: str = ""
    error: str | None = None
    structured_output: dict[str, Any] | None = None
    tokens_used: dict[str, int] | None = None


@runtime_checkable
class PhaseExecutor(Protocol):
    async def execute(self, phase: Phase, context: dict[str, Any]) -> PhaseResult: ...
