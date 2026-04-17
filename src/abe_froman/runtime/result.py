"""Execution result type and executor protocols.

ExecutionResult is the single result type for backends, executors, and
subprocesses. PhaseExecutor and PromptBackend are the two duck-typed
protocols that produce them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from abe_froman.schema.models import Phase


@dataclass
class ExecutionResult:
    success: bool = True
    output: str = ""
    error: str | None = None
    structured_output: dict[str, Any] | None = None
    tokens_used: dict[str, int] | None = None


class OverloadError(Exception):
    """Raised by a PromptBackend when the API returns 529/overloaded."""

    pass


@runtime_checkable
class PhaseExecutor(Protocol):
    async def execute(
        self, phase: Phase, context: dict[str, Any], workdir: str | None = None
    ) -> ExecutionResult: ...


@runtime_checkable
class PromptBackend(Protocol):
    async def send_prompt(
        self, prompt: str, model: str, workdir: str
    ) -> ExecutionResult: ...

    async def close(self) -> None: ...
