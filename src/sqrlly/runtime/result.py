"""Execution result type and executor protocols.

ExecutionResult is the single result type for backends, executors, and
subprocesses. NodeExecutor and PromptBackend are the two duck-typed
protocols that produce them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sqrlly.schema.models import Node, Settings


@dataclass
class ExecutionResult:
    success: bool = True
    output: str = ""
    error: str | None = None
    structured_output: dict[str, Any] | None = None


class OverloadError(Exception):
    """Raised by a PromptBackend when the API returns 529/overloaded."""

    pass


class EvaluationError(RuntimeError):
    """A gate validator could not produce a score — it failed to run
    (missing / non-zero exit) or emitted output with no parseable score.
    Distinct from a low-quality verdict (a valid score below threshold):
    a broken validator halts the run loudly rather than masquerading as
    a 0.0 quality result."""


class ManifestError(RuntimeError):
    """A fan-out node's declared ``manifest_path`` could not be read
    (file missing or invalid JSON). Halts loudly rather than silently
    fanning out over zero items."""


@runtime_checkable
class NodeExecutor(Protocol):
    async def execute(
        self, node: Node, context: dict[str, Any],
        workdir: str | None = None,
        settings_override: Settings | None = None,
    ) -> ExecutionResult: ...


@runtime_checkable
class PromptBackend(Protocol):
    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult: ...

    async def close(self) -> None: ...
